"""Generate a JSON (valid YAML) Mihomo config from this app's own data."""
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import secrets
from urllib.parse import urlsplit

PRIVATE_ROUTES = [
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
    "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16", "224.0.0.0/4", "240.0.0.0/4",
    "::/128", "::1/128", "fc00::/7", "fe80::/10", "fec0::/10", "ff00::/8",
]
EDITABLE = {"subscription_url", "subscription_download", "subscription_interval", "update_download", "tun_enable", "dns_servers", "direct_domains"}


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".new")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def validate(profile):
    url = profile.get("subscription_url", "")
    if not isinstance(url, str):
        raise ValueError("订阅链接必须是字符串")
    if url:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("请填写有效的 HTTP/HTTPS Clash 或 Mihomo 订阅链接")
    if not isinstance(profile.get("tun_enable"), bool):
        raise ValueError("TUN 开关必须是布尔值")
    if profile.get("subscription_download") not in {"auto", "proxy", "direct"}:
        raise ValueError("订阅下载方式必须是 auto、proxy 或 direct")
    if profile.get("update_download", "auto") not in {"auto", "proxy", "direct"}:
        raise ValueError("软件更新下载方式必须是 auto、proxy 或 direct")
    interval = profile.get("subscription_interval")
    if isinstance(interval, bool) or not isinstance(interval, int) or not 60 <= interval <= 86400:
        raise ValueError("订阅更新间隔必须为 60 至 86400 秒")
    for key in ("dns_servers", "direct_domains"):
        values = profile.get(key)
        if not isinstance(values, list) or any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError(f"{key} 必须是非空字符串组成的列表")
    if not profile["dns_servers"]:
        raise ValueError("至少需要一个 DNS 上游")
    if any("," in domain or "\n" in domain for domain in profile["direct_domains"]):
        raise ValueError("直连域名中不能包含逗号或换行")
    return profile


def load_profile(data, options):
    data = Path(data)
    fingerprint = hashlib.sha256(json.dumps(options, sort_keys=True).encode()).hexdigest()
    path = data / "profile.json"
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if state.get("options_hash") != fingerprint:
        state = {key: options.get(key, "auto") for key in EDITABLE}
        state.update(options_hash=fingerprint, mode="rule", log_level="info")
        atomic_json(path, state)
    return validate(state)


def load_secret(data, configured):
    path = Path(data) / "controller-secret"
    if configured:
        return configured
    if not path.exists():
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(secrets.token_urlsafe(32) + "\n")
    return path.read_text(encoding="utf-8").strip()


def trusted_ingress(address):
    try:
        peer = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(peer, ipaddress.IPv6Address) and peer.ipv4_mapped:
        peer = peer.ipv4_mapped
    return peer == ipaddress.ip_address("172.30.32.2")


def make_config(profile, secret, data, *, start_tun=False, nodes_path=None):
    data = Path(data)
    validate(profile)
    provider = None
    nodes = Path(nodes_path) if nodes_path else data / "nodes.yaml"
    if nodes.exists():
        provider = {"type": "file", "path": str(nodes)}
    result = {
        "mixed-port": 17890, "bind-address": "127.0.0.1", "allow-lan": False, "mode": profile.get("mode", "rule"),
        "log-level": profile.get("log_level", "info"), "ipv6": True, "find-process-mode": "off",
        "external-controller": "0.0.0.0:9090", "secret": secret,
        "external-ui": str(data / "ui"),
        "external-ui-url": "https://github.com/Zephyruso/zashboard/releases/download/v3.30.0/dist.zip",
        "external-controller-cors": {"allow-origins": [], "allow-private-network": True},
        "profile": {"store-selected": True, "store-fake-ip": False},
        "tun": {
            "enable": bool(start_tun and provider and profile["tun_enable"]),
            "device": "ha-mihomo", "stack": "mixed", "mtu": 1500,
            "inet6-address": ["fdfe:dcba:9876::1/126"],
            "auto-route": True, "auto-redirect": True, "auto-detect-interface": True,
            "auto-redirect-output-mark": 8230,
            "strict-route": False, "dns-mode": "disabled", "dns-hijack": ["any:53", "tcp://any:53"],
            "route-exclude-address": PRIVATE_ROUTES,
        },
        "dns": {
            "enable": True, "listen": "127.0.0.1:1053", "ipv6": True, "enhanced-mode": "redir-host",
            "default-nameserver": ["223.5.5.5", "1.1.1.1"],
            "nameserver": profile["dns_servers"],
            "proxy-server-nameserver": profile["dns_servers"],
            "nameserver-policy": {"+.local": "system", "+.lan": "system", "+.home.arpa": "system"},
        },
        "rules": ["DOMAIN-SUFFIX,local,DIRECT", "DOMAIN-SUFFIX,lan,DIRECT", "DOMAIN-SUFFIX,home.arpa,DIRECT"],
    }
    for domain in profile["direct_domains"]:
        result["rules"].append("DOMAIN-SUFFIX," + domain.removeprefix("+.") + ",DIRECT")
    for network in PRIVATE_ROUTES:
        kind = "IP-CIDR6" if ":" in network else "IP-CIDR"
        result["rules"].append(f"{kind},{network},DIRECT,no-resolve")
    if provider:
        provider["health-check"] = {"enable": True, "url": "https://cp.cloudflare.com/generate_204", "interval": 300}
        result["proxy-providers"] = {"subscription": provider}
        result["proxy-groups"] = [
            {"name": "AUTO", "type": "url-test", "use": ["subscription"],
             "url": "https://cp.cloudflare.com/generate_204", "interval": 300, "lazy": True},
            {"name": "PROXY", "type": "select", "proxies": ["AUTO", "DIRECT"], "use": ["subscription"]},
        ]
    else:
        result["proxy-groups"] = [{"name": "PROXY", "type": "select", "proxies": ["DIRECT"]}]
    result["rules"].append("MATCH,PROXY")
    return result
