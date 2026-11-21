"""Publish only the verified installer, reports, and matching upstream source."""
import argparse
import json
import os
import shutil

from common import ROOT, WORK, DIST, VERSION, BUNDLE_NAME, digest, run


def verify_reports(folder):
    build = json.loads((folder / "build-report.json").read_text(encoding="utf-8"))
    network = json.loads((folder / "network-test.json").read_text(encoding="utf-8"))
    vm = json.loads((folder / "vm-test.json").read_text(encoding="utf-8"))
    if not build.get("backup_contains_only_app") or not build.get("repository_cache_has_no_credentials"):
        raise ValueError("Installer backup and repository cache verification is incomplete")
    if not network or not all(value is True for value in network.values()):
        raise ValueError("The app network integration tests did not all pass")
    required = ["core_onboarding_before_install", "core_onboarding_after_install", "installed_with_public_egress_blocked",
                "core_and_supervisor_containers_unchanged", "registered_public_update_repository",
                "protected_mode_and_auto_start", "tun_and_apparmor_on_stock_haos", "supervisor_traffic_proxied",
                "core_traffic_proxied", "haos_traffic_proxied", "repeat_install_preserves_configuration",
                "mihomo_update_through_existing_proxy", "official_ota_preserves_app_and_configuration",
                "official_base_image_unchanged", "no_preloaded_or_overridden_core"]
    if not all(vm.get(key) is True for key in required):
        raise ValueError("Installation on original HAOS has incomplete verification evidence")
    for phase in ("after_install", "after_ota"):
        if vm[phase].get("supported") is not True or vm[phase].get("healthy") is not True or vm[phase].get("unsupported") != []:
            raise ValueError("Genuine stock Supervisor support or health checks did not pass")
    if not (folder / BUNDLE_NAME).is_file():
        raise ValueError("The verified installer package is missing")
    if any(path.name.endswith((".img", ".img.xz", ".qcow2", ".qcow2.xz", ".raucb")) for path in folder.iterdir()):
        raise ValueError("Only the app installer is distributed; HAOS test disks must stay temporary")


def prepare():
    shutil.copy2(WORK / "network-test.json", DIST / "network-test.json")
    verify_reports(DIST)
    shutil.copy2(ROOT / "install.sh", DIST / "install.sh")
    shutil.copy2(WORK / "python-base.txt", DIST / "python-base.txt")
    files = sorted(path for path in DIST.iterdir() if path.is_file() and path.name != "SHA256SUMS")
    (DIST / "SHA256SUMS").write_text("".join(f"{digest(path)}  {path.name}\n" for path in files), encoding="utf-8")


def publish():
    verify_reports(DIST)
    files = {path.name: path for path in DIST.iterdir() if path.is_file() and path.name != "SHA256SUMS"}
    sums = DIST / "SHA256SUMS"
    checked = set()
    for line in sums.read_text(encoding="utf-8").splitlines():
        expected, name = line.split("  ", 1)
        if name not in files or name in checked or digest(files[name]) != expected:
            raise ValueError("Release file checksum verification failed")
        checked.add(name)
    if checked != set(files):
        raise ValueError("Release has a file not covered by SHA256SUMS")
    notes = WORK / "release-notes.md"
    notes.write_text(
        "原版 x86-64 HAOS 的 Mihomo + Zashboard 插件安装脚本。\n\n"
        "下载 install.sh，在 HAOS 本地控制台输入 login 后运行 sh install.sh；脚本自动获取完整依赖包并注册公开插件更新仓库。"
        "也可下载 mihomo-installer-amd64.tar.gz，解压后运行其中的 install.sh。\n\n"
        "TUN 覆盖本机、Core、Supervisor 和插件容器；局域网与发现流量保持直连。节点、API 密钥和面板设置保存在本机 data。"
        "插件管理程序可在 HA 插件商店更新；Mihomo/Zashboard 面板更新可通过已有代理下载。\n\n"
        "已通过原版 HAOS 上的安装、保护模式/AppArmor、真实 supported/healthy 检查、"
        "Core/Supervisor 容器保持、TUN、重复安装、通过已有代理更新以及官方签名同版本 A/B OTA 保留测试。"
        "只发布插件和安装脚本，测试用系统磁盘不分发。\n",
        encoding="utf-8")
    tag = "v" + VERSION
    run("gh", "release", "create", tag, sums, *files.values(), "--target", os.environ["GITHUB_SHA"],
        "--title", "Mihomo + Zashboard " + VERSION, "--notes-file", notes)
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
        summary.write(f"### Verified installer\n\n[Release](https://github.com/wsx68696/hassio-mihomo/releases/tag/{tag})\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    prepare() if args.prepare_only else publish()
