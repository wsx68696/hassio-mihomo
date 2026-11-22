#!/usr/bin/env python3
"""Fetch fixed app assets. Official HAOS files are used only by the test VM."""
import argparse
import gzip
import os
from pathlib import Path
import re
import shutil
import zipfile

from common import ROOT, WORK, DIST, LOCK, download, run, save


def app_assets():
    WORK.mkdir(exist_ok=True)
    DIST.mkdir(exist_ok=True)
    vendor = ROOT / "mihomo/vendor"
    vendor.mkdir(parents=True, exist_ok=True)
    mihomo = LOCK["mihomo"]
    archive = download(f'https://github.com/MetaCubeX/mihomo/releases/download/v{mihomo["version"]}/{mihomo["asset"]}',
                       WORK / mihomo["asset"], mihomo["sha256"])
    with gzip.open(archive, "rb") as source, (vendor / "mihomo").open("wb") as destination:
        shutil.copyfileobj(source, destination)
    os.chmod(vendor / "mihomo", 0o755)
    run(vendor / "mihomo", "-v")
    ui = LOCK["zashboard"]
    archive = download(f'https://github.com/Zephyruso/zashboard/releases/download/v{ui["version"]}/dist.zip',
                       WORK / "zashboard.zip", ui["sha256"])
    with zipfile.ZipFile(archive) as bundle:
        entries = [name for name in bundle.namelist() if name.endswith("index.html") and name.count("/") <= 1]
        if len(entries) != 1:
            raise ValueError("Unexpected Zashboard archive entry point")
        prefix = entries[0].removesuffix("index.html")
        for entry in bundle.infolist():
            if entry.is_dir() or not entry.filename.startswith(prefix):
                continue
            relative = Path(entry.filename[len(prefix):])
            if relative.is_absolute() or ".." in relative.parts or "\\" in entry.filename:
                raise ValueError("Unexpected Zashboard archive path")
            destination = vendor / "ui" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(bundle.read(entry))
    geo = vendor / "geodata"
    geo.mkdir(exist_ok=True)
    hashes = {}
    commit = LOCK["geodata"].get("source_commit", "")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Geo build inputs require an immutable source commit")
    for asset in LOCK["geodata"]["assets"]:
        if asset["url"] != f'https://raw.githubusercontent.com/MetaCubeX/meta-rules-dat/{commit}/{asset["asset"]}':
            raise ValueError("Geo build URL is not bound to the pinned source commit")
        target = download(asset["url"], WORK / ("geo-" + asset["name"]), asset["sha256"])
        shutil.copy2(target, geo / asset["name"])
        hashes[asset["name"]] = asset["sha256"]
    save(geo / "manifest.json", {"version": LOCK["geodata"]["published_at"], "sha256": hashes, "source_commit": commit,
                                 "source": "https://github.com/MetaCubeX/meta-rules-dat"})
    save(vendor / "versions.json", {"mihomo": mihomo["version"], "zashboard": ui["version"]})
    source = WORK / "mihomo-source"
    if not source.exists():
        run("git", "clone", "--depth", "1", "--branch", f'v{mihomo["version"]}', "https://github.com/MetaCubeX/mihomo.git", source)
    commit = run("git", "-C", source, "rev-parse", "HEAD", capture_output=True, text=True).stdout.strip()
    if commit != mihomo["commit"]:
        raise ValueError("The upstream Mihomo source tag changed")
    if run("git", "-C", source, "status", "--porcelain", capture_output=True, text=True).stdout:
        raise ValueError("The upstream source checkout contains local changes")
    run("git", "-C", source, "archive", "--format=tar.gz", f'--prefix=mihomo-{mihomo["version"]}/',
        "--output", DIST / f'mihomo-{mihomo["version"]}-source.tar.gz', "HEAD")


def test_assets():
    official = LOCK["test_haos"]
    base = f'https://github.com/home-assistant/operating-system/releases/download/{official["version"]}/'
    for filename, key, destination in [
        (official["image"], "image_sha256", "official.qcow2.xz"),
        (official["ota"], "ota_sha256", "official.raucb"),
        ("SHA256SUMS", "checksums_sha256", "SHA256SUMS.official"),
        ("SHA256SUMS.sigstore.json", "signature_sha256", "SHA256SUMS.sigstore.json"),
    ]:
        download(base + filename, WORK / destination, official[key])
    sums = (WORK / "SHA256SUMS.official").read_text(encoding="utf-8").splitlines()
    for filename, key in [(official["image"], "image_sha256"), (official["ota"], "ota_sha256")]:
        if f'{official[key]}  {filename}' not in sums:
            raise ValueError("Official test assets differ from the signed checksum list")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-haos", action="store_true")
    args = parser.parse_args()
    test_assets() if args.test_haos else app_assets()
