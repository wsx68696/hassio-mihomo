"""Isolated SOCKS, HTTP and DNS fixtures; also used by the QEMU test."""
import argparse
import json
import os
import select
import socket
import socketserver
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

COUNTS = {}
LOCK = threading.Lock()
DOMAIN_IPS = {"example.test": "198.51.100.11", "provider.test": "198.51.100.12",
              "baidu.com": "198.51.100.13", "www.baidu.com": "198.51.100.13", "direct.test": "198.51.100.14"}


def exact(stream, count):
    value = b""
    while len(value) < count:
        block = stream.recv(count - len(value))
        if not block:
            raise EOFError()
        value += block
    return value


class TCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class SocksHandler(socketserver.BaseRequestHandler):
    def handle(self):
        client = self.request
        client.settimeout(30)
        remote = None
        try:
            version, count = exact(client, 2)
            if version != 5 or 0 not in exact(client, count):
                return
            client.sendall(b"\x05\x00")
            version, command, _, kind = exact(client, 4)
            if version != 5 or command != 1:
                return
            if kind == 1:
                address = socket.inet_ntop(socket.AF_INET, exact(client, 4))
            elif kind == 3:
                address = exact(client, exact(client, 1)[0]).decode()
            elif kind == 4:
                address = socket.inet_ntop(socket.AF_INET6, exact(client, 16))
            else:
                return
            port = struct.unpack("!H", exact(client, 2))[0]
            # The DNS fixture owns this synthetic name. Mihomo may preserve
            # the DNS name when forwarding a request originally addressed by IP.
            requested = address
            if address in {*DOMAIN_IPS, *DOMAIN_IPS.values(), "223.5.5.5"}:
                address = "198.51.100.3"
            if address == "10.0.2.2":
                address = "127.0.0.1"
            remote = socket.create_connection((address, port), timeout=30)
            with LOCK:
                key = f"{address}:{port}"
                COUNTS[key] = COUNTS.get(key, 0) + 1
                if requested != address:
                    key = f"{requested}:{port}"
                    COUNTS[key] = COUNTS.get(key, 0) + 1
            client.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            while True:
                readable, _, _ = select.select([client, remote], [], [], 60)
                if not readable:
                    continue
                for stream in readable:
                    block = stream.recv(65536)
                    if not block:
                        return
                    (remote if stream is client else client).sendall(block)
        except (OSError, EOFError):
            pass
        finally:
            if remote is not None:
                remote.close()


class HTTPHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path == "/subscription":
            host = "10.0.2.2" if self.server.vm else os.environ.get("FIXTURE_PROXY_HOST", "172.30.32.3")
            body = json.dumps({"proxies": [{"name": "fixture", "type": "socks5", "server": host,
                                            "port": 1080, "udp": False}]}).encode()
        elif self.path == "/stats":
            with LOCK:
                body = json.dumps(COUNTS).encode()
        elif self.path == "/bad-subscription":
            body = b'{"proxies": [{"name":"broken", "type":"not-a-real-proxy"}]}'
        elif self.path == "/rule-provider":
            body = b'{"payload":["DOMAIN,provider.test"]}'
        elif self.path == "/bad-rule-provider":
            body = b'{"payload":false}'
        else:
            body = b"haos-mihomo-network-fixture\n"
            if self.client_address[0] == "198.51.100.3":
                body += b"via-socks-fixture\n"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class DNSHandler(socketserver.BaseRequestHandler):
    def handle(self):
        raw, stream = self.request
        end = 12
        labels = []
        while raw[end] != 0:
            labels.append(raw[end + 1:end + 1 + raw[end]].decode())
            end += raw[end] + 1
        end += 5
        question = raw[12:end]
        address = DOMAIN_IPS.get(".".join(labels), "198.51.100.3")
        answer = b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 60, 4) + socket.inet_aton(address)
        header = raw[:2] + struct.pack("!HHHHH", 0x8180, 1, 1, 0, 0)
        stream.sendto(header + question + answer, self.client_address)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--vm", action="store_true")
    args = parser.parse_args()
    bind = "127.0.0.1" if args.vm else "0.0.0.0"
    socks = TCPServer((bind, 1080), SocksHandler)
    threading.Thread(target=socks.serve_forever, daemon=True).start()
    if not args.vm:
        dns = socketserver.ThreadingUDPServer((bind, 5353), DNSHandler)
        threading.Thread(target=dns.serve_forever, daemon=True).start()
    http = ThreadingHTTPServer((bind, 8080), HTTPHandler)
    http.vm = args.vm
    http.serve_forever()


if __name__ == "__main__":
    main()
