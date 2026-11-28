from __future__ import annotations

import json
import os
import random
import socket
import socketserver
import ssl
import sys
import threading
import time

import v6dns

HERE = os.path.dirname(os.path.abspath(__file__))
V6_FILE = os.path.join(HERE, ".v6_addrs")
BASE_PORT = int(os.environ.get("V6PROXY_PORT") or 21000)
LOG_FILE = os.path.join(HERE, "logs", "v6proxy.log")

_lock = threading.Lock()


def log(msg: str) -> None:
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def addrs() -> list:
    try:
        with open(V6_FILE, "r", encoding="utf-8") as f:
            return [l.strip() for l in f if l.strip()]
    except OSError:
        return []


def _pump(a: socket.socket, b: socket.socket) -> None:
    try:
        while True:
            d = a.recv(65536)
            if not d:
                break
            b.sendall(d)
    except OSError:
        pass


def _relay(a: socket.socket, b: socket.socket) -> None:
    t = threading.Thread(target=_pump, args=(b, a), daemon=True)
    t.start()
    _pump(a, b)
    for s in (a, b):
        try:
            s.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    t.join(timeout=5)


class Handler(socketserver.StreamRequestHandler):
    timeout = 90

    def handle(self) -> None:
        try:
            self._handle()
        except Exception as e:
            import traceback
            log("handler ERR %s %s" % (type(e).__name__, str(e)[:120]))
            log(traceback.format_exc()[:400])

    def _handle(self) -> None:
        try:
            line = self.rfile.readline(65536).decode("latin-1").strip()
        except OSError:
            return
        if not line:
            return
        parts = line.split()
        if len(parts) < 3:
            return
        method, target, _ver = parts[0], parts[1], parts[2]
        hdrs = []
        while True:
            h = self.rfile.readline(65536)
            if h in (b"\r\n", b"\n", b""):
                break
            hdrs.append(h)
        src = self.server.src_addr
        if method.upper() == "CONNECT":
            host, _, port = target.rpartition(":")
            try:
                port_i = int(port)
            except ValueError:
                self._err(400, "bad CONNECT")
                return
            try:
                ips = v6dns.resolve_v6(host)
                if not ips:
                    raise OSError("no AAAA")
                up = None
                for ip6 in ips:
                    try:
                        up = socket.create_connection((ip6, port_i), timeout=30,
                                                      source_address=(src, 0))
                        break
                    except OSError:
                        continue
                if up is None:
                    raise OSError("all AAAA failed")
            except OSError as e:
                log("CONNECT %s:%s via %s fail %s" % (host, port, src, e))
                self._err(502, "connect failed")
                return
            self.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            self.wfile.flush()
            _relay(self.connection, up)
            try:
                up.close()
            except OSError:
                pass
            return

        if "://" not in target:
            self._err(400, "bad request")
            return
        scheme, _, rest = target.partition("://")
        if scheme.lower() != "http":
            self._err(501, "only http absolute-uri")
            return
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        port_i = int(port or 80)
        try:
            ips = v6dns.resolve_v6(host)
            if not ips:
                raise OSError("no AAAA")
            up = None
            for ip6 in ips:
                try:
                    up = socket.create_connection((ip6, port_i), timeout=30,
                                                  source_address=(src, 0))
                    break
                except OSError:
                    continue
            if up is None:
                raise OSError("all AAAA failed")
        except OSError as e:
            log("GET %s via %s fail %s" % (host, src, e))
            self._err(502, "connect failed")
            return
        req = "%s /%s HTTP/1.1\r\n" % (method, path)
        self.wfile.write(b"")
        up.sendall(req.encode("latin-1"))
        for h in hdrs:
            up.sendall(h)
        up.sendall(b"\r\n")
        _relay(self.connection, up)
        try:
            up.close()
        except OSError:
            pass

    def _err(self, code: int, msg: str) -> None:
        try:
            self.wfile.write(("HTTP/1.1 %d %s\r\nContent-Length: 0\r\n\r\n"
                              % (code, msg)).encode())
            self.wfile.flush()
        except OSError:
            pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, addr, handler, src_addr):
        self.src_addr = src_addr
        super().__init__(addr, handler)


def proxies() -> list:
    out = []
    for i, a in enumerate(addrs()):
        out.append({"server": "http://127.0.0.1:%d" % (BASE_PORT + i),
                    "addr": a})
    return out


def pick() -> dict | None:
    ps = proxies()
    if not ps:
        return None
    return random.choice(ps)


def main() -> int:
    if "--list" in sys.argv:
        print(json.dumps(proxies()[:5], indent=2))
        return 0
    ps = proxies()
    if not ps:
        print("khong co .v6_addrs")
        return 1
    servers = []
    for p in ps:
        port = int(p["server"].rsplit(":", 1)[1])
        try:
            srv = Server(("127.0.0.1", port), Handler, p["addr"])
        except OSError as e:
            log("bind %d fail %s" % (port, e))
            continue
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        servers.append(srv)
    log("v6proxy up: %d cong %d..%d" % (len(servers), BASE_PORT,
                                        BASE_PORT + len(servers) - 1))
    print("v6proxy up: %d cong" % len(servers))
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
