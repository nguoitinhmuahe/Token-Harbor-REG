
from __future__ import annotations

import http.client
import json
import os
import socket
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("FRONT_PORT") or 20130)
HOST = os.environ.get("FRONT_HOST") or "127.0.0.1"
PORTS = [int(p) for p in (os.environ.get("FRONT_BACKENDS") or "20140,20141").split(",")
         if p.strip()]
NAMES = ["a", "b", "c", "d"][:len(PORTS)]
HEALTH_S = float(os.environ.get("FRONT_HEALTH_S") or 3)
TIMEOUT = float(os.environ.get("FRONT_TIMEOUT") or 600)
IDLE = float(os.environ.get("FRONT_IDLE") or 0)
BACKEND_WAIT = float(os.environ.get("FRONT_BACKEND_WAIT") or 25)
LOGP = os.environ.get("FRONT_LOG") or os.path.join(HERE, "logs", "gate_front.log")

SOFT_TEXT = ("\u23f3 Gateway dang qua tai (tam het key kha dung). "
             "Gui lai tin nhan nay giup minh.")

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
       "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length"}

_LOCK = threading.Lock()
_STATE = {
    "active": 0,
    "healthy": [False] * len(PORTS),
    "switches": 0,
    "requests": 0,
    "failovers": 0,
    "errors": 0,
    "client_aborts": 0,
    "started": time.time(),
    "last_switch": 0.0,
}


