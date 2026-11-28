"""Explicit proxy downloads and a socket-marked direct fallback on Linux."""
import http.client
import json
import socket
import ssl
import subprocess
from urllib.error import HTTPError, URLError
from urllib.request import HTTPHandler, HTTPSHandler, ProxyHandler, Request, build_opener

# This equals tun.auto-redirect-output-mark, exempting only these sockets.
DIRECT_MARK = 8230


class DownloadError(ValueError):
    def __init__(self, attempts):
        self.details = "，".join(attempts)
        super().__init__("下载失败，当前版本已保留。尝试方式：" + self.details)


def _failure_reason(error):
    # Report the failure type without including a URL, headers, or response body.
    if isinstance(error, URLError):
        error = error.reason
    if isinstance(error, OSError) and isinstance(error.__cause__, OSError):
        error = error.__cause__
    if isinstance(error, socket.gaierror):
        return "DNS 解析失败"
    if isinstance(error, TimeoutError):
        return "连接或读取超时"
    if isinstance(error, ssl.SSLCertVerificationError):
        return "TLS 证书校验失败"
    if isinstance(error, ssl.SSLError):
        return "TLS 握手失败"
    if isinstance(error, (ValueError, http.client.InvalidURL)):
        return "请求格式不正确"
    return "连接或读取失败"


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
        except HTTPError as error:
            reason = f"HTTP {error.code}"
            error.close()
        except (OSError, ValueError, http.client.HTTPException) as error:
            reason = _failure_reason(error)
        else:
            if len(body) <= limit:
                return body, method
            reason = "响应超过大小限制"
        attempted.append(f"{method}（{reason}）")
    raise DownloadError(attempted)
