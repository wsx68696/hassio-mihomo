#!/usr/bin/env python3
"""Exercise real routing, rule matches and crash recovery in a kept namespace."""
import json
import subprocess
import time
from urllib.error import HTTPError
from urllib.request import Request, ProxyHandler, build_opener

from common import ROOT, WORK, IMAGE, run, save

HTTP = build_opener(ProxyHandler({}))
KEEPER = "mihomo-test-keeper"
APP = "mihomo-test-app"
NODE = "mihomo-test-node"
CLIENT = "mihomo-test-client"
SUPERVISOR = "mihomo-test-supervisor"


def execute(container, *command):
    return run("docker", "exec", container, *command, capture_output=True, text=True).stdout.strip()


def python(container, code):
    return execute(container, "python3", "-c", code)


def eventually(description, callback, timeout=120):
    print(description, flush=True)
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            value = callback()
            if value:
                return value
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            last = str(error)[-500:]
        time.sleep(1)
    raise ValueError(description + " timed out: " + last)


def ingress(path, payload=None, *, expected=200):
    raw = payload if isinstance(payload, bytes) else None if payload is None else json.dumps(payload).encode()
    code = f'''import json,urllib.request,urllib.error
opener=urllib.request.build_opener(urllib.request.ProxyHandler({{}}))
request=urllib.request.Request("http://172.30.32.1:9091{path}",data={raw!r},headers={{"Content-Type":"application/json"}})
try:
 response=opener.open(request,timeout=90)
except urllib.error.HTTPError as error:
 response=error
assert response.code=={expected},(response.code,response.read())
print(response.read().decode())'''
    return json.loads(python(SUPERVISOR, code))


def counts():
    return json.loads(python(CLIENT, 'import urllib.request; print(urllib.request.urlopen("http://172.30.32.3:8080/stats",timeout=5).read().decode())'))


def fetch(container, address, *, hostname=None):
    code = f'''import urllib.request
opener=urllib.request.build_opener(urllib.request.ProxyHandler({{}}))
r=urllib.request.Request("http://{address}:8080/check",headers={{"Host":{hostname or address!r}}})
print(opener.open(r,timeout=15).read().decode())'''
    return python(container, code)


def dns_address(domain):
    code = f'''import socket,struct
labels=b"".join(bytes([len(label)])+label.encode() for label in {domain!r}.split("."))+b"\\x00"
query=b"\\x12\\x34"+struct.pack("!HHHHH",0x0100,1,0,0,0)+labels+struct.pack("!HH",1,1)
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.settimeout(15)
s.sendto(query,("198.51.100.3",53)); answer=s.recv(2048)
assert answer[:2]==b"\\x12\\x34" and struct.unpack("!H",answer[6:8])[0]>0
print(socket.inet_ntoa(answer[-4:]))'''
    return python(CLIENT, code)


def guard_packets():
    document = json.loads(execute(KEEPER, "nft", "-j", "list", "table", "inet", "fixture_guard"))
    return sum(expression["counter"]["packets"] for item in document["nftables"]
               for expression in item.get("rule", {}).get("expr", []) if "counter" in expression)


def guard_survived():
    execute(KEEPER, "nft", "list", "table", "inet", "fixture_guard")
    rules = json.loads(execute(KEEPER, "ip", "-j", "rule", "show"))
    assert any(rule.get("priority") == 21000 and rule.get("table") == 21000 for rule in rules)
    routes = json.loads(execute(KEEPER, "ip", "-j", "route", "show", "table", "21000"))
    assert any(route.get("dst") == "192.0.2.0/24" and route.get("type") == "blackhole" for route in routes)


def node_hash():
    return python(APP, 'import hashlib; from pathlib import Path; print(hashlib.sha256(Path("/data/nodes.yaml").read_bytes()).hexdigest())')


