"""Keep each complete, validated Geo dataset behind one atomic pointer."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid

from config import atomic_json, GEO_FILES


def file_digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as source:
        while block := source.read(1024 * 1024):
            value.update(block)
    return value.hexdigest()


def atomic_link(path, target):
    path, target = Path(path), Path(target)
    if path.exists() and path.is_dir() and not path.is_symlink():
        raise ValueError("Managed update pointer is unexpectedly a directory")
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
    try:
        temporary.symlink_to(os.path.relpath(target, path.parent), target_is_directory=target.is_dir())
        os.replace(temporary, path)
        if os.name == "posix":
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def owned_version(data, path, category):
    if not isinstance(path, (str, os.PathLike)) or not str(path):
        return False
    root = (Path(data) / category).resolve()
    candidate = Path(path).resolve()
    return candidate.parent == root and candidate.is_dir()


def write_durable(path, content):
    with Path(path).open("wb") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())


def valid_dataset(path):
    try:
        path = Path(path)
        metadata = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        return (set(metadata["sha256"]) == set(GEO_FILES.values()) and
                all(file_digest(path / name) == expected for name, expected in metadata["sha256"].items()))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def prepare_geodata(data, bundle, confirmed=None):
    data, bundle = Path(data), Path(bundle)
    metadata = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    for name, expected in metadata["sha256"].items():
        if file_digest(bundle / name) != expected:
            raise ValueError("Bundled Geo dataset checksum mismatch")
    identifier = hashlib.sha256(json.dumps(metadata["sha256"], sort_keys=True).encode()).hexdigest()[:20]
    versions = data / "geodata-versions"
    versions.mkdir(exist_ok=True)
    default = versions / ("bundle-" + identifier)
    if not valid_dataset(default):
        if default.exists():
            default = versions / ("bundle-" + identifier + "-" + uuid.uuid4().hex[:8])
        staging = Path(tempfile.mkdtemp(prefix="bundle-incoming-", dir=versions))
        try:
            for name in GEO_FILES.values():
                shutil.copy2(bundle / name, staging / name)
            shutil.copy2(bundle / "manifest.json", staging / "manifest.json")
            os.replace(staging, default)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    selected = default
    if isinstance(confirmed, dict):
        for candidate in (confirmed.get("path"), confirmed.get("previous_path")):
            if owned_version(data, candidate, "geodata-versions") and valid_dataset(candidate):
                selected = Path(candidate)
                break
    elif not (data / "geo-current").is_symlink():
        existing = [name for name in GEO_FILES.values() if (data / name).is_file() and not (data / name).is_symlink()]
        if existing:
            selected = Path(tempfile.mkdtemp(prefix="legacy-", dir=versions))
            for name in GEO_FILES.values():
                shutil.copy2(data / name if name in existing else default / name, selected / name)
            atomic_json(selected / "manifest.json", {"version": "legacy", "source": "existing device data",
                        "sha256": {name: file_digest(selected / name) for name in GEO_FILES.values()}})
    for name in GEO_FILES.values():
        if not (selected / name).is_file():
            raise ValueError("The confirmed Geo dataset is incomplete")
    # Unconfirmed runtime switches are recovered from the last committed state.
    atomic_link(data / "geo-current", selected)
    for name in GEO_FILES.values():
        atomic_link(data / name, data / "geo-current" / name)
    return str(selected)


def validate_geodata(binary, candidate, config):
    candidate = Path(candidate)
    for name in ("Country.mmdb", "ASN.mmdb"):
        with (candidate / name).open("rb") as source:
            source.seek(max(0, (candidate / name).stat().st_size - 128 * 1024))
            if b"\xab\xcd\xefMaxMind.com" not in source.read():
                raise ValueError("Geo MMDB 文件格式不正确")
    # Validate both datasets even when the user's current rules do not use them.
    probe = {**config, "geo-auto-update": False, "geodata-mode": True, "external-ui": "",
             "rules": ["GEOIP,CN,DIRECT", "GEOSITE,cn,DIRECT", *config.get("rules", [])]}
    # Provider caches are managed inside /data. Validation homes must own a
    # copy rather than depend on Mihomo allowing paths outside its home.
    providers = candidate / "validation-providers"
    localize_providers(probe, candidate.parent.parent, providers)
    filename = candidate / "validation.json"
    try:
        atomic_json(filename, probe)
        result = subprocess.run([binary, "-t", "-d", str(candidate), "-f", str(filename)], capture_output=True, timeout=60)
        if result.returncode:
            raise ValueError("GEOIP/GEOSITE 未通过 Mihomo 及当前规则校验")
        probe["geodata-mode"] = False
        probe["rules"] = ["GEOIP,CN,DIRECT", "IP-ASN,4134,DIRECT", *config.get("rules", [])]
        atomic_json(filename, probe)
        result = subprocess.run([binary, "-t", "-d", str(candidate), "-f", str(filename)], capture_output=True, timeout=60)
        if result.returncode:
            raise ValueError("Country/ASN 数据未通过 Mihomo 校验")
    finally:
        filename.unlink(missing_ok=True)
        if providers.exists():
            shutil.rmtree(providers)


def localize_providers(config, data, destination):
    data, destination = Path(data).resolve(), Path(destination)
    for kind in ("proxy-providers", "rule-providers"):
        for name, spec in config.get(kind, {}).items():
            if spec.get("type") != "file":
                continue
            source = Path(spec.get("path", ""))
            if not source.is_absolute():
                source = data / source
            source = source.resolve()
            if not source.is_relative_to(data) or not source.is_file() or source.stat().st_size > 32 * 1024 * 1024:
                raise ValueError("验证 provider 缓存路径或大小不正确")
            destination.mkdir(parents=True, exist_ok=True)
            target = destination / (kind + "-" + hashlib.sha256(name.encode()).hexdigest()[:20] + source.suffix)
            shutil.copy2(source, target)
            spec["path"] = str(target)
