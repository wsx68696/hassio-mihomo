"""Import Clash routing policy without importing host or controller settings."""
import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from config import GEO_FILES, OUTPUT_MARK, atomic_json, atomic_text
from geodata import write_durable, localize_providers

MAX_CONFIG = 8 * 1024 * 1024
MAX_PROVIDER = 32 * 1024 * 1024
ROUTING_KEYS = {"proxies", "proxy-providers", "proxy-groups", "rules", "rule-providers", "sub-rules",
                "geodata-mode", "geodata-loader", "geosite-matcher", "geo-auto-update", "geo-update-interval"}
BUILTINS = {"DIRECT", "REJECT", "REJECT-DROP", "PASS", "COMPATIBLE", "GLOBAL"}


def bounded_json(value):
    parts, total = [], 0
    for part in json.JSONEncoder(ensure_ascii=False).iterencode(value):
        total += len(part.encode())
        if total > MAX_CONFIG:
            raise ValueError("展开后的配置超过 8 MiB")
        parts.append(part)
    return "".join(parts)


def parse_document(raw):
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError):
        try:
            import yaml
        except ImportError:
            raise ValueError("缺少安全 YAML 解析器") from None
        try:
            return yaml.safe_load(raw)
        except (ValueError, UnicodeError, RecursionError, yaml.YAMLError):
            raise ValueError("Clash/Mihomo YAML 格式不正确") from None


def normalize_policy(raw):
    if not raw or len(raw) > MAX_CONFIG:
        raise ValueError("配置为空或超过 8 MiB")
    original = parse_document(raw)
    if not isinstance(original, dict):
        raise ValueError("Clash 配置必须是对象")
    policy = copy.deepcopy({key: original[key] for key in ROUTING_KEYS if key in original})
    nodes = policy.get("proxies", [])
    if not isinstance(nodes, list) or len(nodes) > 4096 or any(not isinstance(node, dict) for node in nodes):
        raise ValueError("proxies 必须是节点对象列表，最多 4096 个")
    groups = policy.get("proxy-groups", [])
    if not isinstance(groups, list) or len(groups) > 1024 or any(not isinstance(group, dict) for group in groups):
        raise ValueError("proxy-groups 格式不正确")
    for entry in [*nodes, *groups]:
        name = entry.get("name")
        if not isinstance(name, str) or not name or len(name) > 256 or name in BUILTINS:
            raise ValueError("节点和分组需要有效名称，不能覆盖内置 DIRECT/REJECT/GLOBAL")
    for key in ("proxy-providers", "rule-providers"):
        providers = policy.get(key, {})
        if not isinstance(providers, dict) or len(providers) > 128 or any(
            not isinstance(name, str) or not name or len(name) > 256 or not isinstance(spec, dict)
            for name, spec in providers.items()
        ):
            raise ValueError(key + " 格式不正确")
    rules = policy.get("rules", [])
    if not isinstance(rules, list) or len(rules) > 65536 or any(not isinstance(rule, str) or len(rule) > 4096 for rule in rules):
        raise ValueError("rules 必须是规则字符串列表")
    subrules = policy.get("sub-rules", {})
    if not isinstance(subrules, dict) or any(not isinstance(name, str) or not isinstance(values, list)
                                          or any(not isinstance(rule, str) for rule in values) for name, values in subrules.items()):
        raise ValueError("sub-rules 格式不正确")
    if not (nodes or policy.get("proxy-providers") or rules or policy.get("rule-providers") or subrules):
        raise ValueError("配置没有节点、规则或 provider")
    for key in ("geodata-mode", "geo-auto-update"):
        if key in policy and not isinstance(policy[key], bool):
            raise ValueError(key + " 必须是布尔值")
    if "geo-update-interval" in policy and (type(policy["geo-update-interval"]) is not int or not 1 <= policy["geo-update-interval"] <= 720):
        raise ValueError("geo-update-interval 必须为 1 至 720 小时")
    if "mode" in original and (not isinstance(original["mode"], str) or original["mode"] not in {"rule", "global", "direct"}):
        raise ValueError("mode 必须是 rule、global 或 direct")
    if isinstance(original.get("mode"), str):
        policy["_suggested_mode"] = original["mode"]
    policy["_full_policy"] = any(key in original for key in ("proxy-groups", "proxy-providers", "rules", "rule-providers", "sub-rules"))
    try:
        policy = json.loads(bounded_json(policy))
    except (TypeError, RecursionError):
        raise ValueError("配置包含不支持的数据类型或循环引用") from None
    return policy


