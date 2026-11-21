"""Update only this app's own binaries and dashboard in persistent /data."""
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
import zipfile

from config import atomic_json, make_config
from downloads import fetch

REPOSITORIES = {"mihomo": "MetaCubeX/mihomo", "zashboard": "Zephyruso/zashboard"}


class Updates:
    def __init__(self, controller):
        self.controller = controller
        self.path = controller.data / "software.json"
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        self.status = {"phase": "idle", "message": ""}
        self.pending = None
        binary = self.state.get("mihomo", {}).get("path")
        if binary and Path(binary).is_file() and self.valid_path(binary):
            controller.binary = binary

    def valid_path(self, binary):
        return Path(binary).resolve().parent == (self.controller.data / "bin").resolve()

    def snapshot(self):
        return {"installed": {key: value.get("version") for key, value in self.state.items()}, **self.status}

    def release(self, component, policy):
        repository = REPOSITORIES[component]
        raw, _ = fetch(f"https://api.github.com/repos/{repository}/releases/latest", policy, 2 * 1024 * 1024)
        release = json.loads(raw)
        version = release.get("tag_name", "").removeprefix("v")
        if release.get("draft") or release.get("prerelease") or not re.fullmatch(r"\d+\.\d+\.\d+", version):
            raise ValueError("上游未返回可验证的稳定版")
        name = f"mihomo-linux-amd64-compatible-v{version}.gz" if component == "mihomo" else "dist.zip"
        assets = [asset for asset in release.get("assets", []) if asset.get("name") == name]
        if len(assets) != 1:
            raise ValueError("未找到通用 x86-64 对应的上游文件")
        asset = assets[0]
        expected_url = f"https://github.com/{repository}/releases/download/v{version}/{name}"
        if asset.get("browser_download_url") != expected_url or not re.fullmatch(r"sha256:[0-9a-f]{64}", asset.get("digest") or ""):
            raise ValueError("上游文件缺少 SHA256，已保留当前版本")
        return version, expected_url, asset["digest"].split(":", 1)[1]

    def install(self, component):
        if component not in REPOSITORIES:
            raise ValueError("未知更新组件")
        with self.controller.lock:
            if self.status["phase"] in {"downloading", "validating", "switching"}:
                raise ValueError("已有更新正在进行")
            self.status = {"phase": "downloading", "message": "通过现有网络下载并校验，当前代理继续运行。"}
            policy = self.controller.profile.get("update_download", "auto")
        stage = None
        try:
            version, url, expected = self.release(component, policy)
            raw, method = fetch(url, policy, 100 * 1024 * 1024, timeout=180)
            if hashlib.sha256(raw).hexdigest() != expected:
                raise ValueError("文件 SHA256 校验失败，已保留当前版本")
            self.status = {"phase": "validating", "message": f"正在校验 {component} {version}。"}
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
                with stage.open("rb") as source:
                    if source.read(4) != b"\x7fELF":
                        raise ValueError("上游文件不是 Linux 程序")
                os.chmod(stage, 0o700)
                result = subprocess.run([str(stage), "-v"], capture_output=True, timeout=30)
                if result.returncode or f"v{version}".encode() not in result.stdout:
                    raise ValueError("Mihomo 版本验证失败")
                check = self.controller.data / "update-check.json"
                try:
                    with self.controller.lock:
                        atomic_json(check, make_config(self.controller.profile, self.controller.secret, self.controller.data))
                    result = subprocess.run([str(stage), "-t", "-d", str(self.controller.data), "-f", str(check)], capture_output=True, timeout=30)
                    if result.returncode:
                        raise ValueError("新版本不兼容当前配置，已保留旧版本")
                finally:
                    check.unlink(missing_ok=True)
                destination = folder / f"mihomo-{version}-{expected[:12]}"
                os.replace(stage, destination)
                stage = None
                with self.controller.lock:
                    self.pending = {"previous_binary": self.controller.binary, "previous_state": dict(self.state),
                                    "version": version, "path": str(destination), "sha256": expected}
                    self.controller.binary = str(destination)
                    self.status = {"phase": "switching", "message": f"正在切换至 Mihomo {version}；异常时自动回退。"}
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
                    output.write_bytes(archive.read(entry))
            destination = self.controller.data / "ui"
            previous = self.controller.data / "ui-previous"
            if previous.exists():
                shutil.rmtree(previous)
            destination.rename(previous)
            try:
                stage.rename(destination)
                stage = None
            except OSError:
                previous.rename(destination)
                raise
            self.state[component] = {"version": version, "sha256": expected}
            atomic_json(self.path, self.state)
            self.status = {"phase": "complete", "message": f"Zashboard 已通过 {method} 更新至 {version}。刷新面板生效。"}
            return "complete"
        except Exception:
            self.status = {"phase": "failed", "message": "更新下载或校验失败，当前版本已保留。"}
            raise
        finally:
            if stage and stage.exists():
                shutil.rmtree(stage) if stage.is_dir() else stage.unlink()

    def confirm(self):
        if self.pending:
            self.state["mihomo"] = {key: self.pending[key] for key in ("version", "path", "sha256")}
            atomic_json(self.path, self.state)
            self.status = {"phase": "complete", "message": f'Mihomo 已更新至 {self.pending["version"]}。'}
            self.pending = None

    def rollback(self):
        if not self.pending:
            return False
        self.controller.binary = self.pending["previous_binary"]
        self.state = self.pending["previous_state"]
        atomic_json(self.path, self.state)
        self.pending = None
        self.status = {"phase": "failed", "message": "新版本启动失败，已回退到原版本。"}
        return True
