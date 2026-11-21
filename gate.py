"""gate.py - the gate (gateway), OpenAI-compatible, placed in front of the router.

Why it is needed: tokenharbor free-tier blocks by NETWORK (IP), not by key.
Measured: 1 IP firing too fast hits 429 "many free accounts are sharing this
network address", then for the next few minutes almost every request 429s ->
your code eats 503s in bulk. router does round-robin over keys but knows
nothing about the IP's budget.

This gate does 3 things:
  1. token bucket: only let through GATE_RPM requests/minute (default 30), small burst.
     -> never push the IP into a blocked state yourself.
  2. on 429: read "next request in N s" / Retry-After -> pause everything
     (stop firing for nothing), then retry a few times before answering.
  3. /gate/stats: count reqs, 429s, how many times it had to wait -> know the real budget.

Run: gate_run.bat, or:
  GATE_MODE=serve python gate.py
Point your code at http://127.0.0.1:20130/v1 instead of ...:20128/v1.

Env:
  GATE_HOST    127.0.0.1
  GATE_PORT    20130
  GATE_UP      http://127.0.0.1:20128     (router)
  GATE_RPM     0      fixed rate (0 = automatic, by quota pool)
  GATE_BURST   4      number of requests fired early
  GATE_RETRY   3      number of retries on 429/503
  GATE_MAXWAIT 40     max seconds to wait when 429ed (caps the delay on 1 req)
  GATE_MAXHOLD 15     cap on the global pause time on 429 (1 key out of
                      quota returning "59 min" will not lock the whole gate for 59 minutes)
  GATE_AUTO    1      derive a safe rate from .pool_quota.json
                      (total_left/7 days, take GATE_AUTO_PCT%)
  GATE_AUTO_PCT 80    use only 80% of the pool's remaining capacity
  GATE_RPM_MAX 100    upper cap on the rate (IP allows a burst of 100/minute)
  GATE_VPN     0      =1: on 429 tell rotator.py to change IP (a new IP =
                      a new network budget). Requires rotator.py running alongside.
  GATE_LOG     logs/gate.log
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
UP = (os.environ.get("GATE_UP") or "http://127.0.0.1:20128").rstrip("/")
HOST = os.environ.get("GATE_HOST") or "127.0.0.1"
PORT = int(os.environ.get("GATE_PORT") or 20130)
RPM = float(os.environ.get("GATE_RPM") or 0)
AUTO = (os.environ.get("GATE_AUTO") or "1").strip().lower() not in ("0", "false", "no", "off")
RPM_MAX = float(os.environ.get("GATE_RPM_MAX") or 100)
AUTO_PCT = float(os.environ.get("GATE_AUTO_PCT") or 80)
QUOTA_JSON = os.path.join(HERE, ".pool_quota.json")
BURST = max(1, int(os.environ.get("GATE_BURST") or 6))
RETRY = max(0, int(os.environ.get("GATE_RETRY") or 3))
MAXWAIT = float(os.environ.get("GATE_MAXWAIT") or 40)
MAXHOLD = float(os.environ.get("GATE_MAXHOLD") or 15)
LOGP = os.environ.get("GATE_LOG") or os.path.join(HERE, "logs", "gate.log")

# Automatic IP rotation: on 429 -> create a request file, rotator.py changes
# region -> new IP. GATE_VPN=0 turns it off.
VPN = (os.environ.get("GATE_VPN") or "0").strip().lower() not in ("0", "false", "no", "off")
VPN_TRIG = os.path.join(HERE, ".vpn_rotate_req")
VPN_SWITCH = os.path.join(HERE, ".vpn_switching")

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
       "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length"}

_lock = threading.Lock()
_tokens = float(BURST)
_last = time.monotonic()
_hold_until = 0.0            # global pause (due to 429)
_stats = {"total": 0, "ok": 0, "err429": 0, "err503": 0, "other": 0,
          "waited_s": 0.0, "throttled": 0, "retried": 0, "started": time.time()}
_auto_rpm: float | None = None
_auto_ts = 0.0


def auto_rpm(now: float) -> float | None:
    """Safe rate from the remaining weekly quota (.pool_quota.json from POOL_MODE=quota)."""
    global _auto_rpm, _auto_ts
    if not AUTO:
        return None
    if now - _auto_ts < 60:
        return _auto_rpm
    _auto_ts = now
    _auto_rpm = None
    try:
        if now - os.path.getmtime(QUOTA_JSON) < 6 * 3600:
            with open(QUOTA_JSON, "r", encoding="utf-8") as f:
                d = json.load(f)
            left = float(d.get("total_left") or 0)
            if left > 0:
                _auto_rpm = left / (7 * 24 * 60) * (AUTO_PCT / 100.0)
    except Exception:
        _auto_rpm = None
    return _auto_rpm


def cur_rpm(now: float) -> float:
    a = auto_rpm(now)
    if RPM > 0:
        base = min(RPM, a) if (a and a > 0) else RPM
    else:
        base = a if (a and a > 0) else (25.0 if AUTO else 0.0)
    return min(base, RPM_MAX) if base > 0 else 0.0


def log(*parts):
    line = time.strftime("%H:%M:%S ") + " ".join(str(p) for p in parts)
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOGP), exist_ok=True)
        with open(LOGP, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def refill() -> float:
    global _tokens, _last
    now = time.monotonic()
    rpm = cur_rpm(time.time())
    if rpm > 0:
        _tokens = min(float(BURST), _tokens + (now - _last) * rpm / 60.0)
    _last = now
    return _tokens


def take_token(deadline: float) -> bool:
    """Wait until a token is available. False = out of patience."""
    global _tokens
    while True:
        rpm = cur_rpm(time.time())
        with _lock:
            if rpm <= 0:
                return True
            if refill() >= 1.0:
                _tokens -= 1.0
                return True
            need = (1.0 - _tokens) * 60.0 / rpm
        if time.monotonic() + need > deadline:
            return False
        time.sleep(min(need, 0.25))


def hold(sec: float) -> None:
    global _hold_until
    with _lock:
        _hold_until = max(_hold_until, time.monotonic() + sec)


def wait_hold(deadline: float) -> float:
    """Wait out the hold. -> seconds waited, -1 = past deadline."""
    waited = 0.0
    while True:
        with _lock:
            left = _hold_until - time.monotonic()
        if left <= 0:
            return waited
        if time.monotonic() + left > deadline:
            return -1.0
        s = min(left, 0.5)
        time.sleep(s)
        waited += s


def wait_vpn(deadline: float) -> bool:
    """If rotator is changing IP (network is bouncing) then wait. False = past deadline."""
    if not os.path.exists(VPN_SWITCH):
        return True
    while os.path.exists(VPN_SWITCH):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.5)
    return True


def ask_rotate() -> None:
    """Tell rotator to change IP (if enabled). Only creates a file, does not change it itself."""
    if not VPN:
        return
    try:
        with open(VPN_TRIG, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
    except Exception:
        pass


def vpn_state() -> dict:
    try:
        with open(os.path.join(HERE, ".vpn_state.json"), "r", encoding="utf-8") as f:
            d = json.load(f)
        return {"vpn_region": d.get("region"), "vpn_ip": d.get("ip"),
                "vpn_switches": d.get("switches"), "vpn_ok": d.get("ok")}
    except Exception:
        return {}


OUT_RX = re.compile(r"(?:in|after)\s+(\d+)\s*s", re.I)
OUT_MIN = re.compile(r"(\d+)\s*min", re.I)


def out_rx_retry() -> int:
    """Retry-After (seconds) for the 429/503 that the GATE itself returns -> the
    client waits the right amount instead of backing off long (Claude often waits 3-4 minutes)."""
    with _lock:
        left = max(0.0, _hold_until - time.monotonic())
    rpm = cur_rpm(time.time())
    need = max(1.0, 60.0 / rpm) if rpm > 0 else 5.0
    return int(max(left, need) + 0.999)


def retry_after(headers, body: bytes) -> float:
    ra = None
    try:
        ra = headers.get("Retry-After")
    except Exception:
        ra = None
    if ra:
        try:
            v = float(str(ra).strip())
            if v > 0:
                return min(v, 600.0)
        except Exception:
            pass
    txt = ""
    try:
        txt = body.decode("utf-8", "replace")
    except Exception:
        pass
    m = OUT_RX.search(txt)
    if m:
        return min(float(m.group(1)), 600.0)
    m = OUT_MIN.search(txt)
    if m:
        return min(float(m.group(1)) * 60.0, 600.0)
    return 3.0


class Gate(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "gate/1.0"

    def log_message(self, fmt, *args):     # disable default logging
        pass

    # ---------------------------------------------------------------- helpers

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

    def _stats_json(self):
        with _lock:
            left = max(0.0, _hold_until - time.monotonic())
            s = dict(_stats)
            s["hold_s"] = round(left, 1)
            s["tokens"] = round(_tokens, 2)
        s["rpm_set"] = RPM
        s["rpm_auto"] = None if _auto_rpm is None else round(_auto_rpm, 2)
        s["rpm_eff"] = round(cur_rpm(time.time()), 2)
        s["auto"] = AUTO
        s["vpn"] = VPN
        s["vpn_switching"] = os.path.exists(VPN_SWITCH)
        s.update(vpn_state())
        s["up"] = UP
        s["uptime_s"] = round(time.time() - s["started"], 0)
        return s

    # ---------------------------------------------------------------- proxy

    def _proxy(self, method: str) -> None:
        body = self._read_body()
        path = self.path
        if path.startswith("/gate/stats"):
            out = json.dumps(self._stats_json(), ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return

        deadline = time.monotonic() + MAXWAIT
        attempt = 0
        last_code = 0
        last_headers = None
        last_body = b""
        metered = path.startswith("/v1")      # only meter the chat path

        with _lock:
            _stats["total"] += 1

        while True:
            attempt += 1
            if metered:
                if not wait_vpn(deadline):
                    with _lock:
                        _stats["other"] += 1
                    self._reply(503, {"Content-Type": "application/json",
                                      "Retry-After": str(out_rx_retry())},
                                json.dumps({"error": {"message":
                                                      "gate: rotating IP, retry later",
                                                      "type": "server_error",
                                                      "code": "vpn_switching"}}).encode())
                    return
                if wait_hold(deadline) < 0:
                    with _lock:
                        _stats["err429"] += 1
                    self._reply(429, {"Content-Type": "application/json",
                                      "Retry-After": str(out_rx_retry())},
                                json.dumps({"error": {"message":
                                                      "gate: out of patience for the network budget",
                                                      "type": "rate_limit_exceeded",
                                                      "code": "rate_limit_exceeded"}}).encode())
                    return
                if not take_token(deadline):
                    with _lock:
                        _stats["throttled"] += 1
                    self._reply(429, {"Content-Type": "application/json",
                                      "Retry-After": str(out_rx_retry())},
                                json.dumps({"error": {"message":
                                                      "gate: over the allowed speed",
                                                      "type": "rate_limit_exceeded",
                                                      "code": "rate_limit_exceeded"}}).encode())
                    return

            req = urllib.request.Request(UP + path, data=body if body else None, method=method)
            for k, v in self.headers.items():
                if k.lower() in HOP:
                    continue
                req.add_header(k, v)
            if body:
                req.add_header("Content-Length", str(len(body)))
            try:
                with urllib.request.urlopen(req, timeout=180) as r:
                    code, hdrs, payload = r.status, r.headers, r
                    if code < 400 or attempt > RETRY:
                        with _lock:
                            _stats["ok" if code < 400 else "other"] += 1
                        self._stream(code, hdrs, payload)
                        return
                    last_code = code
                    last_headers = hdrs
                    last_body = payload.read()
            except urllib.error.HTTPError as e:
                code = e.code
                hdrs = e.headers
                try:
                    payload = e.read()
                except Exception:
                    payload = b""
                if code not in (429, 500, 502, 503, 504) or attempt > RETRY:
                    with _lock:
                        key = {429: "err429", 503: "err503"}.get(code, "other")
                        _stats[key] += 1
                    self._reply(code, dict(hdrs.items()) if hdrs else {}, payload)
                    return
                last_code = code
                last_headers = hdrs
                last_body = payload
            except Exception as e:
                if attempt > RETRY:
                    self._reply(502, {"Content-Type": "application/json",
                                      "Retry-After": str(out_rx_retry())},
                                json.dumps({"error": {"message":
                                                      f"gate: upstream {type(e).__name__}"}}).encode())
                    with _lock:
                        _stats["other"] += 1
                    return
                last_code = 502
                last_headers = None
                last_body = json.dumps({"error": {"message": str(e)}}).encode()

            # 429/5xx -> pause globally then retry
            if metered:
                if last_code == 429:
                    wait = retry_after(last_headers, last_body)
                else:
                    wait = 1.5            # 5xx: temporary error, retry right away
                hold(min(wait, MAXHOLD))
                if last_code in (429, 503):
                    ask_rotate()          # tell rotator to change IP for next time
            else:
                wait = 0.5
            with _lock:
                _stats["retried"] += 1
            log(f"retry {attempt}/{RETRY} after {wait:.0f}s (upstream {last_code})")
            if time.monotonic() + wait > deadline or attempt > RETRY:
                with _lock:
                    key = {429: "err429", 503: "err503"}.get(last_code, "other")
                    _stats[key] += 1
                hh = dict(last_headers.items()) if last_headers else {}
                hh = {k: v for k, v in hh.items() if k.lower() != "retry-after"}
                hh["Retry-After"] = str(out_rx_retry())
                self._reply(last_code or 502, hh, last_body)
                return

    def _reply(self, code: int, headers, body: bytes) -> None:
        try:
            self.send_response(code)
            for k, v in (headers or {}).items():
                if k.lower() in HOP:
                    continue
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            pass

    def _stream(self, code: int, headers, payload) -> None:
        ct = str(headers.get("Content-Type") or "").lower()
        streaming = "text/event-stream" in ct
        clen = headers.get("Content-Length")
        chunked = False
        self.send_response(code)
        for k, v in headers.items():
            if k.lower() in HOP:
                continue
            self.send_header(k, v)
        if clen is not None:
            self.send_header("Content-Length", str(clen))
        elif streaming:
            # SSE: MUST frame it explicitly. Otherwise an HTTP/1.1 keep-alive
            # client (Claude Code) hangs, waiting forever with no idea where the body ends.
            chunked = True
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
        else:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        try:
            if chunked:
                while True:
                    b = payload.read(8192)
                    if not b:
                        break
                    self.wfile.write(b"%x\r\n" % len(b))
                    self.wfile.write(b)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            else:
                while True:
                    b = payload.read(8192)
                    if not b:
                        break
                    self.wfile.write(b)
                    self.wfile.flush()
        except Exception:
            pass

    # ---------------------------------------------------------------- verbs

    def do_GET(self):
        self._proxy("GET")

    def do_POST(self):
        self._proxy("POST")

    def do_PUT(self):
        self._proxy("PUT")

    def do_DELETE(self):
        self._proxy("DELETE")

    def do_OPTIONS(self):
        self._reply(204, {"Access-Control-Allow-Origin": "*",
                          "Access-Control-Allow-Headers": "*",
                          "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS"}, b"")


def main() -> int:
    if not UP.startswith("http"):
        log(f"[x] GATE_UP not valid: {UP}")
        return 2
    srv = ThreadingHTTPServer((HOST, PORT), Gate)
    srv.daemon_threads = True
    eff = cur_rpm(time.time())
    log(f"[ok] gate {HOST}:{PORT} -> {UP} | {RPM:.0f} req/min"
        + (f" (auto {eff:.1f} by quota pool)" if AUTO else "")
        + f", burst {BURST}, retry {RETRY} | stats: http://{HOST}:{PORT}/gate/stats")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        log(f"[i] stopped. {json.dumps({k: (round(v, 1) if isinstance(v, float) else v) for k, v in _stats.items()})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