def load_policy(path):
    raw = Path(path).read_bytes()
    value = parse_document(raw)
    if not isinstance(value, dict):
        raise ValueError("保存的 Clash 配置格式不正确")
    return value


def request_headers(spec):
    supplied = spec.get("header", {})
    if not isinstance(supplied, dict) or len(supplied) > 32:
        raise ValueError("provider HTTP header 格式不正确")
    result = {}
    for key, value in supplied.items():
        values = value if isinstance(value, list) else [value]
        if not isinstance(key, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key) or any(
            not isinstance(item, str) or "\r" in item or "\n" in item for item in values
        ):
            raise ValueError("provider HTTP header 格式不正确")
        result[key] = ", ".join(values)
        supplied[key] = values
    return result


def provider_path(data, value):
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("文件 provider 需要 user-providers 内的相对路径")
    relative = PurePosixPath(value)
    if ".." in relative.parts or any(":" in part for part in relative.parts):
        raise ValueError("文件 provider 路径不能越界")
    root = (Path(data) / "user-providers").resolve()
    path = Path(value).resolve() if relative.is_absolute() else (root / Path(*relative.parts)).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("文件 provider 只能读取已放入 user-providers 的规则文件")
    return path


def prepare_providers(policy, data, downloader):
    policy = copy.deepcopy(policy)
    assets = Path(tempfile.mkdtemp(prefix="policy-assets-", dir=data))
    total = 0
    empty_rules = []
    try:
        atomic_text(assets / ".ha-mihomo-policy", "managed-policy-v1\n")
        for kind in ("proxy-providers", "rule-providers"):
            for name, spec in policy.get(kind, {}).items():
                provider_type = spec.get("type")
                if provider_type is None:
                    provider_type = "inline" if "payload" in spec else "http" if "url" in spec else "file"
                if provider_type not in {"inline", "http", "file"}:
                    raise ValueError("provider type 仅支持 http、file 或 inline")
                spec["type"] = provider_type
                if provider_type == "inline":
                    payload = spec.get("payload")
                    if kind == "rule-providers":
                        if spec.get("behavior") not in {"domain", "ipcidr", "classical"}:
                            raise ValueError("inline rule-provider behavior 不正确")
                        if not isinstance(payload, list) or any(not isinstance(item, str) for item in payload):
                            raise ValueError("inline rule-provider payload 必须是规则列表")
                        if not payload:
                            empty_rules.append(name)
                        spec.pop("path", None)
                        spec.pop("url", None)
                        continue
                    raw = json.dumps({"proxies": payload}, ensure_ascii=False).encode()
                elif provider_type == "http":
                    url = spec.get("url")
                    if not isinstance(url, str):
                        raise ValueError("HTTP provider 缺少 URL")
                    parsed = urlsplit(url)
                    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                        raise ValueError("HTTP provider URL 不正确")
                    raw = downloader(url, MAX_PROVIDER, request_headers(spec))
                else:
                    source = provider_path(data, spec.get("path"))
                    if source.stat().st_size > MAX_PROVIDER:
                        raise ValueError("provider 文件超过 32 MiB")
                    raw = source.read_bytes()
                total += len(raw)
                if total > 128 * 1024 * 1024:
                    raise ValueError("provider 文件总计超过 128 MiB")
                file_format = spec.get("format", "yaml")
                if kind == "proxy-providers":
                    parsed = normalize_policy(raw)
                    nodes = parsed.get("proxies")
                    if not nodes:
                        raise ValueError("proxy-provider 没有可用节点定义")
                    raw = json.dumps({"proxies": nodes}, ensure_ascii=False).encode()
                    file_format = "yaml"
                    if provider_type == "inline":
                        spec["type"] = "file"
                        spec.pop("payload", None)
                else:
                    if spec.get("behavior") not in {"domain", "ipcidr", "classical"} or file_format not in {"yaml", "text", "mrs"}:
                        raise ValueError("rule-provider behavior/format 不正确")
                    if file_format == "yaml":
                        parsed = parse_document(raw)
                        if not isinstance(parsed, dict) or not isinstance(parsed.get("payload"), list) or any(not isinstance(item, str) for item in parsed["payload"]):
                            raise ValueError("rule-provider 必须包含 payload 规则列表")
                        if not parsed["payload"]:
                            empty_rules.append(name)
                        # Mihomo's rule-provider loader scans a YAML payload
                        # block; JSON is valid YAML but that scanner does not
                        # accept a JSON object. Canonicalize this input first.
                        lines = [b"payload:\n"]
                        canonical_size = len(lines[0])
                        for rule in parsed["payload"]:
                            line = ("  - " + json.dumps(rule, ensure_ascii=False) + "\n").encode()
                            canonical_size += len(line)
                            if canonical_size > MAX_PROVIDER:
                                raise ValueError("展开后的规则 provider 超过 32 MiB")
                            lines.append(line)
                        raw = b"".join(lines)
                    elif file_format == "text" and not raw.strip():
                        empty_rules.append(name)
                destination = assets / (kind + "-" + hashlib.sha256(name.encode()).hexdigest()[:20] + "." + file_format)
                write_durable(destination, raw)
                spec["path"] = str(destination)
        policy["_empty_rule_providers"] = empty_rules
        return policy, assets
    except Exception:
        shutil.rmtree(assets)
        raise


