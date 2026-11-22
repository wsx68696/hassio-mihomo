"""Bind immutable release assets and the image to one fully verified commit."""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import shutil
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

from common import ROOT, WORK, DIST, VERSION, BUNDLE_NAME, IMAGE, APP_SLUG, digest, run

REPOSITORY = "wsx68696/hassio-mihomo"
TAG = "v" + VERSION
NETWORK_REQUIRED = (
    "local_output_proxied", "forwarded_container_output_proxied", "lan_stays_direct", "lan_traverses_router",
    "dns_hijack", "dashboard_available", "api_requires_secret", "invalid_api_returns_json",
    "dashboard_choices_survive_restart", "subscription_updated_through_existing_proxy",
    "invalid_nodes_preserve_working_cache", "invalid_rules_preserve_working_cache", "direct_fallback_bypasses_tun",
    "domain_rule_matches", "rule_provider_matches", "geosite_matches", "geoip_matches", "match_direct_fallback",
    "provider_download_through_existing_proxy", "child_sigkill_recovers", "addon_sigkill_recovers_stale_nft",
    "normal_stop_cleans_network", "unrelated_network_objects_preserved", "yaml_policy_import",
    "inline_rule_provider_matches", "file_rule_provider_matches", "mrs_rule_provider_matches", "http_proxy_provider_loaded")
VM_REQUIRED = (
    "core_onboarding_before_install", "core_onboarding_after_install", "core_onboarding_reachable_through_qemu",
    "installed_with_public_egress_blocked", "core_and_supervisor_containers_unchanged", "registered_public_update_repository",
    "protected_mode_and_auto_start", "tun_and_apparmor_on_stock_haos", "supervisor_traffic_proxied", "core_traffic_proxied",
    "haos_traffic_proxied", "repeat_install_preserves_configuration", "registered_unhealthy_app_repaired",
    "installer_lock_survives_process_death", "mihomo_update_through_existing_proxy", "geodata_update_through_existing_proxy",
    "proxy_update_works_in_direct_mode", "official_ota_preserves_app_and_configuration", "official_base_image_unchanged",
    "no_preloaded_or_overridden_core")


def revision():
    value = os.environ.get("GITHUB_SHA", "")
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError("The exact GitHub source commit is required")
    return value


