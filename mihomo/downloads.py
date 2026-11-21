"""Explicit proxy downloads and a socket-marked direct fallback on Linux."""
import http.client
import json
import socket
import subprocess
from urllib.request import HTTPHandler, HTTPSHandler, ProxyHandler, Request, build_opener

# This equals tun.auto-redirect-output-mark, exempting only these sockets.
DIRECT_MARK = 8230


def outbound_interface(family):
    version = "-6" if family == socket.AF_INET6 else "-4"
    result = subprocess.run(["ip", "-json", version, "route", "show", "table", "main", "default"],
                            capture_output=True, text=True, timeout=5, check=True)
    routes = [route for route in json.loads(result.stdout) if route.get("dev") not in {None, "lo", "ha-mihomo"}]
    if not routes:
        raise OSError("No physical default route for direct fallback")
    return min(routes, key=lambda route: route.get("metric", 0))["dev"]


def direct_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
    last_error = None
    for family, kind, protocol, _, endpoint in socket.getaddrinfo(*address, 0, socket.SOCK_STREAM):
        stream = socket.socket(family, kind, protocol)
        try:
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                stream.settimeout(timeout)
            if hasattr(socket, "SO_MARK"):
                stream.setsockopt(socket.SOL_SOCKET, socket.SO_MARK, DIRECT_MARK)
            if hasattr(socket, "SO_BINDTODEVICE"):
                interface = outbound_interface(family)
                stream.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode() + b"\0")
            if source_address:
                stream.bind(source_address)
            stream.connect(endpoint)
            return stream
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            last_error = error
            stream.close()
    raise OSError("No working physical route for direct download") from last_error


class DirectHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = direct_connection


class DirectHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = direct_connection


class DirectHTTPHandler(HTTPHandler):
    def http_open(self, request):
        return self.do_open(DirectHTTPConnection, request)


class DirectHTTPSHandler(HTTPSHandler):
    def https_open(self, request):
        return self.do_open(DirectHTTPSConnection, request, context=self._context)


def opener_for(method, proxy="http://127.0.0.1:17890"):
    if method == "proxy":
        return build_opener(ProxyHandler({"https": proxy, "http": proxy}))
    if method == "direct":
        return build_opener(ProxyHandler({}), DirectHTTPHandler(), DirectHTTPSHandler())
    raise ValueError("Unknown download method")


def fetch(url, policy, limit, *, timeout=60, headers=None, proxy="http://127.0.0.1:17890"):
    methods = ("proxy", "direct") if policy == "auto" else (policy,)
    attempted = []
    for method in methods:
        try:
            request = Request(url, headers={"User-Agent": "haos-mihomo", "Accept-Encoding": "identity", **(headers or {})})
            with opener_for(method, proxy).open(request, timeout=timeout) as response:
                body = response.read(limit + 1)
            if len(body) > limit:
                raise ValueError("Downloaded file exceeds its size limit")
            return body, method
        except (OSError, ValueError):
            attempted.append(method)
    raise ValueError("下载失败，当前版本已保留。尝试方式：" + ", ".join(attempted))