def prune_policy_assets(data):
    """Keep cached assets for current, previous and still-running policies."""
    data = Path(data).resolve()
    keep = set()
    try:
        for name in ("nodes.yaml", "nodes-previous.yaml", "config.json"):
            if not (data / name).is_file():
                continue
            policy = load_policy(data / name)
            for kind in ("proxy-providers", "rule-providers"):
                for spec in policy.get(kind, {}).values():
                    if spec.get("path"):
                        folder = Path(spec["path"]).resolve().parent
                        if folder.parent == data:
                            keep.add(folder)
    except (OSError, ValueError, TypeError, AttributeError):
        return
    for folder in data.glob("policy-assets-*"):
        marker = folder / ".ha-mihomo-policy"
        if (folder.is_symlink() or not folder.is_dir() or folder.resolve().parent != data
                or folder.resolve() in keep or marker.is_symlink()):
            continue
        try:
            if marker.is_file() and marker.read_text(encoding="utf-8") == "managed-policy-v1\n":
                shutil.rmtree(folder)
        except OSError:
            pass


def local_sources(config, *, checking=True):
    config = copy.deepcopy(config)
    for kind in ("proxy-providers", "rule-providers"):
        for spec in config.get(kind, {}).values():
            if spec.get("type") == "http":
                spec["type"] = "file"
                for key in ("url", "header", "proxy"):
                    spec.pop(key, None)
            if checking and kind == "proxy-providers" and isinstance(spec.get("health-check"), dict):
                spec["health-check"]["enable"] = False
    return config


def download_configuration(config, selections=None):
    config = local_sources(config)
    sources = list(config.get("proxy-providers", {}))
    nodes = [node["name"] for node in config.get("proxies", [])]
    if not sources and not nodes:
        raise ValueError("没有已配置的代理节点，请选择直连或先导入节点")
    groups = config.setdefault("proxy-groups", [])
    preferred = next((group["name"] for group in groups if group["name"] == "PROXY"), groups[0]["name"] if groups else None)
    selected = preferred
    seen = set()
    while (selected and selected not in seen and isinstance((selections or {}).get(selected), dict)
           and "now" in selections[selected]):
        seen.add(selected)
        selected = selections[selected].get("now")
    name = "__ha_download_" + secrets.token_hex(8)
    if preferred and selected and selected not in BUILTINS:
        group = {"name": name, "type": "select", "proxies": [preferred]}
    else:
        group = {"name": name, "type": "fallback", "proxies": nodes, "use": sources,
                 "url": "https://cp.cloudflare.com/generate_204", "interval": 300, "lazy": True}
    groups.append(group)
    config["rules"] = ["MATCH," + name]
    config.pop("rule-providers", None)
    config.pop("sub-rules", None)
    return config


def free_port():
    with socket.socket() as stream:
        stream.bind(("127.0.0.1", 0))
        return stream.getsockname()[1]


