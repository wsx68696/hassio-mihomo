"""Shared build and verification settings for the public app repository."""
import hashlib
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / ".work"
DIST = ROOT / "dist"
LOCK = json.loads((ROOT / "sources.lock.json").read_text(encoding="utf-8"))
DEFINITION = json.loads((ROOT / "mihomo/config.json").read_text(encoding="utf-8"))
VERSION = DEFINITION["version"]
REPOSITORY = "https://github.com/wsx68696/hassio-mihomo"
REPOSITORY_ID = hashlib.sha1(REPOSITORY.lower().encode()).hexdigest()[:8]
APP_SLUG = REPOSITORY_ID + "_mihomo"
IMAGE = DEFINITION["image"].format(arch="amd64") + ":" + VERSION
BUNDLE_NAME = "mihomo-installer-amd64.tar.gz"


def run(*args, **kwargs):
    return subprocess.run([str(value) for value in args], check=True, **kwargs)


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as source:
        while block := source.read(1024 * 1024):
            value.update(block)
    return value.hexdigest()


def download(url, path, expected):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    run("curl", "--fail", "--location", "--retry", "4", "--retry-all-errors",
        "--connect-timeout", "20", "--max-time", "1200", "--output", path, url)
    if digest(path) != expected:
        path.unlink()
        raise ValueError("Downloaded asset SHA256 differs from the pinned input")
    return path
