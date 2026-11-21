"""Update this app's programs, UI and Geo data with one reserved operation."""
import copy
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from contextlib import ExitStack

from config import atomic_json, make_config, GEO_FILES, GEO_ASSETS
from downloads import fetch
from geodata import atomic_link, owned_version, validate_geodata, write_durable
from policies import validate_runtime, load_policy, local_sources

REPOSITORIES = {"mihomo": "MetaCubeX/mihomo", "zashboard": "Zephyruso/zashboard", "geodata": "MetaCubeX/meta-rules-dat"}


class Updates:
    def __init__(self, controller):
        self.controller = controller
        self.path = controller.data / "software.json"
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        if not isinstance(self.state, dict) or any(not isinstance(value, dict) for value in self.state.values()):
            raise ValueError("软件状态文件格式不正确")
        self.status = {"phase": "idle", "message": ""}
        self.pending = None
        self.reserved = False
        self.component = None
        self.proxy = None
        versions_file = Path(__file__).with_name("versions.json")
        self.bundled = json.loads(versions_file.read_text(encoding="utf-8")) if versions_file.exists() else {}
        geo = controller.data / "geo-current"
        if (geo / "manifest.json").is_file():
            metadata = json.loads((geo / "manifest.json").read_text(encoding="utf-8"))
            self.bundled["geodata"] = metadata.get("version")
            confirmed = self.state.get("geodata")
            if confirmed and Path(confirmed.get("path", "")).resolve() != geo.resolve():
                # Startup selected a valid previous dataset. Persist the same
                # selection so the displayed version and next recovery agree.
                self.state["geodata"] = {"version": metadata["version"], "path": str(geo.resolve()),
                                         "assets": metadata["sha256"], "source": metadata.get("source", "")}
                with controller.lock:
                    atomic_json(self.path, self.state)
        binary = self.state.get("mihomo", {}).get("path")
        if binary and Path(binary).is_file() and self.valid_path(binary):
            controller.binary = binary
        selected = self.state.get("zashboard", {}).get("path")
        if selected and owned_version(controller.data, selected, "ui-versions"):
            atomic_link(controller.data / "ui-current", selected)
        elif (controller.data / "ui/index.html").is_file():
            atomic_link(controller.data / "ui-current", controller.data / "ui")

    def valid_path(self, binary):
        return Path(binary).resolve().parent == (self.controller.data / "bin").resolve()

    def snapshot(self):
        with self.controller.lock:
            return {"installed": {**self.bundled, **{key: value.get("version") for key, value in self.state.items()}}, **self.status}

    def reserve(self, component):
        if not isinstance(component, str) or component not in REPOSITORIES:
            raise ValueError("未知更新组件")
        with self.controller.lock:
            if self.reserved or self.pending or self.controller.import_lock.locked():
                raise ValueError("已有配置或软件更新正在进行")
            self.reserved = True
            self.component = component
            self.status = {"phase": "downloading", "message": "下载并验证期间继续使用当前代理和规则。"}

    def downloading(self, url, policy, limit, **kwargs):
        if policy == "proxy" and not self.proxy:
            raise ValueError("没有可用的已配置代理，请先导入节点或选择直连")
        method = policy if self.proxy else "direct"
        return fetch(url, method, limit, **kwargs, **({"proxy": self.proxy} if self.proxy else {}))

    def release(self, component, policy):
        repository = REPOSITORIES[component]
        raw, _ = self.downloading(f"https://api.github.com/repos/{repository}/releases/latest", policy, 2 * 1024 * 1024)
        release = json.loads(raw)
        if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
            raise ValueError("上游未返回可验证的公开发行版")
        version = str(release.get("tag_name", "")).removeprefix("v")
        if component != "geodata" and not re.fullmatch(r"\d+\.\d+\.\d+", version):
            raise ValueError("上游未返回稳定版")
        if component == "geodata":
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", str(release.get("tag_name", ""))):
                raise ValueError("规则资料版本标识不正确")
            return release
        name = f"mihomo-linux-amd64-compatible-v{version}.gz" if component == "mihomo" else "dist.zip"
        asset = self.asset(release, repository, "v" + version, name)
        return version, asset["browser_download_url"], asset["digest"].split(":", 1)[1]

    def asset(self, release, repository, tag, name):
        items = release.get("assets")
        if not isinstance(items, list):
            raise ValueError("发行文件列表格式不正确")
        assets = [asset for asset in items if isinstance(asset, dict) and asset.get("name") == name]
        expected_url = f"https://github.com/{repository}/releases/download/{tag}/{name}"
        if len(assets) != 1 or assets[0].get("browser_download_url") != expected_url or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", assets[0].get("digest") or ""
        ):
            raise ValueError("上游文件缺少匹配的 URL 和 SHA256")
        return assets[0]

    def install(self, component, *, reserved=False):
        if not reserved:
            self.reserve(component)
        stage = None
        switching = False
        try:
            with self.controller.lock:
                if not self.reserved or self.component != component:
                    raise ValueError("更新操作未预留")
                setting = "geodata_download" if component == "geodata" else "update_download"
                policy = self.controller.profile.get(setting, "auto")
            with ExitStack() as stack:
                self.proxy = None
                if policy != "direct" and (self.controller.data / "nodes.yaml").exists():
                    try:
                        self.proxy = stack.enter_context(self.controller.configured_proxy())
                    except (OSError, ValueError):
                        if policy == "proxy":
                            raise
                if component == "geodata":
                    result = self.install_geodata(policy)
                    switching = True
                    return result
                version, url, expected = self.release(component, policy)
                raw, method = self.downloading(url, policy, 100 * 1024 * 1024, timeout=180)
                if hashlib.sha256(raw).hexdigest() != expected:
                    raise ValueError("文件 SHA256 校验失败，已保留当前版本")
                with self.controller.lock:
                    self.status = {"phase": "validating", "message": f"正在验证 {component} {version}。"}
                if component == "mihomo":
                    folder = self.controller.data / "bin"
                    folder.mkdir(exist_ok=True)
                    descriptor, filename = tempfile.mkstemp(prefix="mihomo-incoming-", dir=folder)
                    os.close(descriptor)
                    stage = Path(filename)
                    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as source, stage.open("wb") as target:
                        total = 0
                        while block := source.read(1024 * 1024):
                            total += len(block)
                            if total > 250 * 1024 * 1024:
                                raise ValueError("Mihomo 文件过大")
                            target.write(block)
                        target.flush()
                        os.fsync(target.fileno())
                    if stage.read_bytes()[:4] != b"\x7fELF":
                        raise ValueError("上游文件不是 Linux 程序")
                    os.chmod(stage, 0o700)
                    result = subprocess.run([str(stage), "-v"], capture_output=True, timeout=30)
                    if result.returncode or f"v{version}".encode() not in result.stdout:
                        raise ValueError("Mihomo 版本验证失败")
                    with self.controller.lock:
                        config = make_config(self.controller.profile, self.controller.secret, self.controller.data)
                        empty = load_policy(self.controller.data / "nodes.yaml").get("_empty_rule_providers", ()) if (self.controller.data / "nodes.yaml").exists() else ()
                    descriptor, filename = tempfile.mkstemp(prefix="update-check-", suffix=".json", dir=self.controller.data)
                    os.close(descriptor)
                    check = Path(filename)
                    try:
                        atomic_json(check, local_sources(config))
                        result = subprocess.run([str(stage), "-t", "-d", str(self.controller.data), "-f", str(check)], capture_output=True, timeout=60)
                        if result.returncode:
                            raise ValueError("新版本不兼容当前配置，已保留旧版本")
                        validate_runtime(str(stage), self.controller.data, config, empty_rules=empty)
                    finally:
                        check.unlink(missing_ok=True)
                    destination = folder / f"mihomo-{version}-{expected[:12]}"
                    os.replace(stage, destination)
                    stage = None
                    with self.controller.lock:
                        self.pending = {"id": uuid.uuid4().hex, "component": component, "previous_binary": self.controller.binary,
                                        "previous_state": copy.deepcopy(self.state), "version": version,
                                        "path": str(destination), "sha256": expected}
                        self.controller.binary = str(destination)
                        self.status = {"phase": "switching", "message": f"切换至 Mihomo {version}，启动异常时回退。"}
                    switching = True
                    return "reload"
                stage = Path(tempfile.mkdtemp(prefix="ui-incoming-", dir=self.controller.data))
                with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                    entries = [name for name in archive.namelist() if name.endswith("index.html") and name.count("/") <= 1]
                    if len(entries) != 1:
                        raise ValueError("Zashboard 文件结构不正确")
                    prefix = entries[0].removesuffix("index.html")
                    total = 0
                    for entry in archive.infolist():
                        if entry.is_dir() or not entry.filename.startswith(prefix):
                            continue
                        relative = Path(entry.filename[len(prefix):])
                        total += entry.file_size
                        if total > 150 * 1024 * 1024 or relative.is_absolute() or ".." in relative.parts or "\\" in entry.filename:
                            raise ValueError("Zashboard 文件结构不正确")
                        output = stage / relative
                        output.parent.mkdir(parents=True, exist_ok=True)
                        write_durable(output, archive.read(entry))
                versions = self.controller.data / "ui-versions"
                versions.mkdir(exist_ok=True)
                destination = versions / (version + "-" + expected[:20])
                if destination.exists():
                    destination = versions / (version + "-" + expected[:20] + "-" + uuid.uuid4().hex[:8])
                os.replace(stage, destination)
                stage = None
                with self.controller.lock:
                    previous = copy.deepcopy(self.state)
                    old_target = (self.controller.data / "ui-current").resolve()
                    proposed = {**self.state, component: {"version": version, "sha256": expected, "path": str(destination),
                                                         "previous_path": str(old_target)}}
                    try:
                        atomic_json(self.path, proposed)
                        atomic_link(self.controller.data / "ui-current", destination)
                    except OSError:
                        atomic_json(self.path, previous)
                        atomic_link(self.controller.data / "ui-current", old_target)
                        raise
                    self.state = proposed
                    self.status = {"phase": "complete", "message": f"Zashboard 已通过 {method} 更新至 {version}。"}
                return "complete"
        except Exception:
            with self.controller.lock:
                self.status = {"phase": "failed", "message": "下载或验证失败，原版本和规则已保留。"}
            raise
        finally:
            self.proxy = None
            if stage is not None and stage.exists():
                shutil.rmtree(stage) if stage.is_dir() else stage.unlink()
            if not switching:
                with self.controller.lock:
                    self.reserved = False
                    self.component = None

    def install_geodata(self, policy):
        release = self.release("geodata", policy)
        versions = self.controller.data / "geodata-versions"
        versions.mkdir(exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix="incoming-", dir=versions))
        installed = False
        try:
            hashes = {}
            last_method = "direct"
            for key, name in GEO_FILES.items():
                asset = self.asset(release, REPOSITORIES["geodata"], release["tag_name"], GEO_ASSETS[key])
                raw, last_method = self.downloading(asset["browser_download_url"], policy, 64 * 1024 * 1024, timeout=180)
                expected = asset["digest"].split(":", 1)[1]
                if hashlib.sha256(raw).hexdigest() != expected:
                    raise ValueError("规则资料 SHA256 校验失败")
                write_durable(stage / name, raw)
                hashes[name] = expected
            version = str(release.get("published_at") or release["tag_name"])
            identity = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
            metadata = {"version": version, "sha256": hashes, "source": "https://github.com/MetaCubeX/meta-rules-dat"}
            atomic_json(stage / "manifest.json", metadata)
            with self.controller.lock:
                self.status = {"phase": "validating", "message": "正在用 Mihomo 验证 GEOIP/GEOSITE 和当前规则。"}
                config = local_sources(make_config(self.controller.profile, self.controller.secret, self.controller.data))
            validate_geodata(self.controller.binary, stage, config)
            destination = versions / (identity[:24] + "-" + uuid.uuid4().hex[:8])
            os.replace(stage, destination)
            installed = True
            with self.controller.lock:
                previous = (self.controller.data / "geo-current").resolve()
                pending = {"id": uuid.uuid4().hex, "component": "geodata", "previous_state": copy.deepcopy(self.state),
                                "previous_geo": str(previous), "version": version, "path": str(destination),
                                "previous_path": str(previous), "sha256": identity, "assets": hashes, "source": metadata["source"]}
                atomic_link(self.controller.data / "geo-current", destination)
                self.pending = pending
                self.status = {"phase": "switching", "message": f"GEOIP/GEOSITE 已通过 {last_method} 验证，正在重载；异常时回退。"}
            return "reload"
        finally:
            if not installed and stage.exists():
                shutil.rmtree(stage)

    def confirm(self, *, running_binary=None, running_update=None):
        with self.controller.lock:
            if not self.pending:
                return False
            if running_update != self.pending["id"]:
                return False
            component = self.pending["component"]
            if component == "mihomo" and running_binary != self.pending["path"]:
                return False
            if component == "geodata" and (self.controller.data / "geo-current").resolve() != Path(self.pending["path"]).resolve():
                return False
            keys = ("version", "path", "sha256") if component == "mihomo" else ("version", "path", "sha256", "assets", "source", "previous_path")
            proposed = {**self.state, component: {key: self.pending[key] for key in keys}}
            atomic_json(self.path, proposed)
            self.state = proposed
            self.status = {"phase": "complete", "message": f'{component} 已更新至 {self.pending["version"]}。'}
            self.pending = None
            self.reserved = False
            self.component = None
            return True

    def rollback(self):
        with self.controller.lock:
            if not self.pending:
                return False
            if self.pending["component"] == "mihomo":
                self.controller.binary = self.pending["previous_binary"]
            else:
                atomic_link(self.controller.data / "geo-current", self.pending["previous_geo"])
            self.state = self.pending["previous_state"]
            atomic_json(self.path, self.state)
            self.pending = None
            self.reserved = False
            self.component = None
            self.status = {"phase": "failed", "message": "新程序或规则资料重载失败，已恢复原版本。"}
            return True