def read_object(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Verification report must be an object")
    return value


def verify_reports(folder, expected_revision=None):
    expected_revision = expected_revision or revision()
    build = read_object(folder / "build-report.json")
    network = read_object(folder / "network-test.json")
    vm = read_object(folder / "vm-test.json")
    if (build.get("revision") != expected_revision or build.get("version") != VERSION
            or build.get("image") != IMAGE or build.get("app_slug") != APP_SLUG):
        raise ValueError("Build evidence does not belong to this source commit, version and image")
    identity = build.get("image_content", {})
    if (not isinstance(identity, dict) or identity.get("architecture") != "amd64" or identity.get("os") != "linux"
            or not re.fullmatch(r"[0-9a-f]{64}", identity.get("config_sha256", ""))
            or identity.get("labels", {}).get("org.opencontainers.image.revision") != expected_revision
            or identity.get("labels", {}).get("io.hass.version") != VERSION):
        raise ValueError("Image content is not bound to the verified revision and version")
    if build.get("backup_contains_only_app") is not True or build.get("repository_cache_has_no_credentials") is not True:
        raise ValueError("Installer backup or repository cache verification is incomplete")
    if not all(network.get(key) is True for key in NETWORK_REQUIRED):
        raise ValueError("Required routing and recovery checks are missing or failed")
    if not all(vm.get(key) is True for key in VM_REQUIRED):
        raise ValueError("Installation on original HAOS has incomplete verification evidence")
    for name in ("before_install", "after_install", "after_ota"):
        phase = vm.get(name)
        if (not isinstance(phase, dict) or phase.get("supported") is not True or phase.get("healthy") is not True
                or phase.get("unsupported") != []):
            raise ValueError("Genuine stock Supervisor support or health verification is missing")
    if not (folder / BUNDLE_NAME).is_file():
        raise ValueError("The verified installer package is missing")
    if any(path.name.endswith((".img", ".img.xz", ".qcow2", ".qcow2.xz", ".raucb")) for path in folder.iterdir()):
        raise ValueError("HAOS test disks must not be distributed")
    return build


def render_installer(source, bundle_hash):
    if not re.fullmatch(r"[0-9a-f]{64}", bundle_hash) or source.count("__BUNDLE_SHA256__") != 1:
        raise ValueError("The installer must have exactly one bundle digest placeholder")
    if f"INSTALLER_VERSION={VERSION}\n" not in source:
        raise ValueError("The installer and app versions differ")
    return source.replace("__BUNDLE_SHA256__", bundle_hash)


def asset_files():
    return {path.name: path for path in DIST.iterdir() if path.is_file()
            and path.name not in {"SHA256SUMS", "SHA256SUMS.sigstore.json"}}


def prepare():
    shutil.copy2(WORK / "network-test.json", DIST / "network-test.json")
    verify_reports(DIST)
    (DIST / "install.sh").write_text(render_installer((ROOT / "install.sh").read_text(encoding="utf-8"),
                                                       digest(DIST / BUNDLE_NAME)), encoding="utf-8", newline="\n")
    shutil.copy2(WORK / "python-base.txt", DIST / "python-base.txt")
    files = asset_files()
    (DIST / "SHA256SUMS").write_text("".join(f"{digest(files[name])}  {name}\n" for name in sorted(files)), encoding="utf-8")


def verify_assets(*, require_signature=True):
    build = verify_reports(DIST)
    files = asset_files()
    checked = set()
    sums = DIST / "SHA256SUMS"
    for line in sums.read_text(encoding="utf-8").splitlines():
        expected, name = line.split("  ", 1)
        if name not in files or name in checked or digest(files[name]) != expected:
            raise ValueError("Release asset checksum verification failed")
        checked.add(name)
    if checked != set(files):
        raise ValueError("Some release assets are not covered by SHA256SUMS")
    if (DIST / "install.sh").read_text(encoding="utf-8") != render_installer(
            (ROOT / "install.sh").read_text(encoding="utf-8"), digest(DIST / BUNDLE_NAME)):
        raise ValueError("Release installer does not embed this verified package's digest")
    files[sums.name] = sums
    if require_signature:
        signature = DIST / "SHA256SUMS.sigstore.json"
        run("cosign", "verify-blob", "--bundle", signature, "--certificate-identity",
            "https://github.com/wsx68696/hassio-mihomo/.github/workflows/build.yml@refs/heads/main",
            "--certificate-oidc-issuer", "https://token.actions.githubusercontent.com", sums)
        files[signature.name] = signature
    return build, files


def api(path, method="GET", payload=None, *, missing=False):
    token = os.environ["GH_TOKEN"]
    request = Request("https://api.github.com" + path, method=method,
                      data=None if payload is None else json.dumps(payload).encode(), headers={
                          "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                          "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "haos-mihomo-release"})
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        if missing and error.code == 404:
            return None
        raise RuntimeError(f"GitHub release API failed with HTTP {error.code}") from None


def tag_revision():
    reference = api(f"/repos/{REPOSITORY}/git/ref/tags/{TAG}", missing=True)
    if not reference:
        return None
    value = reference["object"]
    for _ in range(4):
        if value["type"] == "commit":
            return value["sha"]
        if value["type"] != "tag":
            break
        value = api(f'/repos/{REPOSITORY}/git/tags/{value["sha"]}')["object"]
    raise ValueError("Release tag does not resolve to a commit")


def check_tag():
    current = tag_revision()
    if current is not None and current != revision():
        raise ValueError("This version is already bound to another commit; increment the app and installer version")
    return current


def get_release():
    release = api(f"/repos/{REPOSITORY}/releases/tags/{TAG}", missing=True)
    if release:
        return release
    # GitHub does not index draft releases through the tag endpoint. The
    # authenticated release listing includes drafts owned by this repository.
    releases = api(f"/repos/{REPOSITORY}/releases?per_page=100")
    matches = [item for item in releases if item.get("tag_name") == TAG]
    if len(matches) > 1:
        raise ValueError("The release tag has multiple release records")
    return matches[0] if matches else None


def preflight():
    current = check_tag()
    release = get_release()
    needed = not (release and not release.get("draft") and current == revision())
    if release and not current:
        raise ValueError("An existing release has no verifiable tag")
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write("needed=" + str(needed).lower() + "\n")
    print("Version available for verified publication" if needed else "This exact commit is already published; skipped", flush=True)


def registry_manifest(*, authenticated=False):
    repository, tag = IMAGE.removeprefix("ghcr.io/").rsplit(":", 1)
    opener = build_opener(ProxyHandler({}))
    headers = {}
    if authenticated:
        credential = (os.environ["GITHUB_ACTOR"] + ":" + os.environ["GH_TOKEN"]).encode()
        headers["Authorization"] = "Basic " + base64.b64encode(credential).decode()
    token_url = "https://ghcr.io/token?" + urlencode({"service": "ghcr.io", "scope": f"repository:{repository}:pull"})
    try:
        with opener.open(Request(token_url, headers=headers), timeout=30) as response:
            token = json.load(response)["token"]
    except HTTPError as error:
        if (authenticated and error.code in {403, 404}
                and api("/users/wsx68696/packages/container/hassio-mihomo-amd64", missing=True) is None):
            return None
        raise
    headers = {"Authorization": "Bearer " + token, "Accept": ", ".join((
        "application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json"))}

    def manifest(reference):
        try:
            with opener.open(Request(f"https://ghcr.io/v2/{repository}/manifests/{reference}", headers=headers), timeout=30) as response:
                return json.load(response)
        except HTTPError as error:
            if error.code == 404:
                return None
            raise

    value = manifest(tag)
    if value and "manifests" in value:
        matches = [item for item in value["manifests"] if item.get("platform", {}).get("architecture") == "amd64"
                   and item.get("platform", {}).get("os") == "linux"]
        if len(matches) != 1:
            raise ValueError("Published image has an ambiguous amd64 platform")
        value = manifest(matches[0]["digest"])
    return value


def publish_image():
    build, _ = verify_assets()
    check_tag()
    existing = registry_manifest(authenticated=True)
    expected = "sha256:" + build["image_content"]["config_sha256"]
    if existing:
        if existing.get("config", {}).get("digest") != expected:
            raise ValueError("GHCR version already contains different image content; refusing to overwrite")
        print("Identical image content already exists; skipped push", flush=True)
    else:
        run("docker", "push", IMAGE)
    actual = registry_manifest(authenticated=True)
    if not actual or actual.get("config", {}).get("digest") != expected:
        raise ValueError("Published registry image does not match the tested installer image")


def notes_file():
    path = WORK / "release-notes.md"
    path.write_text(
        "在原版 x86-64 HAOS 主机 shell 中运行本发行版 install.sh，自动获取固定版本、固定 SHA256 的完整安装包。"
        "也可下载安装包，解压后运行其中的 install.sh。安装下载可使用 --proxy http://代理主机IP:7897。\n\n"
        "Mihomo TUN 覆盖 HAOS、Core、Supervisor 和插件容器；保留局域网直连。支持完整 Clash 分组、rules、rule-providers、"
        "GEOIP/GEOSITE；Geo 资料可定时或手动更新，支持通过已配置代理、自动回退直连和直连。失败时保留旧配置和资料。\n\n"
        "已验证原版 HAOS 安装、保护模式与 AppArmor、真实 supported/healthy、原版 Core/Supervisor、故障恢复、代理更新、"
        "重复安装，以及官方签名同版本 A/B OTA 保留。新装 Core 按实际端口验证（80 或 8123）。\n\n"
        "不修改 Supervisor/Core。仅分发插件与脚本。SHA256SUMS 附有 GitHub Actions OIDC 的 Sigstore 签名；"
        "安装入口应从可信的本仓库固定版本发行页获取，HAOS 脚本本身不包含 Cosign。\n", encoding="utf-8")
    return path


def release_assets_match(release, files, *, allow_missing=False):
    existing = {item["name"]: item for item in release.get("assets", [])}
    if set(existing) - set(files):
        raise ValueError("Existing release contains unexpected assets")
    for name, asset in existing.items():
        if asset.get("digest") != "sha256:" + digest(files[name]):
            raise ValueError("Existing release asset differs from verified content; refusing to overwrite")
    if not allow_missing and set(existing) != set(files):
        raise ValueError("Release assets are incomplete")
    return [path for name, path in files.items() if name not in existing]


def stage():
    _, files = verify_assets()
    if check_tag() is None:
        api(f"/repos/{REPOSITORY}/git/refs", "POST", {"ref": "refs/tags/" + TAG, "sha": revision()})
    release = get_release()
    if release:
        missing = release_assets_match(release, files, allow_missing=True)
        if missing:
            if not release.get("draft"):
                raise ValueError("Cannot modify an already published release")
            run("gh", "release", "upload", TAG, *missing)
    else:
        run("gh", "release", "create", TAG, *files.values(), "--draft", "--verify-tag", "--title",
            "Mihomo + Zashboard " + VERSION, "--notes-file", notes_file())
    release_assets_match(get_release(), files)


def finalize():
    build, files = verify_assets()
    check_tag()
    actual = registry_manifest()
    if not actual or actual.get("config", {}).get("digest") != "sha256:" + build["image_content"]["config_sha256"]:
        raise ValueError("Public image does not match the verified release")
    release = get_release()
    release_assets_match(release, files)
    if release.get("draft"):
        run("gh", "release", "edit", TAG, "--draft=false", "--latest")
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
        summary.write(f"### Verified installer\n\n[Release](https://github.com/{REPOSITORY}/releases/tag/{TAG})\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--stage", action="store_true")
    parser.add_argument("--publish-image", action="store_true")
    args = parser.parse_args()
    if args.prepare_only:
        prepare()
    elif args.preflight:
        preflight()
    elif args.stage:
        stage()
    elif args.publish_image:
        publish_image()
    else:
        finalize()
