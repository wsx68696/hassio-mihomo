#!/usr/bin/env python3
"""Run Mihomo as a normal Supervisor app and persist dashboard choices."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
import tempfile
import copy
from contextlib import contextmanager, ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, ProxyHandler, build_opener

BUNDLE = Path(__file__).resolve().parent
sys.path.insert(0, str(BUNDLE))

from config import EDITABLE, atomic_json, load_profile, load_secret, make_config, trusted_ingress, validate
from downloads import DownloadError, fetch
from updates import Updates
from networking import InstanceLock, NetworkOwner
from policies import (MAX_CONFIG, PolicyError, normalize_policy, prepare_providers, policy_summary,
                      TemporaryMihomo, download_configuration, validate_runtime, load_policy)
from policies import prune_policy_assets
from geodata import prepare_geodata

DATA = Path(os.environ.get("MIHOMO_DATA", "/data"))
STOP = threading.Event()
RELOAD = threading.Event()


class MihomoAPIError(RuntimeError):
    pass


class Controller:
    def __init__(self, data, options, binary="/usr/local/bin/mihomo"):
        self.data = Path(data)
        self.binary = binary
        self.lock = threading.RLock()
        self.profile = load_profile(self.data, options)
        self.secret = load_secret(self.data, options.get("controller_secret", ""))
        self.phase = "starting"
        self.generation = 0
        self.last_update_error = ""
        self.last_runtime_error = ""
        self.active_has_policy = False
        self.policy_info = {"groups": 0, "rules": 0, "rule_providers": 0, "proxy_providers": 0,
                            "geo_auto_update": False, "geo_update_interval": 24}
        self.import_lock = threading.Lock()
        self.http = build_opener(ProxyHandler({}))
        self.updates = Updates(self)
        if (self.data / "nodes.yaml").exists():
            cached = load_policy(self.data / "nodes.yaml")
            self.policy_info = policy_summary(cached)
            self.active_has_policy = bool(cached.get("_full_policy")) and not (cached.get("proxies") or cached.get("proxy-providers"))

    def api(self, endpoint, method="GET", payload=None):
        request = Request("http://127.0.0.1:9090" + endpoint, method=method,
                          data=None if payload is None else json.dumps(payload).encode(),
                          headers={"Authorization": "Bearer " + self.secret, "Content-Type": "application/json"})
        with self.http.open(request, timeout=3) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
        if not raw and method != "GET":
            return {}
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError):
            raise MihomoAPIError("Mihomo API 暂未返回有效 JSON") from None
        if len(raw) > 8 * 1024 * 1024 or not isinstance(result, dict):
            raise MihomoAPIError("Mihomo API 返回格式不正确")
        if method == "GET":
            if endpoint == "/providers/proxies" and "providers" in result and result["providers"] is None:
                result["providers"] = {}
            for key in {"/configs": ("tun",), "/proxies": ("proxies",), "/providers/proxies": ("providers",)}.get(endpoint, ()):
                if not isinstance(result.get(key), dict):
                    raise MihomoAPIError("Mihomo API 返回了不完整的状态")
            if endpoint == "/configs" and (not isinstance(result.get("mode"), str) or result.get("mode") not in {"rule", "global", "direct"}
                                          or not isinstance(result["tun"].get("enable"), bool)):
                raise MihomoAPIError("Mihomo 配置状态格式不正确")
        return result

    def snapshot(self):
        with self.lock:
            return {**{key: self.profile[key] for key in EDITABLE}, "controller_secret": self.secret,
                    "phase": self.phase, "runtime_generation": self.generation, "mode": self.profile.get("mode", "rule"), "last_update_error": self.last_update_error,
                    "last_runtime_error": self.last_runtime_error, "policy": dict(self.policy_info),
                    "software_update": self.updates.snapshot()}

    @contextmanager
    def configured_proxy(self, config=None):
        with self.lock:
            current = config if config is not None else make_config(self.profile, self.secret, self.data)
            binary = self.binary
        try:
            selections = self.api("/proxies")["proxies"]
        except (OSError, MihomoAPIError):
            selections = {}
        current = download_configuration(current, selections)
        with TemporaryMihomo(binary, self.data, current) as proxy:
            for name, values in selections.items():
                if isinstance(values, dict) and values.get("now") and name in {group["name"] for group in current["proxy-groups"]}:
                    try:
                        from urllib.parse import quote
                        proxy.api("/proxies/" + quote(name, safe=""), "PUT", {"name": values["now"]})
                    except (OSError, ValueError):
                        pass
            yield proxy.proxy

    def update(self, changes):
        if not isinstance(changes, dict):
            raise ValueError("配置必须是 JSON 对象")
        if set(changes) - EDITABLE:
            raise ValueError("只允许修改订阅、TUN、DNS、直连域名和更新设置")
        with self.lock:
            proposed = validate({**self.profile, **changes})
            atomic_json(self.data / "profile.json", proposed)
            self.profile = proposed
            if set(changes) - {"subscription_url", "subscription_interval", "subscription_download", "update_download",
                              "geodata_download", "geodata_auto_update", "geodata_update_interval"}:
                RELOAD.set()

    def upload_nodes(self, raw, *, clear_subscription=True, expected_url=None):
        if not self.import_lock.acquire(blocking=False):
            raise PolicyError("已有配置导入正在进行")
        assets = candidate = None
        committed = False
        try:
            with self.lock:
                if self.updates.status["phase"] in {"downloading", "validating", "switching"}:
                    raise PolicyError("软件或规则资料更新正在进行，请稍后导入配置")
                if expected_url is not None and self.profile["subscription_url"] != expected_url:
                    raise PolicyError("订阅地址已改变，本次下载不替换缓存")
                profile = copy.deepcopy(self.profile)
                binary = self.binary
                initial_url = self.profile["subscription_url"]
            policy = normalize_policy(raw)
            descriptor, filename = tempfile.mkstemp(prefix="incoming-", suffix=".yaml", dir=self.data)
            os.close(descriptor)
            candidate = Path(filename)
            # Validate flat nodes before using any new node for provider downloads.
            if policy.get("proxies"):
                atomic_json(candidate, {"proxies": policy["proxies"]})
                result = subprocess.run([binary, "-t", "-d", str(self.data), "-f", str(candidate)],
                                        capture_output=True, timeout=30)
                if result.returncode:
                    raise PolicyError("节点未通过 Mihomo 校验，原配置已保留")
            with ExitStack() as stack:
                proxies = []
                needs_download = any(spec.get("type") == "http" or "url" in spec for key in ("proxy-providers", "rule-providers")
                                     for spec in policy.get(key, {}).values())
                if needs_download and profile["subscription_download"] in {"proxy", "auto"}:
                    if (self.data / "nodes.yaml").exists():
                        try:
                            proxies.append(stack.enter_context(self.configured_proxy()))
                        except (ValueError, OSError):
                            pass
                    if policy.get("proxies"):
                        bare = {"proxies": policy["proxies"], "_full_policy": True}
                        config = make_config(profile, self.secret, self.data, policy=bare)
                        proxies.append(stack.enter_context(self.configured_proxy(config)))

                def download(url, limit, headers):
                    methods = ("proxy", "direct") if profile["subscription_download"] == "auto" else (profile["subscription_download"],)
                    failures = []
                    for method in methods:
                        choices = proxies if method == "proxy" else [None]
                        for proxy in choices:
                            try:
                                body, _ = fetch(url, method, limit, timeout=60, headers=headers,
                                                **({"proxy": proxy} if proxy else {}))
                                return body
                            except DownloadError as error:
                                failures.append(error.details)
                    detail = "，".join(failures) if failures else "没有可用的下载代理"
                    raise PolicyError(f"provider 下载失败（{detail}），原规则和节点已保留")

                policy, assets = prepare_providers(policy, self.data, download)
                atomic_json(candidate, policy)
                config = make_config(profile, self.secret, self.data, nodes_path=candidate, policy=policy)
                validate_runtime(binary, self.data, config, empty_rules=policy.get("_empty_rule_providers", ()))
            with self.lock:
                if self.binary != binary or self.updates.status["phase"] in {"downloading", "validating", "switching"}:
                    raise PolicyError("软件更新状态已改变，请重新导入配置")
                if (expected_url is not None and self.profile["subscription_url"] != expected_url) or (
                    clear_subscription and self.profile["subscription_url"] != initial_url
                ):
                    raise PolicyError("订阅地址已改变，本次下载不替换缓存")
                previous = self.data / "nodes.yaml"
                if previous.exists():
                    backup = self.data / ("previous-" + candidate.name)
                    shutil.copy2(previous, backup)
                    os.replace(backup, self.data / "nodes-previous.yaml")
                os.replace(candidate, previous)
                proposed = dict(self.profile)
                if clear_subscription:
                    proposed["subscription_url"] = ""
                    if "_suggested_mode" in policy:
                        proposed["mode"] = policy["_suggested_mode"]
                for source, target in (("geo-auto-update", "geodata_auto_update"), ("geo-update-interval", "geodata_update_interval")):
                    if source in policy:
                        proposed[target] = policy[source]
                if proposed != self.profile:
                    atomic_json(self.data / "profile.json", proposed)
                    self.profile = proposed
                self.policy_info = policy_summary(policy)
                self.active_has_policy = bool(policy.get("_full_policy")) and not (
                    policy.get("proxies") or policy.get("proxy-providers")
                )
                self.phase = "applying"
                RELOAD.set()
                committed = True
            prune_policy_assets(self.data)
        except (TypeError, RecursionError, UnicodeError) as error:
            if os.environ.get("MIHOMO_DIAGNOSTICS") == "1":
                import traceback
                traceback.print_exc()
            raise PolicyError("无法解析或应用 Clash/Mihomo 配置") from error
        finally:
            if candidate is not None:
                candidate.unlink(missing_ok=True)
            if assets is not None and not committed:
                shutil.rmtree(assets, ignore_errors=True)
            self.import_lock.release()

    def refresh_subscription(self, url, policy):
        methods = ("proxy", "direct") if policy == "auto" else (policy,)
        errors = []
        for method in methods:
            stage = "启动下载代理" if method == "proxy" else "下载订阅"
            try:
                if method == "proxy":
                    with self.configured_proxy() as proxy:
                        stage = "下载订阅"
                        raw, _ = fetch(url, "proxy", MAX_CONFIG, timeout=60, proxy=proxy)
                else:
                    raw, _ = fetch(url, "direct", MAX_CONFIG, timeout=60)
                stage = "校验或应用配置"
                self.upload_nodes(raw, clear_subscription=False, expected_url=url)
            except DownloadError as error:
                errors.append(f"下载订阅失败（{error.details}）")
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                detail = ""
                if isinstance(error, PolicyError):
                    detail = f"（{error}）"
                elif isinstance(error, (TimeoutError, subprocess.TimeoutExpired)):
                    detail = "（超时）"
                errors.append(f"{method} {stage}失败{detail}")
            else:
                with self.lock:
                    self.last_update_error = ""
                print("订阅、分组与规则已通过校验并更新。", flush=True)
                return
        raise ValueError("订阅刷新失败，已保留原配置。\n" + "\n".join(errors))

    def save_runtime(self, general, learn_tun):
        if not isinstance(general, dict) or not isinstance(general.get("tun", {}), dict):
            raise MihomoAPIError("Mihomo 状态格式不正确")
        with self.lock:
            if RELOAD.is_set():
                return  # An in-flight poll belongs to the configuration being replaced.
            proposed = dict(self.profile)
            if general.get("mode") in {"rule", "global", "direct"}:
                proposed["mode"] = general["mode"]
            if general.get("log-level") in {"silent", "error", "warning", "info", "debug"}:
                proposed["log_level"] = general["log-level"]
            if learn_tun:
                proposed["tun_enable"] = bool(general.get("tun", {}).get("enable"))
            if proposed != self.profile:
                atomic_json(self.data / "profile.json", proposed)
                self.profile = proposed

    def update_software(self, component):
        def worker():
            try:
                if self.updates.install(component, reserved=True) == "reload":
                    RELOAD.set()
            except Exception:
                print("软件更新失败，当前版本已保留。", flush=True)
        if not isinstance(component, str) or component not in {"mihomo", "zashboard", "geodata"}:
            raise ValueError("未知更新组件")
        self.updates.reserve(component)
        try:
            threading.Thread(target=worker, daemon=True).start()
        except RuntimeError:
            with self.lock:
                self.updates.reserved = False
                self.updates.component = None
            raise ValueError("无法创建更新任务，请稍后重试") from None


def handler_for(controller):
    class SetupHandler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Subscription URLs and credentials never enter access logs.

        def allowed(self):
            return trusted_ingress(self.client_address[0])

        def reply(self, code, body, content_type="application/json; charset=utf-8"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self.allowed():
                return self.reply(403, {"error": "请通过 Home Assistant 的 Mihomo 面板访问"})
            if self.path.rstrip("/") == "/profile":
                return self.reply(200, controller.snapshot())
            if self.path in {"/", "/index.html"}:
                return self.reply(200, (BUNDLE / "setup.html").read_bytes(), "text/html; charset=utf-8")
            self.reply(404, {"error": "Not found"})

        def do_POST(self):
            if not self.allowed():
                return self.reply(403, {"error": "Ingress only"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > MAX_CONFIG:
                    raise ValueError("请求大小不正确")
                raw = self.rfile.read(length)
                if self.path == "/profile":
                    changes = json.loads(raw)
                    if not isinstance(changes, dict):
                        raise ValueError("配置必须是 JSON 对象")
                    controller.update(changes)
                elif self.path == "/nodes":
                    controller.upload_nodes(raw)
                elif self.path == "/update":
                    changes = json.loads(raw)
                    if not isinstance(changes, dict):
                        raise ValueError("更新请求必须是 JSON 对象")
                    controller.update_software(changes.get("component"))
                else:
                    return self.reply(404, {"error": "Not found"})
                self.reply(200, {"ok": True})
            except (ValueError, TimeoutError, subprocess.TimeoutExpired) as error:
                self.reply(400, {"error": str(error)})

    return SetupHandler


def stop_child(process, ownership):
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    finally:
        ownership.cleanup()


def subscription_worker(controller):
    previous_url = ""
    next_attempt = 0.0
    while not STOP.is_set():
        with controller.lock:
            profile = dict(controller.profile)
        url = profile["subscription_url"]
        if url and (url != previous_url or time.monotonic() >= next_attempt):
            previous_url = url
            try:
                controller.refresh_subscription(url, profile["subscription_download"])
                next_attempt = time.monotonic() + profile["subscription_interval"]
            except ValueError as error:
                with controller.lock:
                    controller.last_update_error = str(error)
                print(str(error), flush=True)
                next_attempt = time.monotonic() + 60
        STOP.wait(2)


def geodata_worker(controller):
    next_attempt = time.monotonic() + 60
    while not STOP.is_set():
        with controller.lock:
            automatic = controller.profile.get("geodata_auto_update", False)
            interval = controller.profile.get("geodata_update_interval", 24)
        if automatic and time.monotonic() >= next_attempt:
            try:
                controller.update_software("geodata")
                next_attempt = time.monotonic() + interval * 3600
            except ValueError:
                next_attempt = time.monotonic() + 60
        STOP.wait(5)


def usable_proxies(controller, providers, proxies):
    if not (controller.data / "nodes.yaml").exists():
        return False
    if controller.active_has_policy:
        return True
    group_types = {"selector", "urltest", "fallback", "loadbalance", "relay", "smart",
                   "direct", "reject", "rejectdrop", "compatible", "pass"}
    for proxy in proxies.values():
        if not isinstance(proxy, dict):
            raise MihomoAPIError("代理状态格式不正确")
        kind = str(proxy.get("type", "")).replace("-", "").replace("_", "").lower()
        if kind and kind not in group_types:
            return True
    return any(isinstance(provider, dict) and bool(provider.get("proxies")) for provider in providers.values())


def supervise(controller, ownership):
    child = None
    failures = 0
    retry_at = 0.0
    started_at = 0.0
    last_good_api = 0.0
    settled = False
    running_binary = None
    running_update = None

    def failed(message):
        nonlocal failures, retry_at
        failures += 1
        retry_at = time.monotonic() + min(15, 2 ** min(failures, 4))
        with controller.lock:
            controller.phase = "restarting"
            controller.last_runtime_error = message
        print(message, flush=True)

    try:
        while not STOP.is_set():
            if child is not None and child.poll() is not None:
                code = child.returncode
                try:
                    stop_child(child, ownership)
                except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                    print(str(error), flush=True)
                child = None
                try:
                    rolled_back = controller.updates.rollback()
                except (OSError, RuntimeError, ValueError) as error:
                    failed(str(error))
                    continue
                if rolled_back:
                    failures = 0
                    retry_at = 0
                else:
                    failed(f"Mihomo 子进程退出（{code}），正在恢复。")
            if (child is not None and controller.updates.pending
                    and running_update == controller.updates.pending["id"] and time.monotonic() - started_at > 40):
                try:
                    stop_child(child, ownership)
                except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                    print(str(error), flush=True)
                finally:
                    child = None
                try:
                    controller.updates.rollback()
                except (OSError, RuntimeError, ValueError) as error:
                    failed(str(error))
                continue
            if child is None or RELOAD.is_set():
                if time.monotonic() < retry_at:
                    STOP.wait(min(1, retry_at - time.monotonic()))
                    continue
                RELOAD.clear()
                try:
                    if child is not None:
                        stop_child(child, ownership)
                        child = None
                    ownership.prepare()
                    with controller.lock:
                        atomic_json(controller.data / "config.json",
                                    make_config(controller.profile, controller.secret, controller.data))
                        controller.phase = "waiting-for-nodes"
                        running_binary = controller.binary
                        running_update = controller.updates.pending["id"] if controller.updates.pending else None
                        child = subprocess.Popen([running_binary, "-d", str(controller.data),
                                                  "-f", str(controller.data / "config.json")])
                        controller.generation += 1
                    started_at = last_good_api = time.monotonic()
                    settled = False
                except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
                    failed(str(error))
                    continue
            try:
                general = controller.api("/configs")
                providers = controller.api("/providers/proxies")["providers"]
                proxies = controller.api("/proxies")["proxies"]
                ready = usable_proxies(controller, providers, proxies)
                with controller.lock:
                    desired = controller.profile["tun_enable"]
                    controller.phase = "running" if ready else "waiting-for-nodes"
                    controller.last_runtime_error = ""
                if ready and not settled:
                    if desired and not ownership.claimed:
                        ownership.prepare()
                    controller.api("/configs", "PATCH", {"tun": {"enable": desired}})
                    ownership.stamp()
                    settled = True
                    last_good_api = time.monotonic()
                    failures = 0
                    STOP.wait(1)
                    continue
                if not ready:
                    settled = False
                    if general["tun"].get("enable"):
                        controller.api("/configs", "PATCH", {"tun": {"enable": False}})
                        ownership.cleanup()
                elif general["tun"].get("enable"):
                    ownership.stamp()
                if not controller.updates.pending:
                    controller.save_runtime(general, ready and settled)
                elif time.monotonic() - started_at >= 5 and (not ready or bool(general["tun"].get("enable")) == desired):
                    if not controller.updates.confirm(running_binary=running_binary, running_update=running_update):
                        RELOAD.set()
                last_good_api = time.monotonic()
                failures = 0
            except (OSError, MihomoAPIError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
                with controller.lock:
                    controller.last_runtime_error = str(error)
                if time.monotonic() - last_good_api >= 30:
                    try:
                        stop_child(child, ownership)
                    except (OSError, RuntimeError, subprocess.SubprocessError) as cleanup_error:
                        print(str(cleanup_error), flush=True)
                    finally:
                        child = None
                    failed("Mihomo 健康检查持续失败，正在恢复子进程。")
            STOP.wait(2)
    finally:
        if child is not None:
            stop_child(child, ownership)
        else:
            ownership.cleanup()


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA, 0o700)
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: STOP.set())
    with InstanceLock(DATA):
        ownership = NetworkOwner(DATA)
        ownership.cleanup()
        options = json.loads((DATA / "options.json").read_text(encoding="utf-8"))
        version = json.loads((BUNDLE / "addon.json").read_text(encoding="utf-8"))["version"]
        bundle_stamp = DATA / "ui-bundle-version"
        software = json.loads((DATA / "software.json").read_text(encoding="utf-8")) if (DATA / "software.json").exists() else {}
        prepare_geodata(DATA, BUNDLE / "geodata", software.get("geodata"))
        if not (DATA / "ui/index.html").exists() or (not software.get("zashboard") and (not bundle_stamp.exists() or bundle_stamp.read_text(encoding="utf-8") != version)):
            shutil.copytree(BUNDLE / "ui", DATA / "ui", dirs_exist_ok=True)
            bundle_stamp.write_text(version, encoding="utf-8")
        controller = Controller(DATA, options)
        server = ThreadingHTTPServer(("0.0.0.0", 9091), handler_for(controller))
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        threading.Thread(target=subscription_worker, args=(controller,), daemon=True).start()
        threading.Thread(target=geodata_worker, args=(controller,), daemon=True).start()
        print("Mihomo + Zashboard: 在 Home Assistant 的 Mihomo 面板中配置订阅或导入 Clash 配置。", flush=True)
        try:
            supervise(controller, ownership)
        finally:
            server.shutdown()
            server.server_close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(f"Mihomo app: {error}", file=sys.stderr)
        sys.exit(1)
