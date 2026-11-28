#!/usr/bin/env python3
"""Verify installation on a disposable, unmodified official HAOS OVA."""
import json
import lzma
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import time
from urllib.request import ProxyHandler, build_opener

from common import ROOT, WORK, DIST, LOCK, IMAGE, APP_SLUG, REPOSITORY_ID, REPOSITORY, BUNDLE_NAME, run, save, digest
from package_installer import build_installer

LOCAL_HTTP = build_opener(ProxyHandler({}))


class Guest:
    def __init__(self, key):
        self.command = ["ssh", "-i", str(key), "-p", "12222", "-o", "BatchMode=yes",
                        "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                        "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=5", "root@127.0.0.1"]

    def shell(self, code, *, timeout=60, check=True):
        result = subprocess.run([*self.command, "sh", "-s"], input=code, text=True,
                                capture_output=True, timeout=timeout)
        if check and result.returncode:
            raise RuntimeError("Guest command failed: " + result.stderr[-2500:])
        return result

    def cli(self, *args, timeout=60):
        result = self.shell(shlex.join(["ha", *args, "--no-progress", "--raw-json"]), timeout=timeout)
        raw = result.stdout.strip()
        data = json.loads(raw[raw.index("{"):])
        if data.get("result") == "error":
            raise RuntimeError("Stock Supervisor returned an API error")
        return data.get("data", data)

    def ingress(self, path, payload=None):
        code = (
            "import urllib.request,json; "
            "opener=urllib.request.build_opener(urllib.request.ProxyHandler({})); "
            f"r=urllib.request.Request('http://172.30.32.1:9091{path}', "
            f"data={None if payload is None else json.dumps(payload).encode()!r}, "
            "headers={'Content-Type':'application/json'}); "
            "print(opener.open(r,timeout=30).read().decode())"
        )
        result = self.shell(shlex.join(["docker", "exec", "hassio_supervisor", "python3", "-c", code]), timeout=60)
        return json.loads(result.stdout)

    def core_onboarding(self):
        port = self.core_http_port()
        code = (
            "import json,urllib.request; "
            "opener=urllib.request.build_opener(urllib.request.ProxyHandler({})); "
            f"print(opener.open('http://127.0.0.1:{port}/api/onboarding',timeout=5).read().decode())"
        )
        result = self.shell(shlex.join(["docker", "exec", "homeassistant", "python3", "-c", code]), timeout=20)
        return isinstance(json.loads(result.stdout), list)

    def core_http_port(self):
        info = self.cli("core", "info")
        port = info.get("port")
        if info.get("ssl") or port not in {80, 8123}:
            raise RuntimeError("Unexpected HTTP configuration on the fresh official test VM")
        return port

    def controller(self, path, secret, method="GET", payload=None):
        code = (
            "import json,urllib.request; o=urllib.request.build_opener(urllib.request.ProxyHandler({})); "
            f"r=urllib.request.Request('http://172.30.32.1:9090{path}',method={method!r},"
            f"data={None if payload is None else json.dumps(payload).encode()!r},"
            f"headers={{'Authorization':{'Bearer ' + secret!r},'Content-Type':'application/json'}}); "
            "b=o.open(r,timeout=15).read(); print(b.decode() if b else '{}')"
        )
        return json.loads(self.shell(shlex.join(["docker", "exec", "hassio_supervisor", "python3", "-c", code])).stdout)


def eventually(description, callback, timeout=600):
    print(description, flush=True)
    deadline = time.monotonic() + timeout
    next_message = time.monotonic() + 60
    last_error = ""
    while time.monotonic() < deadline:
        try:
            value = callback()
            if value:
                return value
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
            last_error = str(error)
        if time.monotonic() >= next_message:
            print(description + ": still waiting" + ("; " + last_error[-500:] if last_error else ""), flush=True)
            next_message = time.monotonic() + 60
        time.sleep(5)
    raise RuntimeError(description + " timed out. " + last_error[-1500:])


def core_api(guest):
    # Probe the real HTTP API in HAOS itself. The caller also records whether
    # QEMU's external forwarding works; a forwarding error is distinct from
    # the original Core's service health and must not hide its diagnostics.
    return guest.core_onboarding()


def forwarded_core_api(guest):
    port = {80: 18080, 8123: 18123}[guest.core_http_port()]
    with LOCAL_HTTP.open(f"http://127.0.0.1:{port}/api/onboarding", timeout=5) as response:
        return isinstance(json.load(response), list)


def core_diagnostics(guest):
    for name, command in [
        ("vm-core-info.log", "ha core info --raw-json --no-progress"),
        ("vm-listeners.log", "ss -lntp 2>/dev/null || netstat -lnt"),
        ("vm-core-files.log", "cat /mnt/data/supervisor/homeassistant/configuration.yaml\ntail -n 100 /mnt/data/supervisor/homeassistant/home-assistant.log 2>/dev/null"),
        ("vm-http-client.log", "command -v curl\ncommand -v wget\ncurl --noproxy '*' -sS --max-time 5 -D - http://127.0.0.1:9090/ui/ | head -c 1000"),
    ]:
        result = guest.shell(command, timeout=20, check=False)
        content = result.stdout + result.stderr
        (WORK / name).write_text(content, encoding="utf-8")
        print(name + ":\n" + content[-12000:], flush=True)


def wait_for_core(guest, description, timeout=600):
    next_diagnostic = 0

    def probe():
        nonlocal next_diagnostic
        try:
            return core_api(guest)
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
            if time.monotonic() >= next_diagnostic:
                next_diagnostic = time.monotonic() + 60
                core_diagnostics(guest)
            raise

    return eventually(description, probe, timeout)


def public_egress(guest, blocked):
    if blocked:
        guest.shell('''iptables -N MIHOMO_INSTALL_TEST
for subnet in 127.0.0.0/8 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16; do
    iptables -A MIHOMO_INSTALL_TEST -d "$subnet" -j RETURN
done
iptables -A MIHOMO_INSTALL_TEST -j REJECT
iptables -I OUTPUT 1 -j MIHOMO_INSTALL_TEST
ip6tables -N MIHOMO_INSTALL_TEST
for subnet in ::1/128 fc00::/7 fe80::/10; do
    ip6tables -A MIHOMO_INSTALL_TEST -d "$subnet" -j RETURN
done
ip6tables -A MIHOMO_INSTALL_TEST -j REJECT
ip6tables -I OUTPUT 1 -j MIHOMO_INSTALL_TEST''')
    else:
        guest.shell('''iptables -D OUTPUT -j MIHOMO_INSTALL_TEST
iptables -F MIHOMO_INSTALL_TEST
iptables -X MIHOMO_INSTALL_TEST
ip6tables -D OUTPUT -j MIHOMO_INSTALL_TEST
ip6tables -F MIHOMO_INSTALL_TEST
ip6tables -X MIHOMO_INSTALL_TEST''', check=False)


def main():
    if os.geteuid() != 0:
        raise ValueError("Run the disposable VM test with sudo on the Actions runner")
    WORK.mkdir(exist_ok=True)
    DIST.mkdir(exist_ok=True)
    official = WORK / "official.qcow2"
    with lzma.open(WORK / "official.qcow2.xz", "rb") as source, official.open("wb") as destination:
        shutil.copyfileobj(source, destination, 1024 * 1024)
    official_hash = digest(official)
    overlay = WORK / "test-vm.qcow2"
    run("qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", official, overlay)
    run("qemu-img", "resize", overlay, "32G")
    secrets = WORK / "vm-secrets"
    secrets.mkdir(mode=0o700)
    key = secrets / "test-key"
    run("ssh-keygen", "-t", "ed25519", "-N", "", "-f", key, capture_output=True)
    config_disk = secrets / "config.img"
    run("truncate", "--size", "16M", config_disk)
    run("mkfs.vfat", "-n", "CONFIG", config_disk)
    run("mcopy", "-i", config_disk, key.with_suffix(".pub"), "::/authorized_keys")
    ovmf = Path("/usr/share/OVMF")
    variables = secrets / "OVMF_VARS.fd"
    shutil.copyfile(ovmf / "OVMF_VARS_4M.fd", variables)
    kvm = os.access("/dev/kvm", os.R_OK | os.W_OK)
    command = [
        "qemu-system-x86_64", "-machine", "q35", "-m", "4096", "-smp", "2",
        "-accel", "kvm" if kvm else "tcg,thread=multi", "-cpu", "host" if kvm else "max",
        "-drive", f"if=pflash,format=raw,readonly=on,file={ovmf / 'OVMF_CODE_4M.fd'}",
        "-drive", f"if=pflash,format=raw,file={variables}",
        "-drive", f"if=virtio,format=qcow2,file={overlay}",
        "-device", "qemu-xhci", "-drive", f"if=none,id=config,format=raw,file={config_disk}",
        "-device", "usb-storage,drive=config", "-device", "virtio-net-pci,netdev=net0",
        "-netdev", "user,id=net0,hostfwd=tcp:127.0.0.1:12222-:22222,hostfwd=tcp:127.0.0.1:18080-:80,hostfwd=tcp:127.0.0.1:18123-:8123,hostfwd=tcp:127.0.0.1:29090-:9090",
        "-display", "none", "-serial", f"file:{WORK / 'vm-serial.log'}", "-monitor", "none",
    ]
    guest = Guest(key)
    report = {"test_haos_version": LOCK["test_haos"]["version"], "base": "official unmodified OVA",
              "acceleration": "kvm" if kvm else "tcg", "app_slug": APP_SLUG,
              "official_download_sha256": LOCK["test_haos"]["image_sha256"]}
    secret = ""
    fixture_output = (WORK / "vm-fixtures.log").open("w")
    fixture = subprocess.Popen(["python3", str(ROOT / "tests/fixtures.py"), "--vm"], stdout=fixture_output, stderr=subprocess.STDOUT)
    qemu_output = (WORK / "vm-qemu.log").open("w")
    qemu = subprocess.Popen(command, stdout=qemu_output, stderr=subprocess.STDOUT)
    try:
        eventually("Waiting for the original HAOS debug SSH", lambda: guest.shell("true", check=False).returncode == 0, 420)

        def supported():
            info = guest.cli("supervisor", "info")
            reasons = sorted(guest.cli("resolution", "info").get("unsupported") or [])
            if info.get("supported") is True and info.get("healthy") is True and not reasons:
                return {"supported": True, "healthy": True, "unsupported": [], "version": info["version"]}
            return False

        before = eventually("Waiting for genuine stock Supervisor support and health", supported, 1200)
        report["before_install"] = before
        wait_for_core(guest, "Waiting for the official Core onboarding API")
        report["core_onboarding_before_install"] = True
        assert forwarded_core_api(guest)
        report["core_http_port"] = guest.core_http_port()
        report["core_onboarding_reachable_through_qemu"] = True
        core_before = guest.shell("docker inspect --format '{{.Id}}' homeassistant").stdout.strip()
        supervisor_before = guest.shell("docker inspect --format '{{.Id}}' hassio_supervisor").stdout.strip()
        core_image = guest.shell("docker inspect --format '{{.Config.Image}}' homeassistant").stdout.strip()
        supervisor_image = guest.shell("docker inspect --format '{{.Config.Image}}' hassio_supervisor").stdout.strip()
        core_config_before = guest.shell("sha256sum /mnt/data/supervisor/homeassistant/configuration.yaml").stdout.split()[0]
        core_info = guest.cli("core", "info")
        expected_core_image = core_info["image"] + ":" + core_info["version"]
        assert core_image == expected_core_image and core_info["image"].startswith("ghcr.io/home-assistant/")
        assert supervisor_image.startswith("ghcr.io/home-assistant/amd64-hassio-supervisor:")
        report["stock_core_image"] = core_image
        report["stock_supervisor_image"] = supervisor_image
        assert guest.shell(f"ha addons info {APP_SLUG} --no-progress >/dev/null 2>&1", check=False).returncode != 0
        bundle = build_installer(before["version"], os.environ["GITHUB_SHA"])
        with bundle.open("rb") as source:
            run(*guest.command, "cat > /mnt/data/mihomo-installer.tar.gz", stdin=source, timeout=300)
        guest.shell("mkdir -p /mnt/data/mihomo-install-test\ntar -xzf /mnt/data/mihomo-installer.tar.gz -C /mnt/data/mihomo-install-test")
        installer = "sh /mnt/data/mihomo-install-test/haos-mihomo-installer/install.sh"
        # Register while the normal Supervisor Internet checks are satisfied;
        # the app import itself must not need public registries or package feeds.
        registered = guest.shell(installer + " --register-only", timeout=180)
        (WORK / "vm-register.log").write_text(registered.stdout + registered.stderr, encoding="utf-8")
        public_egress(guest, True)
        try:
            installed = guest.shell(installer, timeout=600, check=False)
            (WORK / "vm-install.log").write_text(installed.stdout + installed.stderr, encoding="utf-8")
            print(installed.stdout, flush=True)
            if installed.returncode:
                print(installed.stderr, flush=True)
                raise RuntimeError("Installer exited before its health check passed")
            report["installed_with_public_egress_blocked"] = True
        finally:
            public_egress(guest, False)
        profile = eventually("Waiting for script-installed Mihomo", lambda: guest.ingress("/profile"), 180)
        assert profile["phase"] == "waiting-for-nodes" and profile["subscription_url"] == ""
        secret = profile["controller_secret"]
        assert secret
        app = guest.cli("addons", "info", APP_SLUG)
        assert app["protected"] and app["apparmor"] and app["boot"] == "auto"
        assert app["repository"] == REPOSITORY_ID
        assert guest.shell("docker inspect --format '{{.Id}}' homeassistant").stdout.strip() == core_before
        assert guest.shell("docker inspect --format '{{.Id}}' hassio_supervisor").stdout.strip() == supervisor_before
        assert guest.shell("sha256sum /mnt/data/supervisor/homeassistant/configuration.yaml").stdout.split()[0] == core_config_before
        report["core_and_supervisor_containers_unchanged"] = True
        report["no_preloaded_or_overridden_core"] = (core_image == expected_core_image
            and supervisor_image.startswith("ghcr.io/home-assistant/amd64-hassio-supervisor:")
            and digest(official) == official_hash)
        report["registered_public_update_repository"] = True
        report["protected_mode_and_auto_start"] = True
        report["core_onboarding_after_install"] = core_api(guest) and forwarded_core_api(guest)
        report["after_install"] = eventually("Checking genuine support after installation", supported, 180)

        guest.ingress("/profile", {"subscription_url": "http://10.0.2.2:8080/subscription", "subscription_download": "auto",
                                    "update_download": "proxy", "direct_domains": ["installer.test"], "tun_enable": True})
        eventually("Waiting for TUN on original HAOS", lambda: guest.ingress("/profile")["phase"] == "running", 180)
        guest.shell("ip link show ha-mihomo")
        app_container = guest.shell("docker ps --format '{{.Names}}' | sed -n '/" + APP_SLUG + "$/p'").stdout.strip()
        assert len(app_container.splitlines()) == 1
        assert guest.shell(shlex.join(["docker", "inspect", "--format", "{{.AppArmorProfile}}", app_container])).stdout.strip() == APP_SLUG
        report["tun_and_apparmor_on_stock_haos"] = True

        def socks_count():
            return sum(json.load(LOCAL_HTTP.open("http://127.0.0.1:8080/stats", timeout=5)).values())

        def fixture_count():
            return json.load(LOCAL_HTTP.open("http://127.0.0.1:8080/stats", timeout=5)).get("198.51.100.87:8080", 0)

        # This documentation IP has no physical fixture. Only the SOCKS
        # server maps it to its HTTP listener, and its exact counter excludes
        # unrelated Supervisor background requests.
        code = "import urllib.request; o=urllib.request.build_opener(urllib.request.ProxyHandler({})); r=o.open('http://198.51.100.87:8080/check',timeout=30); assert b'haos-mihomo-network-fixture' in r.read()"
        for description, shell in [
            ("supervisor_traffic_proxied", shlex.join(["docker", "exec", "hassio_supervisor", "python3", "-c", code])),
            ("core_traffic_proxied", shlex.join(["docker", "exec", "homeassistant", "python3", "-c", code])),
            ("haos_traffic_proxied", "curl --noproxy '*' -fsS --max-time 30 http://198.51.100.87:8080/check | grep -q haos-mihomo-network-fixture"),
        ]:
            count = fixture_count()
            guest.shell(shell, timeout=90)
            assert fixture_count() > count, description
            report[description] = True

        guest.cli("addons", "stop", APP_SLUG)
        assert guest.cli("addons", "info", APP_SLUG)["state"] == "stopped"
        repeated = guest.shell(installer, timeout=240)
        (WORK / "vm-repeat.log").write_text(repeated.stdout + repeated.stderr, encoding="utf-8")
        profile = eventually("Waiting for repaired app to resume proxying", lambda: (state if (state := guest.ingress("/profile"))["phase"] == "running" else False), 60)
        assert profile["controller_secret"] == secret and profile["direct_domains"] == ["installer.test"]
        assert profile["subscription_url"] == "http://10.0.2.2:8080/subscription" and profile["phase"] == "running"
        report["repeat_install_preserves_configuration"] = True
        report["registered_unhealthy_app_repaired"] = True
        guest.shell('''sh -c 'exec 9>/mnt/data/.mihomo-install.flock; flock -n 9; echo $$ > /mnt/data/mihomo-test-lock.pid; exec sleep 600' </dev/null >/dev/null 2>&1 &''')
        eventually("Waiting for a held installer lock", lambda: guest.shell("test -s /mnt/data/mihomo-test-lock.pid", check=False).returncode == 0, 15)
        assert guest.shell(installer, timeout=30, check=False).returncode != 0
        lock_pid = guest.shell("cat /mnt/data/mihomo-test-lock.pid").stdout.strip()
        assert lock_pid.isdigit()
        guest.shell("kill -9 " + lock_pid + "\nrm -f /mnt/data/mihomo-test-lock.pid")
        guest.shell(installer, timeout=60)
        report["installer_lock_survives_process_death"] = True

        def update_done():
            state = guest.ingress("/profile")["software_update"]
            if state["phase"] == "failed":
                raise AssertionError("Update failed: " + state.get("error", ""))
            return state if state["phase"] == "complete" else False

        guest.controller("/configs", secret, "PATCH", {"mode": "direct"})
        eventually("Setting direct mode for the proxy-update regression check", lambda: guest.ingress("/profile")["mode"] == "direct", 30)
        count = socks_count()
        public_egress(guest, True)
        try:
            guest.ingress("/update", {"component": "mihomo"})
            updated = eventually("Updating Mihomo through the existing proxy with public egress blocked", update_done, 600)
            assert updated["installed"].get("mihomo") and socks_count() > count
        finally:
            public_egress(guest, False)
        report["mihomo_update_through_existing_proxy"] = True
        report["proxy_update_works_in_direct_mode"] = True
        guest.controller("/configs", secret, "PATCH", {"mode": "rule"})
        eventually("Restoring rule mode for the Geo mirror update", lambda: guest.ingress("/profile")["mode"] == "rule", 30)
        guest.ingress("/profile", {"geodata_auto_update": False, "geodata_update_interval": 12})
        guest.ingress("/update", {"component": "geodata"})
        geo_updated = eventually("Updating GeoSite and GeoIP directly from gh-proxy.com with TUN enabled", update_done, 900)
        assert geo_updated["installed"].get("geodata") and "gh-proxy.com" in geo_updated["message"]
        report["geodata_update_through_mirror"] = True
        slot = guest.cli("os", "info")["boot"]
        with (WORK / "official.raucb").open("rb") as source:
            run(*guest.command, "cat > /mnt/data/ci-official.raucb", stdin=source, timeout=600)
        assert guest.shell("sha256sum /mnt/data/ci-official.raucb").stdout.split()[0] == LOCK["test_haos"]["ota_sha256"]
        installed = guest.shell("rauc install /mnt/data/ci-official.raucb", timeout=600)
        (WORK / "vm-ota.log").write_text(installed.stdout + installed.stderr, encoding="utf-8")
        print("Rebooting into the other official A/B system slot", flush=True)
        guest.shell("reboot", check=False)
        time.sleep(15)
        eventually("Waiting for SSH after official OTA", lambda: guest.shell("true", check=False).returncode == 0, 600)
        wait_for_core(guest, "Waiting for Core after official OTA")
        assert forwarded_core_api(guest)
        profile = eventually("Checking persistent app after OTA", lambda: (state if (state := guest.ingress("/profile"))["phase"] == "running" else False), 180)
        assert profile["controller_secret"] == secret and profile["direct_domains"] == ["installer.test"]
        assert profile["subscription_url"] == "http://10.0.2.2:8080/subscription" and profile["tun_enable"]
        assert profile["software_update"]["installed"]["mihomo"] == updated["installed"]["mihomo"]
        assert profile["software_update"]["installed"]["geodata"] == geo_updated["installed"]["geodata"]
        assert profile["geodata_update_interval"] == 12 and not profile["geodata_auto_update"]
        assert guest.cli("os", "info")["boot"] != slot
        guest.shell("ip link show ha-mihomo")
        report["official_ota_preserves_app_and_configuration"] = True
        report["ota_test_kind"] = "official signed same-version A/B reinstall"
        report["after_ota"] = eventually("Checking genuine support after official OTA", supported, 300)
        assert digest(official) == official_hash
        report["official_base_image_unchanged"] = True
        save(DIST / "vm-test.json", report)
        print("Stock HAOS, offline install, TUN, proxy updates and official OTA passed", flush=True)
    finally:
        try:
            core_diagnostics(guest)
        except (OSError, subprocess.TimeoutExpired):
            pass
        for name, shell in [
            ("vm-core.log", "docker logs --tail 300 homeassistant"),
            ("vm-core-state.log", "docker inspect --format '{{json .State}} {{json .Path}} {{json .Args}}' homeassistant"),
            ("vm-supervisor.log", "docker logs --tail 250 hassio_supervisor"),
            ("vm-app.log", "docker ps --all --format '{{.Names}}' | sed -n '/" + APP_SLUG + "$/p' | xargs -r docker logs --tail 150"),
            ("vm-journal.log", "journalctl -b -n 300 --no-pager"),
            ("vm-containers.log", "docker ps --all --format '{{.Names}} {{.Image}} {{.Status}}'"),
        ]:
            try:
                result = guest.shell(shell, timeout=20, check=False)
                content = result.stdout + result.stderr
                if secret:
                    content = content.replace(secret, "[redacted]")
                (WORK / name).write_text(content, encoding="utf-8")
                if name in {"vm-app.log", "vm-supervisor.log"}:
                    print(name + ":\n" + content[-12000:], flush=True)
            except (OSError, subprocess.TimeoutExpired):
                pass
        save(WORK / "vm-progress.json", report)
        qemu.terminate()
        fixture.terminate()
        for process in (qemu, fixture):
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        qemu_output.close()
        fixture_output.close()
        shutil.rmtree(secrets)
        overlay.unlink(missing_ok=True)
        official.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
