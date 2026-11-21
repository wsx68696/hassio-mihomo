#!/usr/bin/env python3
"""Test TUN and forwarded traffic only in isolated container namespaces."""
import json
import os
import subprocess
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from common import ROOT, WORK, IMAGE, run, save


def main():
    data = WORK / "network-data"
    data.mkdir(parents=True, exist_ok=True)
    options = json.loads((ROOT / "mihomo/config.json").read_text(encoding="utf-8"))["options"]
    options.update(controller_secret="ci-test-key-not-a-user-credential", dns_servers=["udp://172.28.254.3:5353"])
    save(data / "options.json", options)
    save(data / "nodes.yaml", {"proxies": [{"name": "fixture", "type": "socks5", "server": "172.28.254.3", "port": 1080, "udp": False}]})
    private = "mihomo-test-private"
    public = "mihomo-test-public"
    containers = ["mihomo-test-node", "mihomo-test-app", "mihomo-test-client"]
    report = {}
    try:
        run("docker", "network", "create", "--subnet", "172.28.254.0/24", private)
        run("docker", "network", "create", "--subnet", "198.51.100.0/24", public)
        run("docker", "run", "--detach", "--name", containers[0], "--network", private, "--ip", "172.28.254.3",
            "--volume", f'{ROOT / "tests/fixtures.py"}:/fixtures.py:ro', "--entrypoint", "python3", IMAGE, "/fixtures.py")
        run("docker", "network", "connect", "--ip", "198.51.100.3", public, containers[0])
        run("docker", "run", "--detach", "--name", containers[1], "--network", private, "--ip", "172.28.254.2",
            "--cap-add", "NET_ADMIN", "--cap-add", "NET_RAW", "--device", "/dev/net/tun",
            "--sysctl", "net.ipv4.ip_forward=1", "--sysctl", "net.ipv6.conf.all.disable_ipv6=0",
            "--sysctl", "net.ipv6.conf.default.disable_ipv6=0",
            "--volume", f"{data}:/data", "--publish", "127.0.0.1:19090:9090", IMAGE)
        # The target must be beyond the ordinary gateway. A directly attached
        # public-address subnet is still a LAN and auto-route preserves it.
        run("docker", "exec", containers[1], "ip", "route", "replace", "default", "via", "172.28.254.3")
        run("docker", "run", "--detach", "--name", containers[2], "--network", private, "--ip", "172.28.254.4",
            "--cap-add", "NET_ADMIN", "--entrypoint", "sleep", IMAGE, "infinity")
        run("docker", "exec", containers[2], "ip", "route", "replace", "default", "via", "172.28.254.2")

        def api(path, method="GET", payload=None, authorized=True):
            headers = {"Content-Type": "application/json"}
            if authorized:
                headers["Authorization"] = "Bearer " + options["controller_secret"]
            request = Request("http://127.0.0.1:19090" + path, headers=headers, method=method,
                              data=None if payload is None else json.dumps(payload).encode())
            with urlopen(request, timeout=5) as response:
                content = response.read()
                return json.loads(content) if content else None

        deadline = time.monotonic() + 90
        while True:
            try:
                general = api("/configs")
                if general.get("tun", {}).get("enable"):
                    break
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise ValueError("TUN did not start in the isolated test namespace")
            time.sleep(2)
        with urlopen("http://127.0.0.1:19090/ui/index.html") as response:
            assert response.status == 200 and b"<html" in response.read().lower()
        try:
            api("/configs", authorized=False)
            raise ValueError("Unauthenticated API request unexpectedly succeeded")
        except HTTPError as error:
            assert error.code == 401
        api("/proxies/PROXY", "PUT", {"name": "fixture"})
        print("TUN and authenticated dashboard are ready", flush=True)

        def fetch(container, address):
            code = f'import urllib.request; print(urllib.request.urlopen("http://{address}:8080/check", timeout=15).read().decode())'
            return run("docker", "exec", container, "python3", "-c", code, capture_output=True, text=True).stdout

        def count():
            code = 'import urllib.request; print(urllib.request.urlopen("http://172.28.254.3:8080/stats").read().decode())'
            return json.loads(run("docker", "exec", containers[2], "python3", "-c", code, capture_output=True, text=True).stdout).get("198.51.100.3:8080", 0)

        before = count()
        assert "haos-mihomo-network-fixture" in fetch(containers[1], "198.51.100.3")
        assert count() > before, "Local output did not pass through the proxy"
        print("Local output traversed the SOCKS fixture", flush=True)
        before = count()
        assert "haos-mihomo-network-fixture" in fetch(containers[2], "198.51.100.3")
        assert count() > before, "Forwarded container output did not pass through the proxy"
        print("Forwarded container output traversed the SOCKS fixture", flush=True)
        before = count()
        assert "haos-mihomo-network-fixture" in fetch(containers[2], "172.28.254.3")
        assert count() == before, "LAN traffic unexpectedly passed through the proxy"
        dns = '''import socket,struct
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.settimeout(10)
q=b"\\x12\\x34"+struct.pack("!HHHHH",0x0100,1,0,0,0)+b"\\x07example\\x04test\\x00"+struct.pack("!HH",1,1)
s.sendto(q,("198.51.100.3",53)); answer=s.recv(512)
assert answer[:2]==b"\\x12\\x34" and answer[-4:]==socket.inet_aton("198.51.100.3")
print("DNS hijack succeeded")'''
        run("docker", "exec", containers[2], "python3", "-c", dns)
        updater = '''import json,sys
from pathlib import Path
sys.path.insert(0,"/opt/ha-mihomo")
from run import Controller
c=Controller(Path("/data"),json.loads(Path("/data/options.json").read_text()))
c.update({"subscription_url":"http://198.51.100.3:8080/subscription"})
c.refresh_subscription("http://198.51.100.3:8080/subscription","proxy")
old=Path("/data/nodes.yaml").read_bytes()
c.update({"subscription_url":"http://198.51.100.3:8080/bad-subscription"})
try:
 c.refresh_subscription("http://198.51.100.3:8080/bad-subscription","proxy")
 raise AssertionError("Invalid replacement accepted")
except ValueError:
 assert Path("/data/nodes.yaml").read_bytes()==old
print("Existing proxy updates and invalid-node rollback succeeded")'''
        before = count()
        run("docker", "exec", containers[1], "python3", "-c", updater)
        assert count() > before, "Subscription download did not traverse existing proxy"
        direct = '''import sys
sys.path.insert(0,"/opt/ha-mihomo")
from downloads import fetch
raw,method=fetch("http://198.51.100.3:8080/check","direct",4096)
assert b"haos-mihomo-network-fixture" in raw and method=="direct"'''
        before = count()
        run("docker", "exec", containers[1], "python3", "-c", direct)
        assert count() == before, "Direct fallback was sent back into TUN"
        api("/configs", "PATCH", {"mode": "direct", "tun": {"enable": False}})
        time.sleep(4)
        profile_code = 'import json; from pathlib import Path; p=json.loads(Path("/data/profile.json").read_text()); print(json.dumps({"mode":p["mode"],"tun_enable":p["tun_enable"]}))'
        profile = json.loads(run("docker", "exec", containers[1], "python3", "-c", profile_code, capture_output=True, text=True).stdout)
        assert profile["mode"] == "direct" and not profile["tun_enable"]
        run("docker", "restart", containers[1])
        time.sleep(7)
        general = api("/configs")
        assert general["mode"] == "direct" and not general["tun"]["enable"]
        assert api("/proxies/PROXY")["now"] == "fixture"
        report = {"local_output_proxied": True, "forwarded_container_output_proxied": True,
                  "lan_stays_direct": True, "dns_hijack": True, "dashboard_available": True,
                  "api_requires_secret": True, "dashboard_choices_survive_restart": True,
                  "subscription_updated_through_existing_proxy": True, "invalid_nodes_preserve_working_cache": True,
                  "direct_fallback_bypasses_tun": True}
        save(WORK / "network-test.json", report)
    finally:
        for container in containers:
            logs = subprocess.run(["docker", "logs", "--tail", "100", container], capture_output=True, text=True)
            (WORK / (container + ".log")).write_text(logs.stdout + logs.stderr, encoding="utf-8")
            print(logs.stdout + logs.stderr, flush=True)
            if container == containers[1]:
                for command in [["ip", "rule"], ["ip", "route", "show", "table", "all"], ["nft", "list", "ruleset"]]:
                    debug = subprocess.run(["docker", "exec", container, *command], capture_output=True, text=True)
                    print(debug.stdout + debug.stderr, flush=True)
            subprocess.run(["docker", "rm", "--force", container], capture_output=True)
        for network in (public, private):
            subprocess.run(["docker", "network", "rm", network], capture_output=True)


if __name__ == "__main__":
    main()
