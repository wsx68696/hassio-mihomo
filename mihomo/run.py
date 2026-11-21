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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, ProxyHandler, build_opener

BUNDLE = Path(__file__).resolve().parent
sys.path.insert(0, str(BUNDLE))

from config import EDITABLE, atomic_json, load_profile, load_secret, make_config, trusted_ingress, validate
from downloads import opener_for
from updates import Updates

DATA = Path(os.environ.get("MIHOMO_DATA", "/data"))
STOP = threading.Event()
RELOAD = threading.Event()


class Controller:
    def __init__(self, data, options, binary="/usr/local/bin/mihomo"):
        self.data = Path(data)
        self.binary = binary
        self.lock = threading.RLock()
        self.profile = load_profile(self.data, options)
        self.secret = load_secret(self.data, options.get("controller_secret", ""))
        self.phase = "starting"
        self.last_update_error = ""
        self.http = build_opener(ProxyHandler({}))
        self.updates = Updates(self)

    def api(self, endpoint, method="GET", payload=None):
        request = Request("http://127.0.0.1:9090" + endpoint, method=method,
                          data=None if payload is None else json.dumps(payload).encode(),
                          headers={"Authorization": "Bearer " + self.secret, "Content-Type": "application/json"})
        with self.http.open(request, timeout=3) as response:
            raw = response.read()
            return json.loads(raw) if raw else None

    def snapshot(self):
        with self.lock:
            return {**{key: self.profile[key] for key in EDITABLE}, "controller_secret": self.secret,
                    "phase": self.phase, "mode": self.profile.get("mode", "rule"), "last_update_error": self.last_update_error,
                    "software_update": self.updates.snapshot()}

    def update(self, changes):
        if set(changes) - EDITABLE:
            raise ValueError("只允许修改订阅、TUN、DNS 和直连域名")
        with self.lock:
            proposed = validate({**self.profile, **changes})
            atomic_json(self.data / "profile.json", proposed)
            self.profile = proposed
            if set(changes) - {"subscription_url", "subscription_interval", "subscription_download", "update_download"}:
                RELOAD.set()

    def upload_nodes(self, raw, *, clear_subscription=True, expected_url=None):
        if not raw or len(raw) > 2 * 1024 * 1024:
            raise ValueError("节点文件为空或超过 2 MiB")
        try:
            try:
                parsed = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                import yaml
                try:
                    parsed = yaml.safe_load(raw)
                except yaml.YAMLError as error:
                    raise ValueError("节点 YAML 格式不正确") from error
            nodes = parsed.get("proxies") if isinstance(parsed, dict) else None
            if not isinstance(nodes, list) or not 1 <= len(nodes) <= 4096 or any(not isinstance(node, dict) for node in nodes):
                raise ValueError("节点文件必须包含非空 proxies 列表")
            raw = (json.dumps({"proxies": nodes}, ensure_ascii=False) + "\n").encode()
        except (ImportError, RecursionError, TypeError, UnicodeError) as error:
            raise ValueError("无法解析 Clash/Mihomo 节点文件") from error
        descriptor, filename = tempfile.mkstemp(prefix="incoming-", suffix=".yaml", dir=self.data)
        os.close(descriptor)
        candidate = Path(filename)
        check = candidate.with_suffix(".json")
        try:
            candidate.write_bytes(raw)
            os.chmod(candidate, 0o600)
            # -t does not initialize file providers. Parse the normalized flat
            # proxies list directly so malformed nodes cannot replace a cache.
            result = subprocess.run([self.binary, "-t", "-d", str(self.data), "-f", str(candidate)],
                                    capture_output=True, timeout=30)
            if result.returncode:
                raise ValueError("节点文件未通过 Mihomo 校验，请使用 Clash/Mihomo 格式")
            with self.lock:
                if expected_url is not None and self.profile["subscription_url"] != expected_url:
                    raise ValueError("订阅地址已改变，本次下载不替换缓存节点")
                os.replace(candidate, self.data / "nodes.yaml")
            if clear_subscription:
                self.update({"subscription_url": ""})
            try:
                self.api("/providers/proxies/subscription", "PUT")
            except OSError:
                RELOAD.set()
        finally:
            candidate.unlink(missing_ok=True)
            check.unlink(missing_ok=True)

    def refresh_subscription(self, url, policy):
        # Existing Mihomo stays alive while we fetch and validate a replacement.
        # A file provider avoids a subscription -> selector -> subscription cycle.
        methods = ("proxy", "direct") if policy == "auto" else (policy,)
        if policy == "proxy" and not (self.data / "nodes.yaml").exists():
            raise ValueError("暂无缓存节点，请先导入节点，或选择自动/直连下载")
        errors = []
        for method in methods:
            opener = opener_for(method)
            try:
                request = Request(url, headers={"User-Agent": "mihomo/1.19.34", "Accept-Encoding": "identity"})
                with opener.open(request, timeout=20) as response:
                    raw = response.read(2 * 1024 * 1024 + 1)
                self.upload_nodes(raw, clear_subscription=False, expected_url=url)
                with self.lock:
                    self.last_update_error = ""
                print(f"订阅已通过{('现有代理' if method == 'proxy' else '直连')}更新并通过校验。", flush=True)
                return
            except (OSError, ValueError, subprocess.TimeoutExpired):
                errors.append(method)
        raise ValueError("订阅下载或校验失败，已保留原有节点。尝试方式：" + ", ".join(errors))

    def save_runtime(self, general, learn_tun):
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
                if self.updates.install(component) == "reload":
                    RELOAD.set()
            except Exception:
                print("软件更新失败，当前版本已保留。", flush=True)
        if component not in {"mihomo", "zashboard"}:
            raise ValueError("未知更新组件")
        with self.lock:
            if self.updates.status["phase"] in {"downloading", "validating", "switching"}:
                raise ValueError("已有更新正在进行")
        threading.Thread(target=worker, daemon=True).start()


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
                if length < 1 or length > 2 * 1024 * 1024:
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
                    controller.update_software(changes.get("component"))
                else:
                    return self.reply(404, {"error": "Not found"})
                self.reply(200, {"ok": True})
            except (ValueError, TimeoutError, subprocess.TimeoutExpired) as error:
                self.reply(400, {"error": str(error)})

    return SetupHandler


