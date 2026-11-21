"""Create an app-only Supervisor backup with a complete prebuilt image."""
import copy
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import tarfile
import tempfile

from common import ROOT, WORK, DIST, DEFINITION, VERSION, APP_SLUG, IMAGE, REPOSITORY, REPOSITORY_ID, BUNDLE_NAME, digest, run, save
from image_identity import saved_image_identity


def add_bytes(archive, name, content):
    info = tarfile.TarInfo(name)
    info.size = len(content)
    info.mode = 0o600
    archive.addfile(info, io.BytesIO(content))


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def write_backup(path, image_path, expected_image, supervisor_version, revision):
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Installer needs the exact source commit")
    with Path(image_path).open("rb") as source:
        if saved_image_identity(source, IMAGE) != expected_image:
            raise ValueError("Installer app image content changed")
    if expected_image["architecture"] != "amd64" or expected_image["os"] != "linux":
        raise ValueError("Installer image is not Linux amd64")
    slug = hashlib.sha256((revision + expected_image["config_sha256"]).encode()).hexdigest()[:8]
    system = {**copy.deepcopy(DEFINITION), "repository": REPOSITORY_ID, "translations": {}}
    if system["options"].get("controller_secret") or system["options"].get("subscription_url"):
        raise ValueError("Distributed app defaults must not contain runtime credentials or subscriptions")
    app = {
        "version": VERSION, "state": "started", "system": system,
        "user": {"version": VERSION, "image": IMAGE.rsplit(":", 1)[0], "options": {},
                 "boot": "auto", "watchdog": True, "ingress_panel": True, "protected": True},
    }
    metadata = {
        "version": 2, "slug": slug, "type": "partial", "name": "Mihomo + Zashboard installer",
        "date": datetime.now(timezone.utc).isoformat(), "supervisor_version": supervisor_version,
        "compressed": True, "protected": False, "homeassistant": None, "folders": [],
        "addons": [{"slug": APP_SLUG, "name": DEFINITION["name"], "version": VERSION, "size": 0}],
        "repositories": [], "extra": {"source": REPOSITORY, "revision": revision},
    }
    profile = (ROOT / "mihomo/apparmor.txt").read_bytes().replace(b"profile hassio-addon ", f"profile {APP_SLUG} ".encode(), 1)
    with tempfile.TemporaryDirectory() as temporary:
        nested = Path(temporary) / (APP_SLUG + ".tar.gz")
        with tarfile.open(nested, "w:gz") as archive:
            add_bytes(archive, "./addon.json", json_bytes(app))
            add_bytes(archive, "./apparmor.txt", profile)
            archive.add(image_path, arcname="./image.tar")
        with tarfile.open(path, "w:") as archive:
            add_bytes(archive, "./backup.json", json_bytes(metadata))
            archive.add(nested, arcname="./" + APP_SLUG + ".tar.gz")
    return slug


def build_installer(supervisor_version, revision):
    staging = WORK / "installer"
    staging.mkdir(parents=True, exist_ok=False)
    image_path = WORK / "mihomo-image.tar"
    with image_path.open("rb") as source:
        expected = saved_image_identity(source, IMAGE)
    slug = write_backup(staging / "mihomo-backup.tar", image_path, expected, supervisor_version, revision)
    (staging / "backup-slug.txt").write_text(slug + "\n", encoding="utf-8")
    shutil.copy2(ROOT / "install.sh", staging / "install.sh")
    repository = staging / "repository"
    # A clean local clone has no checkout authentication headers. It is later
    # loaded and owned by stock Supervisor as the cache for this public URL.
    run("git", "-c", f"safe.directory={ROOT}", "clone", "--no-hardlinks", ROOT, repository)
    run("git", "-C", repository, "remote", "set-url", "origin", REPOSITORY)
    run("git", "-C", repository, "checkout", "-B", "main", revision)
    configuration = (repository / ".git/config").read_text(encoding="utf-8").lower()
    if "extraheader" in configuration or "credential" in configuration:
        raise ValueError("Offline repository cache unexpectedly contains authentication settings")
    files = sorted(path for path in staging.rglob("*") if path.is_file())
    (staging / "SHA256SUMS").write_text(
        "".join(f"{digest(path)}  {path.relative_to(staging).as_posix()}\n" for path in files), encoding="utf-8")
    DIST.mkdir(exist_ok=True)
    destination = DIST / BUNDLE_NAME
    with tarfile.open(destination, "w:gz") as archive:
        archive.add(staging, arcname="haos-mihomo-installer")
    save(DIST / "build-report.json", {"image": IMAGE, "image_content": expected, "revision": revision,
                                     "app_slug": APP_SLUG, "backup_slug": slug, "supervisor_version": supervisor_version,
                                     "repository_cache_has_no_credentials": True,
                                     "backup_contains_only_app": True})
    return destination