def log(*parts) -> None:
    line = time.strftime("%H:%M:%S ") + " ".join(str(p) for p in parts)
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOGP), exist_ok=True)
        with open(LOGP, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def backend_stats(i: int, timeout: float = 2.0):
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{PORTS[i]}/gate/stats", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def health_loop() -> None:
    while True:
        try:
            stats = [backend_stats(i) for i in range(len(PORTS))]
            with _LOCK:
                _STATE["healthy"] = [s is not None for s in stats]
                if not _STATE["healthy"][_STATE["active"]]:
                    for i in range(len(PORTS)):
                        if _STATE["healthy"][i]:
                            if i != _STATE["active"]:
                                log(f"backend {NAMES[i]} is healthy again -> "
                                    f"switch from {NAMES[_STATE['active']]}")
                                _STATE["active"] = i
                                _STATE["switches"] += 1
                                _STATE["last_switch"] = time.time()
                            break
        except Exception:
            pass
        time.sleep(HEALTH_S)


class Front(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "gate-front/1.0"

    def log_message(self, fmt, *args):
        pass

    def _read_body(self) -> bytes:
        n = self.headers.get("Content-Length")
        if n:
            try:
                return self.rfile.read(int(n))
            except Exception:
                return b""
        if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
            buf = b""
            while True:
                line = self.rfile.readline().strip()
                if not line:
                    break
                try:
                    size = int(line.split(b";")[0], 16)
                except Exception:
                    break
                if size == 0:
                    self.rfile.readline()
                    break
                buf += self.rfile.read(size)
                self.rfile.readline()
            return buf
        return b""

    def _client_gone(self) -> bool:
        try:
            self.connection.setblocking(False)
            try:
                return self.connection.recv(1, socket.MSG_PEEK) == b""
            except BlockingIOError:
                return False
            except OSError:
                return True
            finally:
                self.connection.setblocking(True)
        except Exception:
            return True

    def _json(self, code: int, obj) -> None:
        out = json.dumps(obj, ensure_ascii=False).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
        except Exception:
            pass

    def _soft(self, method: str, path: str, body: bytes) -> None:
        import os as _os
        anth = path.split("?")[0].rstrip("/").endswith("/messages")
        stream = False
        try:
            jb = json.loads((body or b"").decode("utf-8", "replace"))
            stream = bool(jb.get("stream")) if isinstance(jb, dict) else False
        except Exception:
            stream = b'"stream":true' in (body or b"").replace(b" ", b"")
        if anth:
            mid = "msg_" + _os.urandom(8).hex()
            ev = lambda n, o: ("event: %s\ndata: %s\n\n"
                               % (n, json.dumps(o))).encode()
            if stream:
                out = (ev("message_start", {"type": "message_start", "message": {
                            "id": mid, "type": "message", "role": "assistant",
                            "model": "gate", "content": [], "stop_reason": None,
                            "stop_sequence": None,
                            "usage": {"input_tokens": 0, "output_tokens": 0}}})
                       + ev("content_block_start", {"type": "content_block_start",
                             "index": 0, "content_block": {"type": "text",
                                                           "text": ""}})
                       + ev("content_block_delta", {"type": "content_block_delta",
                             "index": 0,
                             "delta": {"type": "text_delta",
                                       "text": SOFT_TEXT}})
                       + ev("content_block_stop", {"type": "content_block_stop",
                                                    "index": 0})
                       + ev("message_delta", {"type": "message_delta",
                             "delta": {"stop_reason": "end_turn",
                                       "stop_sequence": None},
                             "usage": {"output_tokens": 0}})
                       + ev("message_stop", {"type": "message_stop"}))
            else:
                out = json.dumps({
                    "id": mid, "type": "message", "role": "assistant",
                    "model": "gate", "content": [{"type": "text", "text": SOFT_TEXT}],
                    "stop_reason": "end_turn", "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0}}).encode()
            ct = "text/event-stream" if stream else "application/json"
        else:
            cid = "chatcmpl-" + _os.urandom(8).hex()
            now = int(time.time())
            if stream:
                mk = lambda d: ("data: " + json.dumps(d) + "\n\n").encode()
                out = (mk({"id": cid, "object": "chat.completion.chunk",
                           "created": now, "model": "gate",
                           "choices": [{"index": 0,
                                        "delta": {"role": "assistant"},
                                        "finish_reason": None}]})
                       + mk({"id": cid, "object": "chat.completion.chunk",
                             "created": now, "model": "gate",
                             "choices": [{"index": 0, "delta": {},
                                          "finish_reason": "stop"}]})
                       + b"data: [DONE]\n\n")
                ct = "text/event-stream"
            else:
                out = json.dumps({
                    "id": cid, "object": "chat.completion", "created": now,
                    "model": "gate",
                    "choices": [{"index": 0,
                                 "message": {"role": "assistant", "content": SOFT_TEXT},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                              "total_tokens": 0}}).encode()
                ct = "application/json"
        try:
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
        except Exception:
            pass

    def _order(self):
        with _LOCK:
            act = _STATE["active"]
            healthy = list(_STATE["healthy"])
        first = [act] if healthy[act] else []
        rest = [i for i in range(len(PORTS)) if i != act
                and (healthy[i] or not healthy[act])]
        return first + rest

    def _forward(self, method: str, body: bytes, path: str,
                 _deadline=None) -> bool:
        if _deadline is None:
            _deadline = time.time() + BACKEND_WAIT
        order = self._order()
        phase = ["connect"]
        sent = False
        chunked = False
        for pos, i in enumerate(order):
            conn = None
            try:
                conn = http.client.HTTPConnection("127.0.0.1", PORTS[i], timeout=TIMEOUT)
                conn.putrequest(method, path, skip_host=True)
                for k, v in self.headers.items():
                    if k.lower() in HOP:
                        continue
                    conn.putheader(k, v)
                if body:
                    conn.putheader("Content-Length", str(len(body)))
                conn.endheaders()
                if body:
                    conn.send(body)
                phase[0] = "getresponse"
                resp = conn.getresponse()
                phase[0] = "sendheaders"
                self.send_response(resp.status)
                hdrs = resp.getheaders()
                clen = None
                for k, v in hdrs:
                    lk = k.lower()
                    if lk in ("connection", "keep-alive", "transfer-encoding",
                              "content-length", "te", "trailers", "upgrade"):
                        if lk == "content-length":
                            clen = v
                        continue
                    self.send_header(k, v)
                if clen is not None:
                    self.send_header("Content-Length", clen)
                else:
                    self.send_header("Transfer-Encoding", "chunked")
                    chunked = True
                self.end_headers()
                sent = True
                if IDLE > 0:
                    try:
                        if conn.sock is not None:
                            conn.sock.settimeout(IDLE)
                    except Exception:
                        pass
                if clen is not None:
                    left = int(clen)
                    phase[0] = "stream"
                    while left > 0:
                        chunk = resp.read(min(65536, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)
                    self.wfile.flush()
                else:
                    phase[0] = "stream"
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(b"%x\r\n" % len(chunk))
                        self.wfile.write(chunk)
                        self.wfile.write(b"\r\n")
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                with _LOCK:
                    _STATE["requests"] += 1
                    if pos > 0:
                        _STATE["failovers"] += 1
                return True
            except Exception as e:
                if sent:
                    self.close_connection = True
                    if chunked:
                        try:
                            self.wfile.write(b"0\r\n\r\n")
                            self.wfile.flush()
                        except Exception:
                            pass
                    if phase[0] == "stream" and isinstance(
                            e, (ConnectionAbortedError, ConnectionResetError,
                                BrokenPipeError)):
                        log(f"client hung up mid-stream on {NAMES[i]}")
                        with _LOCK:
                            _STATE["client_aborts"] += 1
                    else:
                        log(f"stream cut from {NAMES[i]}: {type(e).__name__} "
                            f"(phase={phase[0]} {method} {path})")
                        with _LOCK:
                            _STATE["errors"] += 1
                    return True
                if self._client_gone():
                    log(f"client gone while contacting {NAMES[i]}")
                    with _LOCK:
                        _STATE["client_aborts"] += 1
                    return True
                log(f"backend {NAMES[i]} unreachable ({type(e).__name__})"
                    + (f" -> trying {NAMES[order[pos + 1]]}"
                       if pos + 1 < len(order) else " -> giving up"))
                with _LOCK:
                    _STATE["healthy"][i] = False
            finally:
                try:
                    if conn is not None:
                        conn.close()
                except Exception:
                    pass
        if self._client_gone():
            return False
        if time.time() < _deadline:
            time.sleep(1.0)
            log(f"no backend yet -> cho {( _deadline - time.time()):.0f}s nua")
            return self._forward(method, body, path, _deadline)
        with _LOCK:
            _STATE["errors"] += 1
        log("no backend answered (het thoi gian cho) -> tra 200 rong (nuot loi)")
        self._soft(method, path, body)
        return False

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")

    def do_DELETE(self):
        self._route("DELETE")

    def do_OPTIONS(self):
        try:
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,OPTIONS")
            self.send_header("Content-Length", "0")
            self.end_headers()
        except Exception:
            pass

    def _route(self, method: str) -> None:
        path = self.path
        body = self._read_body() if method in ("POST", "PUT", "DELETE") else b""

        if path.startswith("/front/state"):
            with _LOCK:
                s = dict(_STATE)
            s["ports"] = dict(zip(NAMES, PORTS))
            s["active_name"] = NAMES[s["active"]]
            s["uptime_s"] = round(time.time() - s["started"], 0)
            return self._json(200, s)

        if path.startswith("/front/switch"):
            try:
                want = (json.loads(body or b"{}").get("to") or "").strip().lower()
            except Exception:
                want = ""
            if want not in NAMES:
                return self._json(400, {"ok": False, "msg": f"to must be one of {NAMES}"})
            i = NAMES.index(want)
            st = backend_stats(i)
            if st is None:
                return self._json(409, {"ok": False, "msg": f"backend {want} is not answering"})
            with _LOCK:
                old = _STATE["active"]
                _STATE["active"] = i
                _STATE["healthy"][i] = True
                _STATE["switches"] += 1
                _STATE["last_switch"] = time.time()
            log(f"switch {NAMES[old]} -> {want} (requested)")
            return self._json(200, {"ok": True, "active": want})

        if path.startswith("/gate/stats"):
            order = self._order()
            for i in order:
                st = backend_stats(i, timeout=3.0)
                if st is not None:
                    with _LOCK:
                        st["front"] = {"active": NAMES[i], "switches": _STATE["switches"],
                                       "failovers": _STATE["failovers"],
                                       "errors": _STATE["errors"],
                                       "client_aborts": _STATE["client_aborts"],
                                       "healthy": [NAMES[j] for j, h in
                                                   enumerate(_STATE["healthy"]) if h]}
                    return self._json(200, st)
            return self._json(503, {"error": "no backend", "front": "degraded"})

        if path.startswith("/front/health"):
            return self._json(200, {"ok": True})

        return self._forward(method, body, path)


def main() -> int:
    log(f"front {HOST}:{PORT} -> backends "
        + ", ".join(f"{n}:{p}" for n, p in zip(NAMES, PORTS)))
    threading.Thread(target=health_loop, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Front)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