def stop_child(process, owns_table):
    if process.poll() is not None:
        if owns_table and process.returncode:
            subprocess.run(["nft", "delete", "table", "inet", "mihomo"], capture_output=True)
        return
    process.terminate()
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
        # Only the table absent before this child started can belong to us.
        # Never flush Docker's rules or the system ruleset.
        if owns_table:
            subprocess.run(["nft", "delete", "table", "inet", "mihomo"], capture_output=True)


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


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA, 0o700)
    options = json.loads((DATA / "options.json").read_text(encoding="utf-8"))
    version = json.loads((BUNDLE / "addon.json").read_text(encoding="utf-8"))["version"]
    bundle_stamp = DATA / "ui-bundle-version"
    software = json.loads((DATA / "software.json").read_text(encoding="utf-8")) if (DATA / "software.json").exists() else {}
    if not (DATA / "ui/index.html").exists() or (not software.get("zashboard") and (not bundle_stamp.exists() or bundle_stamp.read_text(encoding="utf-8") != version)):
        shutil.copytree(BUNDLE / "ui", DATA / "ui", dirs_exist_ok=True)
        bundle_stamp.write_text(version, encoding="utf-8")
    controller = Controller(DATA, options)
    server = ThreadingHTTPServer(("0.0.0.0", 9091), handler_for(controller))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=subscription_worker, args=(controller,), daemon=True).start()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: STOP.set())
    print("Mihomo + Zashboard: 在 Home Assistant 的 Mihomo 面板中配置订阅或导入节点。", flush=True)
    child = None
    owns_table = False
    try:
        while not STOP.is_set():
            if child is None or RELOAD.is_set():
                RELOAD.clear()
                if child is not None:
                    stop_child(child, owns_table)
                with controller.lock:
                    atomic_json(DATA / "config.json", make_config(controller.profile, controller.secret, DATA))
                    controller.phase = "waiting-for-nodes"
                existing = subprocess.run(["nft", "list", "table", "inet", "mihomo"], capture_output=True)
                owns_table = existing.returncode != 0
                if not owns_table:
                    raise RuntimeError("检测到已有 Mihomo 防火墙表，请先停止其他 Mihomo 实例")
                child = subprocess.Popen([controller.binary, "-d", str(DATA), "-f", str(DATA / "config.json")])
                started_at = time.monotonic()
                settled = False
            if child.poll() is not None:
                stop_child(child, owns_table)
                if controller.updates.rollback():
                    child = None
                    continue
                raise RuntimeError(f"Mihomo 已退出（状态 {child.returncode}）")
            if controller.updates.pending and time.monotonic() - started_at > 40:
                stop_child(child, owns_table)
                controller.updates.rollback()
                child = None
                continue
            try:
                general = controller.api("/configs")
                providers = controller.api("/providers/proxies").get("providers", {})
                ready = bool(providers.get("subscription", {}).get("proxies"))
                with controller.lock:
                    desired = controller.profile["tun_enable"]
                    controller.phase = "running" if ready else "waiting-for-nodes"
                if ready and not settled:
                    controller.api("/configs", "PATCH", {"tun": {"enable": desired}})
                    settled = True
                    STOP.wait(1)
                    continue
                if not ready:
                    settled = False
                    if general.get("tun", {}).get("enable"):
                        controller.api("/configs", "PATCH", {"tun": {"enable": False}})
                if not controller.updates.pending:
                    controller.save_runtime(general, ready and settled)
                elif time.monotonic() - started_at >= 5 and (not ready or bool(general.get("tun", {}).get("enable")) == desired):
                    controller.updates.confirm()
            except OSError:
                pass  # Controller and providers become ready asynchronously.
            STOP.wait(2)
    finally:
        if child is not None:
            stop_child(child, owns_table)
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(f"Mihomo app: {error}", file=sys.stderr)
        sys.exit(1)