class TemporaryMihomo:
    def __init__(self, binary, data, config, *, empty_rules=()):
        self.binary, self.data = binary, Path(data)
        self.config = local_sources(config)
        self.empty_rules = set(empty_rules)
        self.process = self.log = self.home = None
        self.secret = secrets.token_urlsafe(24)
        self.http_port, self.proxy_port = free_port(), free_port()
        self.http = build_opener(ProxyHandler({}))

    def api(self, path, method="GET", payload=None):
        request = Request(f"http://127.0.0.1:{self.http_port}{path}", method=method,
                          data=None if payload is None else json.dumps(payload).encode(),
                          headers={"Authorization": "Bearer " + self.secret, "Content-Type": "application/json"})
        with self.http.open(request, timeout=3) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
        if not raw and method != "GET":
            return {}
        result = json.loads(raw)
        if len(raw) > 8 * 1024 * 1024 or not isinstance(result, dict):
            raise ValueError("验证进程返回格式不正确")
        if path in {"/providers/proxies", "/providers/rules"}:
            if "providers" not in result:
                raise ValueError("验证进程未返回 provider 状态")
            if result["providers"] is None:
                result["providers"] = {}
            if not isinstance(result["providers"], dict):
                raise ValueError("验证进程的 provider 状态格式不正确")
        return result

    def __enter__(self):
        self.home = Path(tempfile.mkdtemp(prefix="validation-", dir=self.data))
        try:
            for name in GEO_FILES.values():
                if (self.data / name).is_file():
                    (self.home / name).symlink_to((self.data / name).resolve())
            config = {**self.config, "mode": "rule", "tun": {"enable": False}, "routing-mark": OUTPUT_MARK,
                      "mixed-port": self.proxy_port, "bind-address": "127.0.0.1", "allow-lan": False,
                      "external-controller": f"127.0.0.1:{self.http_port}", "secret": self.secret,
                      "external-ui": "", "geo-auto-update": False,
                      "profile": {"store-selected": False, "store-fake-ip": False}}
            localize_providers(config, self.data, self.home / "providers")
            config.pop("external-ui-url", None)
            config["dns"] = {**config.get("dns", {}), "listen": ""}
            filename = self.home / "config.json"
            atomic_json(filename, config)
            self.log = (self.home / "startup.log").open("wb")
            self.process = subprocess.Popen([self.binary, "-d", str(self.home), "-f", str(filename)], stdout=self.log, stderr=subprocess.STDOUT)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise ValueError("配置未通过 Mihomo 实际启动校验")
                try:
                    self.api("/configs")
                    providers = self.api("/providers/proxies").get("providers", {})
                    rules = self.api("/providers/rules").get("providers", {})
                    if os.environ.get("MIHOMO_DIAGNOSTICS") == "1":
                        atomic_json(self.home / "provider-status.json", {"rules": rules, "proxy_provider_names": list(providers)})
                    if all(providers.get(name, {}).get("proxies") for name in config.get("proxy-providers", {})) and all(
                        name in rules and (name in self.empty_rules or rules[name].get("ruleCount", 0) > 0)
                        for name in config.get("rule-providers", {})
                    ):
                        return self
                except (OSError, ValueError):
                    pass
                time.sleep(0.2)
            raise ValueError("provider 未能在验证进程中加载，原配置已保留")
        except Exception:
            self.__exit__()
            raise

    @property
    def proxy(self):
        return f"http://127.0.0.1:{self.proxy_port}"

    def __exit__(self, *_):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.log is not None:
            self.log.close()
        if self.home is not None and self.home.exists():
            if os.environ.get("MIHOMO_DIAGNOSTICS") == "1" and (self.home / "startup.log").exists():
                with (self.data / "validation-errors.log").open("ab") as target:
                    target.write((self.home / "startup.log").read_bytes()[-16000:] + b"\n")
                    if (self.home / "provider-status.json").exists():
                        target.write((self.home / "provider-status.json").read_bytes() + b"\n")
            shutil.rmtree(self.home)


def validate_runtime(binary, data, config, *, empty_rules=()):
    with TemporaryMihomo(binary, data, config, empty_rules=empty_rules):
        return True


def policy_summary(policy):
    return {"groups": len(policy.get("proxy-groups", [])), "rules": len(policy.get("rules", [])),
            "rule_providers": len(policy.get("rule-providers", {})), "proxy_providers": len(policy.get("proxy-providers", {})),
            "geo_auto_update": bool(policy.get("geo-auto-update")), "geo_update_interval": policy.get("geo-update-interval", 24)}