def main():
    data = WORK / "network-data"
    data.mkdir(parents=True, exist_ok=True)
    options = json.loads((ROOT / "mihomo/config.json").read_text(encoding="utf-8"))["options"]
    options.update(controller_secret="ci-test-key-not-a-user-credential",
                   dns_servers=["udp://172.30.32.3:5353"], subscription_download="proxy")
    save(data / "options.json", options)
    node = {"name": "fixture", "type": "socks5", "server": "172.30.32.3", "port": 1080, "udp": False}
    save(data / "nodes.yaml", {"proxies": [node]})
    networks = {"mihomo-test-private": "172.30.32.0/24", "mihomo-test-public": "198.51.100.0/24",
                "mihomo-test-lan": "10.29.255.0/24"}
    containers = [APP, CLIENT, SUPERVISOR, NODE, KEEPER]

    def api(path, method="GET", payload=None, authorized=True):
        headers = {"Content-Type": "application/json"}
        if authorized:
            headers["Authorization"] = "Bearer " + options["controller_secret"]
        request = Request("http://127.0.0.1:19090" + path, headers=headers, method=method,
                          data=None if payload is None else json.dumps(payload).encode())
        with HTTP.open(request, timeout=5) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}

    def ready():
        general = api("/configs")
        return general["tun"]["enable"] and ingress("/profile")["phase"] == "running"

    def apply_policy(payload):
        generation = ingress("/profile")["runtime_generation"]
        ingress("/nodes", payload)
        eventually("Waiting for the new policy process", lambda: ready() and ingress("/profile")["runtime_generation"] > generation)

    try:
        for network, subnet in networks.items():
            run("docker", "network", "create", "--subnet", subnet, "--gateway", subnet.rsplit(".", 1)[0] + ".254", network)
        private, public, lan = networks
        run("docker", "run", "--detach", "--name", NODE, "--network", private, "--ip", "172.30.32.3",
            "--cap-add", "NET_ADMIN", "--sysctl", "net.ipv4.ip_forward=1",
            "--volume", f'{ROOT / "tests/fixtures.py"}:/fixtures.py:ro', "--entrypoint", "python3", IMAGE, "/fixtures.py")
        run("docker", "network", "connect", "--ip", "198.51.100.3", public, NODE)
        run("docker", "network", "connect", "--ip", "10.29.255.3", lan, NODE)
        python(NODE, '''import json,subprocess
links=json.loads(subprocess.check_output(["ip","-j","addr"]))
dev=next(link["ifname"] for link in links if any(addr.get("local")=="198.51.100.3" for addr in link.get("addr_info",[])))
for suffix in (11,12,13,14):
 subprocess.run(["ip","addr","add",f"198.51.100.{suffix}/24","dev",dev],check=True)''')
        run("docker", "run", "--detach", "--name", KEEPER, "--network", private, "--ip", "172.30.32.1",
            "--cap-add", "NET_ADMIN", "--sysctl", "net.ipv4.ip_forward=1",
            "--sysctl", "net.ipv4.conf.all.send_redirects=0", "--sysctl", "net.ipv4.conf.default.send_redirects=0",
            "--sysctl", "net.ipv6.conf.all.disable_ipv6=0", "--sysctl", "net.ipv6.conf.default.disable_ipv6=0",
            "--publish", "127.0.0.1:19090:9090", "--entrypoint", "sleep", IMAGE, "infinity")
        execute(KEEPER, "ip", "route", "replace", "default", "via", "172.30.32.3")
        execute(KEEPER, "ip", "route", "replace", "10.29.255.0/24", "via", "172.30.32.3")
        execute(KEEPER, "nft", "add", "table", "inet", "fixture_guard")
        execute(KEEPER, "nft", "add", "chain", "inet", "fixture_guard", "forward",
                "{ type filter hook forward priority -10; policy accept; }")
        execute(KEEPER, "nft", "add", "rule", "inet", "fixture_guard", "forward", "ip", "daddr", "10.29.255.3", "counter")
        execute(KEEPER, "ip", "route", "add", "blackhole", "192.0.2.0/24", "table", "21000")
        execute(KEEPER, "ip", "rule", "add", "priority", "21000", "to", "192.0.2.0/24", "table", "21000")
        for name, address in ((CLIENT, "172.30.32.4"), (SUPERVISOR, "172.30.32.2")):
            run("docker", "run", "--detach", "--name", name, "--network", private, "--ip", address,
                "--cap-add", "NET_ADMIN", "--sysctl", "net.ipv4.conf.all.accept_redirects=0",
                "--entrypoint", "sleep", IMAGE, "infinity")
        execute(CLIENT, "ip", "route", "replace", "default", "via", "172.30.32.1")
        # The keeper models host_network: SIGKILL cannot destroy its nft table.
        run("docker", "run", "--detach", "--name", APP, "--network", "container:" + KEEPER,
            "--cap-add", "NET_ADMIN", "--cap-add", "NET_RAW", "--device", "/dev/net/tun",
            "--volume", f"{data}:/data", IMAGE)
        eventually("Waiting for TUN and authenticated controller", ready)
        with HTTP.open("http://127.0.0.1:19090/ui/index.html", timeout=5) as response:
            assert response.status == 200 and b"<html" in response.read().lower()
        try:
            api("/configs", authorized=False)
            raise ValueError("Unauthenticated API request unexpectedly succeeded")
        except HTTPError as error:
            assert error.code == 401
        api("/proxies/PROXY", "PUT", {"name": "fixture"})
        for container in (KEEPER, CLIENT):
            before = counts().get("198.51.100.3:8080", 0)
            assert "via-socks-fixture" in fetch(container, "198.51.100.3")
            assert counts().get("198.51.100.3:8080", 0) > before
        print("Host and forwarded traffic used SOCKS", flush=True)
        before_proxy = counts().get("10.29.255.3:8080", 0)
        before_forward = guard_packets()
        assert "via-socks-fixture" not in fetch(CLIENT, "10.29.255.3")
        assert guard_packets() > before_forward, "LAN request bypassed the router: invalid test topology"
        assert counts().get("10.29.255.3:8080", 0) == before_proxy, "LAN target was proxied"
        assert dns_address("example.test") == "198.51.100.11"
        print("Routed LAN stayed direct; UDP DNS traversed TUN", flush=True)
        for path in ("/update", "/profile"):
            assert "error" in ingress(path, [], expected=400)
        before, old = counts().get("198.51.100.3:8080", 0), node_hash()
        generation = ingress("/profile")["runtime_generation"]
        ingress("/profile", {"subscription_url": "http://198.51.100.3:8080/subscription"})
        eventually("Updating subscription through the running proxy", lambda: ready() and node_hash() != old
                   and ingress("/profile")["runtime_generation"] > generation)
        assert counts().get("198.51.100.3:8080", 0) > before
        old = node_hash()
        ingress("/profile", {"subscription_url": "http://198.51.100.3:8080/bad-subscription"})
        eventually("Rejecting invalid subscription without losing nodes", lambda: bool(ingress("/profile")["last_update_error"]))
        assert node_hash() == old
        ingress("/profile", {"subscription_url": ""})
        before = counts().get("198.51.100.3:8080", 0)
        python(APP, '''import sys
sys.path.insert(0,"/opt/ha-mihomo")
from downloads import fetch
raw,method=fetch("http://198.51.100.3:8080/check","direct",4096)
assert b"haos-mihomo-network-fixture" in raw and b"via-socks-fixture" not in raw and method=="direct"''')
        assert counts().get("198.51.100.3:8080", 0) == before
        policy = {"mode": "rule", "proxies": [node, {**node, "name": "alternate"}],
                  "proxy-groups": [{"name": "RULE", "type": "select", "proxies": ["fixture", "alternate", "DIRECT"]}],
                  "rule-providers": {"fixture_rules": {"type": "http", "behavior": "classical", "format": "yaml",
                       "url": "http://198.51.100.3:8080/rule-provider", "interval": 3600}},
                  "rules": ["DOMAIN,example.test,RULE", "RULE-SET,fixture_rules,RULE", "GEOSITE,cn,RULE",
                            "GEOIP,CN,RULE,no-resolve", "MATCH,DIRECT"], "geodata-mode": True}
        before = counts().get("198.51.100.3:8080", 0)
        yaml_policy = b'''mode: rule
proxies:
  - &fixture
    name: fixture
    type: socks5
    server: 172.30.32.3
    port: 1080
    udp: false
  - <<: *fixture
    name: alternate
proxy-groups:
  - name: RULE
    type: select
    proxies: [fixture, alternate, DIRECT]
rule-providers:
  fixture_rules:
    type: http
    behavior: classical
    format: yaml
    url: http://198.51.100.3:8080/rule-provider
    interval: 3600
rules:
  - DOMAIN,example.test,RULE
  - RULE-SET,fixture_rules,RULE
  - GEOSITE,cn,RULE
  - GEOIP,CN,RULE,no-resolve
  - MATCH,DIRECT
geodata-mode: true
'''
        apply_policy(yaml_policy)
        eventually("Applying complete Clash policy", lambda: ready() and ingress("/profile")["policy"]["rule_providers"] == 1)
        assert counts().get("198.51.100.3:8080", 0) > before, "Rule provider did not use the configured proxy"
        api("/proxies/RULE", "PUT", {"name": "alternate"})
        rules = api("/rules")["rules"]
        assert {"Domain", "RuleSet", "GeoSite", "GeoIP", "Match"} <= {rule["type"] for rule in rules}
        for domain, expected_ip in (("example.test", "198.51.100.11"), ("provider.test", "198.51.100.12"),
                                    ("baidu.com", "198.51.100.13")):
            address = dns_address(domain)
            assert address == expected_ip
            before = counts().get("198.51.100.3:8080", 0)
            assert "via-socks-fixture" in fetch(CLIENT, address, hostname=domain), domain
            assert counts().get("198.51.100.3:8080", 0) > before, domain
        before = counts().get("198.51.100.3:8080", 0)
        assert "via-socks-fixture" in fetch(CLIENT, "223.5.5.5")
        assert counts().get("198.51.100.3:8080", 0) > before
        address = dns_address("direct.test")
        assert address == "198.51.100.14"
        before = counts().get("198.51.100.3:8080", 0)
        assert "via-socks-fixture" not in fetch(CLIENT, address, hostname="direct.test")
        assert counts().get("198.51.100.3:8080", 0) == before, "MATCH,DIRECT was ignored"
        old = node_hash()
        broken = json.loads(json.dumps(policy))
        broken["rule-providers"]["fixture_rules"]["url"] = "http://198.51.100.3:8080/bad-rule-provider"
        assert "error" in ingress("/nodes", broken, expected=400)
        assert node_hash() == old
        python(APP, '''import json
from pathlib import Path
p=Path("/data/user-providers"); p.mkdir(exist_ok=True)
(p/"domains.yaml").write_text(json.dumps({"payload":["provider.test"]}))''')
        execute(APP, "/usr/local/bin/mihomo", "convert-ruleset", "domain", "yaml", "/data/user-providers/domains.yaml", "/data/user-providers/domains.mrs")
        for spec in ({"type": "inline", "behavior": "domain", "payload": ["provider.test"]},
                     {"type": "file", "behavior": "domain", "format": "yaml", "path": "domains.yaml"},
                     {"type": "file", "behavior": "domain", "format": "mrs", "path": "domains.mrs"}):
            variant = {**policy, "rule-providers": {"fixture_rules": spec}}
            apply_policy(variant)
            eventually("Applying " + spec["type"] + " " + spec.get("format", "inline") + " rule provider", ready)
            address = dns_address("provider.test")
            before = counts().get("198.51.100.3:8080", 0)
            assert "via-socks-fixture" in fetch(CLIENT, address, hostname="provider.test")
            assert counts().get("198.51.100.3:8080", 0) > before
        provider_policy = {**policy, "proxies": [],
            "proxy-providers": {"fixture_nodes": {"type": "http", "url": "http://198.51.100.3:8080/subscription", "interval": 3600}},
            "proxy-groups": [{"name": "RULE", "type": "select", "proxies": ["DIRECT"], "use": ["fixture_nodes"]}]}
        apply_policy(provider_policy)
        eventually("Loading HTTP proxy provider", ready)
        api("/proxies/RULE", "PUT", {"name": "fixture"})
        assert "via-socks-fixture" in fetch(CLIENT, "223.5.5.5")
        apply_policy(policy)
        eventually("Restoring policy before crash recovery checks", ready)
        api("/proxies/RULE", "PUT", {"name": "alternate"})
        print("DOMAIN, RULE-SET, GEOSITE and GEOIP matched independently; fallback stayed direct", flush=True)
        parent = execute(APP, "cat", "/proc/1/stat")
        killed = python(APP, '''import os,signal
from pathlib import Path
for process in Path("/proc").iterdir():
 if not process.name.isdigit(): continue
 try: argv=(process/"cmdline").read_bytes().split(b"\\0")
 except OSError: continue
 if b"/data/config.json" in argv and argv[0].endswith(b"mihomo"):
  os.kill(int(process.name),signal.SIGKILL); print(process.name); break
else: raise AssertionError("Main Mihomo child not found")''')
        assert killed
        eventually("Recovering a killed Mihomo child", ready)
        assert execute(APP, "cat", "/proc/1/stat").split()[21] == parent.split()[21]
        assert "via-socks-fixture" in fetch(CLIENT, "223.5.5.5")
        guard_survived()
        run("docker", "kill", "--signal", "KILL", APP)
        execute(KEEPER, "nft", "list", "table", "inet", "mihomo")
        run("docker", "start", APP)
        eventually("Recovering the real nft table left by SIGKILL", ready)
        assert "via-socks-fixture" in fetch(CLIENT, "223.5.5.5")
        guard_survived()
        run("docker", "stop", "--time", "45", APP)
        missing = subprocess.run(["docker", "exec", KEEPER, "nft", "list", "table", "inet", "mihomo"], capture_output=True)
        assert missing.returncode != 0, "Normal termination left a Mihomo nft table"
        for family in ("-4", "-6"):
            rules = json.loads(execute(KEEPER, "ip", family, "-j", "rule", "show"))
            assert not any(18300 <= rule.get("priority", 0) <= 18320 or rule.get("priority") == 18500
                           or rule.get("table") == 8301 for rule in rules)
        guard_survived()
        run("docker", "start", APP)
        eventually("Restarting after normal cleanup", ready)
        api("/configs", "PATCH", {"mode": "direct", "tun": {"enable": False}})
        eventually("Persisting dashboard mode and TUN choices", lambda: ingress("/profile")["mode"] == "direct"
                   and ingress("/profile")["tun_enable"] is False)
        run("docker", "restart", "--time", "45", APP)
        eventually("Restoring saved dashboard state", lambda: api("/configs")["mode"] == "direct")
        assert api("/configs")["tun"]["enable"] is False
        assert api("/proxies/RULE")["now"] == "alternate"
        guard_survived()
        save(WORK / "network-test.json", {key: True for key in (
            "local_output_proxied", "forwarded_container_output_proxied", "lan_stays_direct", "lan_traverses_router",
            "dns_hijack", "dashboard_available", "api_requires_secret", "invalid_api_returns_json",
            "dashboard_choices_survive_restart", "subscription_updated_through_existing_proxy",
            "invalid_nodes_preserve_working_cache", "invalid_rules_preserve_working_cache", "direct_fallback_bypasses_tun",
            "domain_rule_matches", "rule_provider_matches", "geosite_matches", "geoip_matches", "match_direct_fallback",
            "provider_download_through_existing_proxy", "child_sigkill_recovers", "addon_sigkill_recovers_stale_nft",
            "normal_stop_cleans_network", "unrelated_network_objects_preserved", "yaml_policy_import",
            "inline_rule_provider_matches", "file_rule_provider_matches", "mrs_rule_provider_matches", "http_proxy_provider_loaded")})
    finally:
        for container in containers:
            logs = subprocess.run(["docker", "logs", "--tail", "180", container], capture_output=True, text=True)
            (WORK / (container + ".log")).write_text(logs.stdout + logs.stderr, encoding="utf-8")
            if container in {APP, KEEPER}:
                print(logs.stdout + logs.stderr, flush=True)
        debug_log = []
        for container in (KEEPER, CLIENT, NODE):
            for command in (["ip", "rule"], ["ip", "route", "show", "table", "all"], ["nft", "list", "ruleset"]):
                result = subprocess.run(["docker", "exec", container, *command], capture_output=True, text=True)
                debug_log.append(container + " " + " ".join(command) + "\n" + result.stdout + result.stderr)
        (WORK / "mihomo-test-routing.log").write_text("\n".join(debug_log), encoding="utf-8")
        print("\n".join(debug_log), flush=True)
        for container in containers:
            subprocess.run(["docker", "rm", "--force", container], capture_output=True)
        for network in reversed(networks):
            subprocess.run(["docker", "network", "rm", network], capture_output=True)


if __name__ == "__main__":
    main()
