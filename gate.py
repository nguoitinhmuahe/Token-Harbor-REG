
from __future__ import annotations

import calendar
import concurrent.futures as cf
import hashlib
import http.client
import json
import os
import random
import re
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import reglog
import v6dns

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
UP_TIMEOUT = float(os.environ.get("GATE_UP_TIMEOUT") or 90)
MAXWAIT = float(os.environ.get("GATE_MAXWAIT") or 300)
MAXHOLD = float(os.environ.get("GATE_MAXHOLD") or 15)
LOGP = os.environ.get("GATE_LOG") or os.path.join(HERE, "logs", "gate.log")
LOG = reglog.get("gate", LOGP)
try:
    DUMP_N = int(os.environ.get("GATE_DUMP") or 0)
except ValueError:
    DUMP_N = 0
DUMP_PATH = os.path.join(reglog.LOG_DIR, time.strftime("gate_dump_%Y%m%d_%H%M%S.log"))
_dump_lock = threading.Lock()
_dump_left = DUMP_N
try:
    BODY_CAP = int(os.environ.get("GATE_BODY_CAP") or 800)
except ValueError:
    BODY_CAP = 800

VPN = (os.environ.get("GATE_VPN") or "0").strip().lower() not in ("0", "false", "no", "off")
VPN_TRIG = os.path.join(HERE, ".vpn_rotate_req")
VPN_SWITCH = os.path.join(HERE, ".vpn_switching")
VPN_ARMED = os.path.join(HERE, ".vpn_armed")
VPN_SWITCH_STALE = float(os.environ.get("GATE_VPN_SWITCH_STALE") or 180)
_ROTATE_AT = 0.0
_IP429_UNTIL = 0.0


KEYPOOL_ON = (os.environ.get("GATE_KEYPOOL") or "0").strip().lower() not in \
    ("0", "false", "no", "off")
POOL_UP = (os.environ.get("GATE_POOL_UP") or "https://tokenharbor.ai").rstrip("/")
POOL_PREFIX = (os.environ.get("GATE_POOL_PREFIX") or "a").strip().strip("/")
POOL_TRIES = max(1, int(os.environ.get("GATE_POOL_TRIES") or 24))
POOL_HEDGE = max(1, int(os.environ.get("GATE_HEDGE") or 2))
POOL_RPM = float(os.environ.get("GATE_POOL_RPM") or 30)
POOL_BURST = float(os.environ.get("GATE_POOL_BURST") or 6)
_TB = {"n": 0.0, "last": 0.0}
_TB_LOCK = threading.Lock()


def _tb_wait(max_wait: float = 3.0) -> float:
    rate = POOL_RPM / 60.0
    if rate <= 0:
        return 0.0
    now = time.monotonic()
    with _TB_LOCK:
        if not _TB["last"]:
            _TB["last"] = now
            _TB["n"] = POOL_BURST
        _TB["n"] = min(POOL_BURST, _TB["n"] + (now - _TB["last"]) * rate)
        _TB["last"] = now
        if _TB["n"] >= 1:
            _TB["n"] -= 1
            return 0.0
        need = (1.0 - _TB["n"]) / rate
        _TB["n"] = 0.0
    return min(need, max_wait)
POOL_HEDGE_MAX = max(POOL_HEDGE, int(os.environ.get("GATE_HEDGE_MAX") or 6))
_HEDGE_NOW = POOL_HEDGE
POOL_MAX_CONC = max(1, int(os.environ.get("GATE_POOL_MAX_CONC") or 8))
POOL_UP_SEM = threading.BoundedSemaphore(POOL_MAX_CONC)
POOL_EX = cf.ThreadPoolExecutor(max_workers=POOL_MAX_CONC + 4,
                                thread_name_prefix="pooltry")
_TLS = threading.local()
_POOL_SPLIT = urlsplit(POOL_UP)
_POOL_HOST = _POOL_SPLIT.hostname or "tokenharbor.ai"
_POOL_PORT = _POOL_SPLIT.port or 443
_POOL_BASE = (_POOL_SPLIT.path or "").rstrip("/")

POOL_PROXY_FILE = os.path.join(HERE, ".pool_proxy")
PROXY_AUTO = (os.environ.get("GATE_POOL_PROXY_AUTO") or "0").strip() == "1"
_PROXY_CACHE = {"raw": "", "at": 0.0}


def pool_proxy() -> str:
    env = (os.environ.get("GATE_POOL_PROXY") or "").strip()
    if env:
        return env
    try:
        mt = os.path.getmtime(POOL_PROXY_FILE)
    except OSError:
        return ""
    if _PROXY_CACHE["at"] != mt:
        try:
            with open(POOL_PROXY_FILE, "r", encoding="utf-8") as f:
                _PROXY_CACHE["raw"] = f.read().strip()
        except OSError:
            _PROXY_CACHE["raw"] = ""
        _PROXY_CACHE["at"] = mt
    return _PROXY_CACHE["raw"]


_PROXY_ROT_AT = 0.0
_PROXY_ROT_LOCK = threading.Lock()

V6_FILE = os.path.join(HERE, ".v6_addrs")
V6_MODE = (os.environ.get("GATE_POOL_V6") or "auto").strip().lower()
_V6_CACHE = {"list": [], "at": 0.0}
_SSL_CTX = ssl.create_default_context()


def v6_addrs() -> list:
    try:
        mt = os.path.getmtime(V6_FILE)
    except OSError:
        return []
    if _V6_CACHE["at"] != mt:
        try:
            with open(V6_FILE, "r", encoding="utf-8") as f:
                _V6_CACHE["list"] = [l.strip() for l in f if l.strip()]
        except OSError:
            _V6_CACHE["list"] = []
        _V6_CACHE["at"] = mt
    return _V6_CACHE["list"]


def v6_on() -> bool:
    if V6_MODE in ("0", "false", "no", "off"):
        return False
    return bool(v6_addrs()) and v6_ok()


_V6_OK = {"at": 0.0, "ok": False}
_NET_COOL = {"v6": 0.0, "v4": 0.0}
_NET_COOL_FILE = os.path.join(HERE, ".gate_netcool.json")


def _net_cool_load() -> None:
    try:
        with open(_NET_COOL_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        now = time.time()
        for k, v in (d or {}).items():
            if k in _NET_COOL:
                left = float(v) - now
                if left > 0:
                    _NET_COOL[k] = time.monotonic() + left
    except Exception:
        pass


def _net_cool_save() -> None:
    try:
        now = time.time()
        d = {k: now + max(0.0, v - time.monotonic())
             for k, v in _NET_COOL.items() if v > time.monotonic()}
        with open(_NET_COOL_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f)
    except Exception:
        pass


def net_pick() -> str:
    now = time.monotonic()
    if v6_on() and _NET_COOL["v6"] <= now:
        return "v6"
    if _NET_COOL["v4"] <= now:
        return "v4"
    return "v6" if _NET_COOL["v6"] <= _NET_COOL["v4"] else "v4"


def net_cool_left(net: str) -> float:
    return max(0.0, _NET_COOL.get(net, 0.0) - time.monotonic())


_net_cool_load()


def v6_ok() -> bool:
    now = time.monotonic()
    if now - _V6_OK["at"] < 60:
        return _V6_OK["ok"]
    ok = False
    addrs = v6_addrs()
    if addrs:
        for ip in (v6dns.resolve_v6(_POOL_HOST) or [])[:2]:
            try:
                s = socket.create_connection((ip, _POOL_PORT), 3,
                                             source_address=(addrs[0], 0))
                s.close()
                ok = True
                break
            except OSError:
                ok = False
    _V6_OK["at"] = now
    _prev = _V6_OK.get("ok")
    _V6_OK["ok"] = ok
    if _prev is not None and _prev != ok and addrs:
        LOG.warn("IPv6 %s -> dung %s", "di duoc" if ok else "bi chan (VPN bat)",
                 "IPv6" if ok else "IPv4")
    return ok


_V6_LOCK = threading.Lock()
_V6_LAST = {}
V6_TTL = float(os.environ.get("GATE_V6_TTL") or 20.0)


def v6_take() -> str:
    lst = v6_addrs()
    if not lst:
        return ""
    now = time.monotonic()
    with _V6_LOCK:
        fresh = [a for a in lst if now - _V6_LAST.get(a, 0.0) > V6_TTL]
        if fresh:
            a = fresh[int(now * 1000) % len(fresh)]
        else:
            a = min(lst, key=lambda x: _V6_LAST.get(x, 0.0))
        _V6_LAST[a] = now
        return a


def _conn_v6(src: str):
    ips = v6dns.resolve_v6(_POOL_HOST)
    if not ips:
        raise OSError("khong resolve duoc AAAA cua %s" % _POOL_HOST)
    last = None
    for host6 in ips:
        try:
            s = socket.create_connection((host6, _POOL_PORT), POOL_TIMEOUT,
                                         source_address=(src, 0))
            ss = _SSL_CTX.wrap_socket(s, server_hostname=_POOL_HOST)
            c = http.client.HTTPSConnection(_POOL_HOST, _POOL_PORT,
                                            timeout=POOL_TIMEOUT)
            c.sock = ss
            return c
        except OSError as e:
            last = e
    raise last or OSError("connect v6 failed")


def _conn_v4():
    ips = v6dns.resolve_v4(_POOL_HOST)
    if not ips:
        return http.client.HTTPSConnection(_POOL_HOST, _POOL_PORT,
                                           timeout=POOL_TIMEOUT)
    last = None
    for ip in ips:
        try:
            s = socket.create_connection((ip, _POOL_PORT), POOL_TIMEOUT)
            ss = _SSL_CTX.wrap_socket(s, server_hostname=_POOL_HOST)
            c = http.client.HTTPSConnection(_POOL_HOST, _POOL_PORT,
                                            timeout=POOL_TIMEOUT)
            c.sock = ss
            return c
        except OSError as e:
            last = e
    raise last or OSError("connect v4 failed")


def ask_proxy_rotate(min_gap: float = 60.0) -> None:
    return


def _anth_to_openai(jb):
    if not isinstance(jb, dict):
        return None
    out = {"model": jb.get("model")}
    if jb.get("max_tokens") is not None:
        out["max_tokens"] = jb["max_tokens"]
    if jb.get("temperature") is not None:
        out["temperature"] = jb["temperature"]
    if jb.get("top_p") is not None:
        out["top_p"] = jb["top_p"]
    if jb.get("stop_sequences"):
        out["stop"] = jb["stop_sequences"]
    if jb.get("stream"):
        out["stream"] = True
    msgs = []
    sys = jb.get("system")
    if isinstance(sys, str) and sys:
        msgs.append({"role": "system", "content": sys})
    elif isinstance(sys, list):
        txt = "".join(b.get("text", "") for b in sys
                      if isinstance(b, dict) and b.get("type") == "text")
        if txt:
            msgs.append({"role": "system", "content": txt})
    for m in jb.get("messages") or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role") or "user"
        c = m.get("content")
        if isinstance(c, str):
            msgs.append({"role": role, "content": c})
            continue
        texts, parts, tools = [], [], []
        for b in (c or []):
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text":
                texts.append(b.get("text") or "")
                parts.append({"type": "text", "text": b.get("text") or ""})
            elif t == "image":
                src = b.get("source") or {}
                if src.get("type") == "base64":
                    parts.append({"type": "image_url", "image_url": {"url":
                        "data:%s;base64,%s" % (src.get("media_type", "image/png"),
                                               src.get("data", ""))}})
            elif t == "tool_use":
                tools.append({"id": b.get("id"), "type": "function",
                              "function": {"name": b.get("name"),
                                           "arguments": json.dumps(b.get("input") or {},
                                                                   ensure_ascii=False)}})
            elif t == "tool_result":
                cc = b.get("content")
                if isinstance(cc, list):
                    cc = "".join(x.get("text", "") for x in cc if isinstance(x, dict))
                msgs.append({"role": "tool", "tool_call_id": b.get("tool_use_id"),
                             "content": cc if isinstance(cc, str) else json.dumps(cc)})
        if role == "assistant" and (tools or texts):
            mm = {"role": "assistant", "content": "".join(texts) or None}
            if tools:
                mm["tool_calls"] = tools
            msgs.append(mm)
        elif parts and role != "assistant":
            one = len(parts) == 1 and parts[0].get("type") == "text"
            msgs.append({"role": role, "content": parts[0]["text"] if one else parts})
        elif texts:
            msgs.append({"role": role, "content": "".join(texts)})
    out["messages"] = msgs
    tools = jb.get("tools")
    if isinstance(tools, list) and tools:
        out["tools"] = [{"type": "function", "function": {
            "name": t.get("name"), "description": t.get("description", ""),
            "parameters": t.get("input_schema") or {"type": "object", "properties": {}}}}
            for t in tools if isinstance(t, dict) and t.get("name")]
    tc = jb.get("tool_choice")
    if isinstance(tc, dict):
        typ = tc.get("type")
        out["tool_choice"] = ("auto" if typ == "auto" else
                              "required" if typ == "any" else
                              ({"type": "function", "function": {"name": tc.get("name")}}
                               if typ == "tool" else "auto"))
    return out


_STOP_MAP = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use",
             "content_filter": "end_turn", None: "end_turn"}


def _anth_from_openai(obj, model):
    try:
        ch = (obj.get("choices") or [{}])[0]
    except Exception:
        ch = {}
    msg = ch.get("message") or {}
    content = []
    if msg.get("content"):
        content.append({"type": "text", "text": msg["content"]})
    for tcall in (msg.get("tool_calls") or []):
        fn = tcall.get("function") or {}
        try:
            inp = json.loads(fn.get("arguments") or "{}")
        except Exception:
            inp = {}
        content.append({"type": "tool_use", "id": tcall.get("id"),
                        "name": fn.get("name"), "input": inp})
    u = obj.get("usage") or {}
    return {"id": obj.get("id") or "msg_gate", "type": "message", "role": "assistant",
            "model": model, "content": content or [{"type": "text", "text": ""}],
            "stop_reason": _STOP_MAP.get(ch.get("finish_reason"), "end_turn"),
            "stop_sequence": None,
            "usage": {"input_tokens": int(u.get("prompt_tokens") or 0),
                      "output_tokens": int(u.get("completion_tokens") or 0)}}


def _anth_err(code, payload, msg=""):
    try:
        d = json.loads(payload or b"{}")
        m = (d.get("error") or {}).get("message") or json.dumps(d)[:200]
    except Exception:
        try:
            m = (payload or b"")[:200].decode("utf-8", "replace")
        except Exception:
            m = msg or "upstream error"
    typ = ("rate_limit_error" if code == 429 else
           "authentication_error" if code in (401, 403) else
           "invalid_request_error" if 400 <= code < 500 else "api_error")
    return json.dumps({"type": "error", "error": {"type": typ, "message": m}}).encode()


SOFT_TEXT = ("\u23f3 Gateway dang qua tai (tam het key kha dung). "
             "Gui lai tin nhan nay giup minh.")


SOFT_BLOCK = ("\u26d4 Upstream chan noi dung cua turn nay (content safety). "
              "Thu lai voi noi dung khac giup minh.")


def _soft_openai(model: str, stream: bool, text: str = "") -> bytes:
    text = text or SOFT_TEXT
    now = int(time.time())
    cid = "chatcmpl-" + os.urandom(8).hex()
    if stream:
        def mk(d):
            return ("data: " + json.dumps(d, ensure_ascii=False) + "\n\n").encode()
        return (mk({"id": cid, "object": "chat.completion.chunk", "created": now,
                    "model": model, "choices": [{"index": 0,
                                                 "delta": {"role": "assistant"},
                                                 "finish_reason": None}]})
                + mk({"id": cid, "object": "chat.completion.chunk", "created": now,
                      "model": model,
                      "choices": [{"index": 0,
                                   "delta": {"content": text},
                                   "finish_reason": None}]})
                + mk({"id": cid, "object": "chat.completion.chunk", "created": now,
                      "model": model, "choices": [{"index": 0, "delta": {},
                                                   "finish_reason": "stop"}]})
                + b"data: [DONE]\n\n")
    return json.dumps({
        "id": cid, "object": "chat.completion", "created": now, "model": model,
        "choices": [{"index": 0,
                     "message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }, ensure_ascii=False).encode()


def _soft_anthropic(model: str, stream: bool, text: str = "") -> bytes:
    text = text or SOFT_TEXT
    mid = "msg_" + os.urandom(8).hex()
    if stream:
        def ev(name, obj):
            return ("event: %s\ndata: %s\n\n"
                    % (name, json.dumps(obj, ensure_ascii=False))).encode()
        return (ev("message_start", {"type": "message_start", "message": {
            "id": mid, "type": "message", "role": "assistant", "model": model,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0}}})
            + ev("content_block_start", {"type": "content_block_start", "index": 0,
                                         "content_block": {"type": "text",
                                                           "text": ""}})
            + ev("content_block_delta", {"type": "content_block_delta", "index": 0,
                                         "delta": {"type": "text_delta",
                                                   "text": text}})
            + ev("content_block_stop", {"type": "content_block_stop", "index": 0})
            + ev("message_delta", {"type": "message_delta",
                                   "delta": {"stop_reason": "end_turn",
                                             "stop_sequence": None},
                                   "usage": {"output_tokens": 0}})
            + ev("message_stop", {"type": "message_stop"}))
    return json.dumps({
        "id": mid, "type": "message", "role": "assistant", "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }, ensure_ascii=False).encode()


def _shrink_body(b2: bytes, keep: int):
    try:
        o = json.loads(b2)
    except Exception:
        return None
    msgs = o.get("messages")
    if not isinstance(msgs, list) or len(msgs) <= keep + 1:
        return None
    head = (msgs[:1] if (msgs and isinstance(msgs[0], dict)
                         and msgs[0].get("role") == "system") else [])
    tail = list(msgs[-keep:])
    tail = [m for m in tail
            if isinstance(m, dict) and m.get("role") in ("user", "assistant")
            and not m.get("tool_calls")]
    while tail and tail[-1].get("role") != "user":
        tail = tail[:-1]
    if not tail:
        return None
    o2 = dict(o)
    o2["messages"] = head + tail
    try:
        mt = int(o2.get("max_tokens") or 0)
        if mt > 8192:
            o2["max_tokens"] = 8192
    except Exception:
        pass
    out = json.dumps(o2, ensure_ascii=False).encode()
    return out if len(out) < len(b2) else None


def _cap_msgs(b2: bytes, cap: int):
    try:
        o = json.loads(b2)
    except Exception:
        return None
    msgs = o.get("messages")
    if not isinstance(msgs, list):
        return None
    changed = False
    out = []
    for m in msgs:
        if not isinstance(m, dict):
            out.append(m)
            continue
        c = m.get("content")
        if isinstance(c, str) and len(c) > cap:
            m2 = dict(m)
            m2["content"] = c[:cap] + "\n...[cat bot]"
            changed = True
            out.append(m2)
        elif isinstance(c, list):
            nb = []
            for blk in c:
                if isinstance(blk, dict):
                    if (isinstance(blk.get("text"), str)
                            and len(blk["text"]) > cap):
                        blk = dict(blk)
                        blk["text"] = blk["text"][:cap] + "\n...[cat bot]"
                        changed = True
                    if (isinstance(blk.get("content"), str)
                            and len(blk["content"]) > cap):
                        blk = dict(blk)
                        blk["content"] = blk["content"][:cap] + "\n...[cat bot]"
                        changed = True
                nb.append(blk)
            m2 = dict(m)
            m2["content"] = nb
            out.append(m2)
        else:
            out.append(m)
    if not changed:
        return None
    o2 = dict(o)
    o2["messages"] = out
    res = json.dumps(o2, ensure_ascii=False).encode()
    return res if len(res) < len(b2) else None


def _minimal_body(b2: bytes):
    try:
        o = json.loads(b2)
    except Exception:
        return None
    msgs = o.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return None
    last_user = None
    for m in reversed(msgs):
        if isinstance(m, dict) and m.get("role") == "user":
            last_user = m
            break
    if last_user is None:
        return None
    head = (msgs[:1] if (msgs and isinstance(msgs[0], dict)
                         and msgs[0].get("role") == "system") else [])
    o2 = dict(o)
    o2["messages"] = head + [last_user]
    out = json.dumps(o2, ensure_ascii=False).encode()
    return out if len(out) < len(b2) else None


def _trim_variants(b2: bytes):
    out = []
    cur = b2
    for cap in (40000, 15000, 6000, 2000, 800):
        c = _cap_msgs(cur, cap)
        if c is not None and len(c) < len(cur):
            out.append(("content cap=%d" % cap, c))
            cur = c
    for keep in (40, 24, 12, 6, 3, 1):
        c = _shrink_body(cur, keep)
        if c is not None and len(c) < len(cur):
            out.append(("history keep=%d" % keep, c))
            cur = c
    c = _minimal_body(cur)
    if c is not None and len(c) < len(cur):
        out.append(("chi turn cuoi", c))
    return out


POOL_TIMEOUT = float(os.environ.get("GATE_POOL_TIMEOUT") or 300)
POOL_DEAD_PARK = float(os.environ.get("GATE_POOL_DEAD_PARK") or 900)
POOL_ACCOUNT_PARK = float(os.environ.get("GATE_POOL_ACCOUNT_PARK") or 3600)
POOL_FALLBACK = (os.environ.get("GATE_POOL_FALLBACK") or "deepseek-v4.1-flash:free").strip()
POOL_WAIT = float(os.environ.get("GATE_POOL_WAIT") or 6)
POOL_MAXWAIT = float(os.environ.get("GATE_POOL_MAXWAIT") or 300)
POOL_REST_MAX = float(os.environ.get("GATE_POOL_REST_MAX") or 45)
POOL_FILES = [os.path.join(HERE, "alive_tokens.txt"),
              os.path.join(HERE, "tokens.txt")]
for _extra in (os.environ.get("GATE_TOKEN_FILE") or "").split(";"):
    _extra = _extra.strip()
    if _extra and _extra not in POOL_FILES:
        POOL_FILES.append(_extra)
POOL_FILES = [p for p in POOL_FILES if p]
KEYPARK = os.path.join(HERE, f".gate_keypark_{PORT}.json")
PRUNED = os.path.join(HERE, ".gate_pruned.json")
PRUNED_TXT = os.path.join(HERE, ".gate_pruned.txt")
NOPLAN = os.path.join(HERE, f".gate_noplan_{PORT}.json")
_NO_PLAN: set = set()


def _load_noplan() -> set:
    try:
        with open(NOPLAN, encoding="utf-8") as f:
            return set(json.load(f) or [])
    except Exception:
        return set()
_PRUNED_SEEN: dict = {}
_PRUNED_MTIME = -1.0


def _flagged_now() -> set:
    global _PRUNED_MTIME
    try:
        mt = os.path.getmtime(PRUNED)
    except OSError:
        mt = 0.0
    if mt != _PRUNED_MTIME:
        _PRUNED_MTIME = mt
        _load_pruned()
    now = time.time()
    return {k for k, u in _PRUNED_SEEN.items() if u == 0 or u > now}


def _load_pruned() -> dict:
    out: dict = {}
    try:
        with open(PRUNED, encoding="utf-8", errors="replace") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    d = json.loads(ln)
                    k = d.get("key")
                    u = float(d.get("until") or 0)
                except Exception:
                    k, u = ln, 0.0
                if k:
                    out[k] = u
    except FileNotFoundError:
        pass
    except Exception:
        pass
    try:
        with open(PRUNED_TXT, encoding="utf-8", errors="replace") as f:
            for ln in f:
                k = ln.strip()
                if k and k not in out:
                    out[k] = 0.0
    except FileNotFoundError:
        pass
    except Exception:
        pass
    _PRUNED_SEEN.update(out)
    return out


class KeyPool:

    def __init__(self, paths):
        self.lock = threading.Lock()
        self.items = []
        self.park = {}
        self.used = {}
        self.hits = {}
        self.paths = list(paths)
        self.stamps = {}
        self.checked = 0.0
        self.state_path = os.path.join(HERE, f".gate_poolstate_{PORT}.json")
        self._state_saved = 0.0
        self.load(paths)
        self._load_poolstate()
        self._stamp()

    def _stamp(self):
        try:
            self.stamps = {p: os.path.getmtime(p) for p in self.paths
                           if os.path.exists(p)}
        except Exception:
            self.stamps = {}

    def reload_if_changed(self, every: float = 30.0) -> int:
        now = time.time()
        if now - self.checked < every:
            return 0
        self.checked = now
        try:
            cur = {p: os.path.getmtime(p) for p in self.paths
                   if os.path.exists(p)}
        except Exception:
            return 0
        if cur == self.stamps:
            return 0
        with self.lock:
            before = {kid for kid, _ in self.items}
            self.load(self.paths)
            added = len({kid for kid, _ in self.items} - before)
        self.stamps = cur
        return added


    def load(self, paths):
        seen, items = set(), []
        pruned = _load_pruned()
        _now = time.time()
        for p in paths:
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        k = line.strip()
                        if k.lower().startswith("bearer "):
                            k = k[7:].strip()
                        if not k or k in seen or len(k) < 20 or " " in k:
                            continue
                        u = pruned.get(k)
                        if u is not None and (u == 0 or u > _now):
                            continue
                        seen.add(k)
                        items.append((hashlib.sha1(k.encode()).hexdigest()[:10], k))
            except FileNotFoundError:
                continue
        self.items = items
        for kid, _ in items:
            self.used.setdefault(kid, 0.0)
            self.hits.setdefault(kid, 0)
        try:
            with open(KEYPARK, encoding="utf-8") as f:
                raw = json.load(f)
            now = time.time()
            self.park = {k: float(v) for k, v in (raw.get("park") or {}).items()
                         if float(v) > now}
        except Exception:
            pass

    def _save(self):
        try:
            with open(KEYPARK, "w", encoding="utf-8") as f:
                json.dump({"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                           "park": self.park}, f)
        except Exception:
            pass

    def pick(self):
        added = self.reload_if_changed()
        if added:
            LOG.info("keypool reloaded: +%d key(s), %d total",
                     added, len(self.items))
        now, mono = time.time(), time.monotonic()
        flagged = _flagged_now()
        with self.lock:
            free = [(self.used.get(kid, 0.0), kid, key) for kid, key in self.items
                    if self.park.get(kid, 0) <= now and key not in flagged]
            if not free:
                nxt = min(self.park.values()) if self.park else now + 60
                return None, max(1.0, nxt - now)
            good = [f for f in free if self.hits.get(f[1], 0) > 0]
            pool = good if good else free
            pool.sort()
            _, kid, key = pool[0]
            self.used[kid] = now
            return (kid, key), 0.0

    def park_key(self, kid, secs):
        with self.lock:
            self.park[kid] = time.time() + max(1.0, secs)
            self._save()
        self._save_poolstate()

    def ok(self, kid):
        with self.lock:
            self.hits[kid] = self.hits.get(kid, 0) + 1
        self._save_poolstate()

    def _load_poolstate(self):
        try:
            with open(self.state_path, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            return
        now = time.time()
        for kid, v in (d.get("used") or {}).items():
            try:
                t = float(v)
                if t <= now + 60:
                    self.used[kid] = t
            except Exception:
                pass
        for kid, v in (d.get("hits") or {}).items():
            try:
                self.hits[kid] = int(v)
            except Exception:
                pass

    def _save_poolstate(self, force: bool = False):
        now = time.time()
        if not force and now - self._state_saved < 15:
            return
        self._state_saved = now
        try:
            with self.lock:
                d = {"ts": round(now), "used": self.used, "hits": self.hits}
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f)
            os.replace(tmp, self.state_path)
        except Exception:
            pass

    def flag(self, kid: str, key: str, until: float = 0.0) -> None:
        now = time.time()
        if until and until > now + 5:
            self.park_key(kid, min(until - now, 7 * 86400))
        else:
            with self.lock:
                self.items = [(k, v) for k, v in self.items if k != kid]
                self.park.pop(kid, None)
                self.used.pop(kid, None)
                self.hits.pop(kid, None)
        if key in _PRUNED_SEEN:
            return
        _PRUNED_SEEN[key] = until
        try:
            with open(PRUNED, "a", encoding="utf-8") as f:
                f.write(json.dumps({"key": key, "until": round(until)}) + "\n")
        except Exception:
            pass

    def stats(self):
        now = time.time()
        with self.lock:
            parked = [v for v in self.park.values() if v > now]
            return {"keys": len(self.items), "keys_parked": len(parked),
                    "keys_ok": sum(1 for v in self.hits.values() if v),
                    "key_wait_s": int(max(0.0, min(parked) - now)) if parked else 0}


POOL = KeyPool(POOL_FILES) if KEYPOOL_ON else None

if (KEYPOOL_ON and not pool_proxy()
        and (os.environ.get("GATE_POOL_PROXY_AUTO") or "0") == "1"):
    ask_proxy_rotate(0)


def vpn_on() -> bool:
    return False

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
       "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length"}

_lock = threading.Lock()
_tokens = float(BURST)
_last = time.monotonic()
_hold_until = 0.0
_stats = {"total": 0, "ok": 0, "err429": 0, "err503": 0, "other": 0,
          "waited_s": 0.0, "throttled": 0, "retried": 0, "started": time.time()}
_auto_rpm: float | None = None
_auto_ts = 0.0

_SESS: dict = {}
_SESS_LOCK = threading.Lock()
SESS_MAX = max(1, int(os.environ.get("GATE_SESS") or 200))
SESS_BUF = 256 * 1024
SESS_FILE = os.path.join(reglog.LOG_DIR, ".gate_sessions.json")


def _sess_load() -> None:
    try:
        with open(SESS_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return
    rows = d.get("sessions") if isinstance(d, dict) else d
    if not isinstance(rows, dict):
        return
    with _SESS_LOCK:
        for k, e in rows.items():
            if isinstance(e, dict) and isinstance(e.get("id"), str):
                e.setdefault("reqs", 0)
                _SESS[k] = e
        if len(_SESS) > SESS_MAX:
            for k in sorted(_SESS, key=lambda x: _SESS[x].get("last") or 0,
                            reverse=True)[SESS_MAX:]:
                _SESS.pop(k, None)


_sess_save_ts = 0.0


def _sess_save(force: bool = False) -> None:
    global _sess_save_ts
    now = time.time()
    if not force and now - _sess_save_ts < 5:
        return
    _sess_save_ts = now
    try:
        with _SESS_LOCK:
            data = {"saved": now,
                    "sessions": {k: dict(v) for k, v in _SESS.items()}}
        tmp = SESS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, SESS_FILE)
    except Exception:
        pass


_sess_load()

PROBE_TRIG = os.path.join(HERE, ".gate_probe")
PROBE_FILE = os.path.join(reglog.LOG_DIR, "gate_probe.log")
PROBE_N = 3
_probe_left = PROBE_N if os.path.exists(PROBE_TRIG) else 0
_probe_lock = threading.Lock()


def probe_request(headers, label, body) -> None:
    global _probe_left
    if _probe_left <= 0:
        return
    with _probe_lock:
        if _probe_left <= 0:
            return
        _probe_left -= 1
    try:
        with open(PROBE_FILE, "a", encoding="utf-8", errors="replace") as f:
            f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} {label} =====\n")
            for k, v in (headers.items() if headers else []):
                f.write(f"  {k}: {reglog.scrub(str(v))}\n")
            if body:
                b = (body.decode("utf-8", "replace")
                     if isinstance(body, (bytes, bytearray)) else str(body))
                f.write("  ---- body ----\n  "
                        + reglog.scrub(b)[:8000].replace("\n", "\n  ") + "\n")
    except Exception:
        pass


def auto_rpm(now: float) -> float | None:
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
    line = " ".join(str(p) for p in parts)
    print(time.strftime("%H:%M:%S ") + line, flush=True)
    LOG.line(line)


def _body_json(body):
    if not body:
        return None
    if isinstance(body, dict):
        return body
    try:
        d = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return None
    return d if isinstance(d, dict) else None


def _model_of(body) -> str:
    d = _body_json(body)
    if isinstance(d, dict):
        for k in ("model", "name", "model_id", "modelId"):
            if d.get(k):
                return str(d[k])
    return "-"


_SR = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
_WS = re.compile(r"\s+")


def _msg_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for p in content:
            if isinstance(p, dict) and p.get("text") and p.get("type") in (None, "text"):
                out.append(str(p["text"]))
        return " ".join(out)
    return ""


_SESS_HDRS = ("x-opencode-session-id", "x-opencode-session", "x-session-id",
              "x-session-affinity", "x-conversation-id", "x-claude-session-id",
              "x-thread-id")


def _session_of(jb: dict, headers) -> tuple:
    sid = ""
    if headers:
        for h in _SESS_HDRS:
            v = headers.get(h)
            if v:
                sid = str(v)
                break
    md = jb.get("metadata") if isinstance(jb, dict) else None
    uid = ""
    if isinstance(md, dict):
        uid = str(md.get("user_id") or md.get("userId") or "")
    first = ""
    msgs = jb.get("messages") if isinstance(jb, dict) else None
    if isinstance(msgs, list):
        for m in msgs:
            if isinstance(m, dict) and m.get("role") == "user":
                t = _msg_text(m.get("content"))
                if t.strip():
                    first = t
                    break
    if not first:
        first = _msg_text(jb.get("system") if isinstance(jb, dict) else None)
    first = _WS.sub(" ", _SR.sub(" ", first)).strip()
    title = first[:80]
    if sid:
        return sid, sid, title
    seed = "\x1f".join((uid, first[:1200]))
    key = hashlib.sha1(seed.encode("utf-8", "replace")).hexdigest()[:12]
    return key, (title or uid or "-"), title


_USAGE_RES = (
    ("in", re.compile(r'"input_tokens"\s*:\s*(\d+)')),
    ("out", re.compile(r'"output_tokens"\s*:\s*(\d+)')),
    ("cr", re.compile(r'"cache_read_input_tokens"\s*:\s*(\d+)')),
    ("cw", re.compile(r'"cache_creation_input_tokens"\s*:\s*(\d+)')),
    ("in", re.compile(r'"prompt_tokens"\s*:\s*(\d+)')),
    ("out", re.compile(r'"completion_tokens"\s*:\s*(\d+)')),
    ("in", re.compile(r'"inputTokens"\s*:\s*(\d+)')),
    ("out", re.compile(r'"outputTokens"\s*:\s*(\d+)')),
)


def _usage_scan(buf: bytes) -> tuple:
    if not buf:
        return 0, 0, 0, 0
    txt = buf.decode("utf-8", "replace")
    acc = {"in": 0, "out": 0, "cr": 0, "cw": 0}
    for field, rx in _USAGE_RES:
        for m in rx.finditer(txt):
            try:
                v = int(m.group(1))
            except Exception:
                continue
            if v > acc[field]:
                acc[field] = v
    return acc["in"], acc["out"], acc["cr"], acc["cw"]


def _sess_begin(key: str, label: str, model: str, path: str, nbytes: int,
                title: str = "") -> dict:
    now = time.time()
    with _SESS_LOCK:
        e = _SESS.get(key)
        if e is None:
            e = {"id": key, "label": label, "title": title, "model": model,
                 "path": path, "first": now, "last": now, "reqs": 0, "ok": 0,
                 "err": 0, "in": 0, "out": 0, "cache_r": 0, "cache_w": 0,
                 "bytes_in": 0, "bytes_out": 0, "ms": 0, "tok_s": 0.0,
                 "status": 0}
            _SESS[key] = e
            if len(_SESS) > SESS_MAX:
                drop = min(_SESS.items(), key=lambda kv: kv[1]["last"])[0]
                _SESS.pop(drop, None)
        e["reqs"] += 1
        e["last"] = now
        e["bytes_in"] += nbytes
        if model and model != "-":
            e["model"] = model
    _sess_save()
    return {"key": key, "t0": time.monotonic()}


def _sess_finish(sess, code: int, in_tok: int, out_tok: int,
                 cache_r: int, cache_w: int, bytes_out: int) -> None:
    if not sess:
        return
    with _SESS_LOCK:
        e = _SESS.get(sess["key"])
        if e is None:
            return
        e["ok" if code < 400 else "err"] += 1
        e["in"] += in_tok
        e["out"] += out_tok
        e["cache_r"] += cache_r
        e["cache_w"] += cache_w
        e["bytes_out"] += bytes_out
        elapsed = time.monotonic() - sess["t0"]
        e["ms"] += int(elapsed * 1000)
        e["tok_s"] = round(out_tok / elapsed, 2) if elapsed > 0 else 0.0
        e["last"] = time.time()
        e["status"] = code
    _sess_save()


def _sessions_json() -> dict:
    with _SESS_LOCK:
        rows = [dict(e) for e in sorted(_SESS.values(),
                                        key=lambda x: x["last"], reverse=True)]
    ms = sum(r["ms"] for r in rows)
    out = sum(r["out"] for r in rows)
    return {"count": len(rows), "now": time.time(),
            "totals": {"reqs": sum(r["reqs"] for r in rows),
                       "in": sum(r["in"] for r in rows), "out": out,
                       "tok_s": round(out / max(0.001, ms / 1000), 2)},
            "rows": rows}


def _body_text(body, cap: int = BODY_CAP) -> str:
    if body is None:
        return ""
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8", "replace")
    body = reglog.scrub(str(body)).replace("\n", " ")
    if len(body) > cap:
        body = body[:cap] + f"...(+{len(body) - cap})"
    return body


def dump(direction: str, label: str, headers=None, body=None) -> None:
    global _dump_left
    if DUMP_N <= 0:
        return
    with _dump_lock:
        if _dump_left <= 0:
            return
        _dump_left -= 1
        n = DUMP_N - _dump_left
    try:
        os.makedirs(os.path.dirname(DUMP_PATH), exist_ok=True)
        with open(DUMP_PATH, "a", encoding="utf-8", errors="replace") as f:
            f.write(f"\n===== #{n} {time.strftime('%H:%M:%S')} {direction} {label} =====\n")
            if headers:
                for k, v in dict(headers).items():
                    f.write(f"  {k}: {reglog.scrub(str(v))}\n")
            if body:
                b = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else str(body)
                if len(b) > 20000:
                    b = b[:20000] + f"...(+{len(b) - 20000})"
                f.write("  " + reglog.scrub(b).replace("\n", "\n  ") + "\n")
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


def vpn_switching() -> bool:
    try:
        return (time.time() - os.path.getmtime(VPN_SWITCH)) < VPN_SWITCH_STALE
    except OSError:
        return False


def wait_vpn(deadline: float) -> bool:
    if not vpn_switching():
        return True
    while vpn_switching():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.5)
    return True


def ask_rotate(min_gap: float = 0.0) -> None:
    global _ROTATE_AT
    if not vpn_on():
        return
    now = time.time()
    if min_gap and now - _ROTATE_AT < min_gap:
        return
    _ROTATE_AT = now
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
                return min(v, 60.0)
        except Exception:
            pass
    txt = ""
    try:
        txt = body.decode("utf-8", "replace")
    except Exception:
        pass
    m = OUT_RX.search(txt)
    if m:
        return min(float(m.group(1)), 60.0)
    m = OUT_MIN.search(txt)
    if m:
        return min(float(m.group(1)) * 60.0, 60.0)
    return 3.0


class Gate(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "nine-gate/1.0"

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
        s["vpn"] = vpn_on()
        s["vpn_switching"] = vpn_switching()
        s.update(vpn_state())
        s["up"] = UP
        if POOL is not None:
            s["keypool"] = True
            s["pool_prefix"] = POOL_PREFIX
            s["pool_up"] = POOL_UP
            s["hedge"] = _HEDGE_NOW
            s["max_conc"] = POOL_MAX_CONC
            s.update(POOL.stats())
        s["pool_files"] = [os.path.basename(p) for p in POOL_FILES]
        s["uptime_s"] = round(time.time() - s["started"], 0)
        return s


    def _proxy(self, method: str) -> None:
        try:
            self._proxy_inner(method)
        except Exception as e:
            import traceback
            _tb = traceback.format_exc()
            LOG.warn("req LOI NOI BO %s: %s", type(e).__name__, str(e)[:200])
            LOG.warn(_tb[:1500])
            _anth = self.path.split("?")[0].rstrip("/").endswith("/messages")
            _stream = b'"stream":true' in (b"" if not hasattr(self, "_body_cache")
                                           else self._body_cache).replace(b" ", b"")
            try:
                self._soft(_anth, "-", _stream, "internal %s" % type(e).__name__)
            except Exception:
                pass

    def _proxy_inner(self, method: str) -> None:
        self._sess = None
        body = self._read_body()
        self._body_cache = body
        path = self.path
        if method == "GET" and path.split("?")[0].rstrip("/") in (
                "/v1/models", "/models"):
            ids = [f"{POOL_PREFIX}/deepseek-v4.1-flash:free",
                   f"{POOL_PREFIX}/deepseek-v4-flash:free"]
            out = json.dumps({"object": "list", "data": [
                {"id": i, "object": "model", "owned_by": "tokenharbor"}
                for i in ids]}).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
            except Exception:
                pass
            return

        if path.startswith("/gate/stats"):
            out = json.dumps(self._stats_json(), ensure_ascii=False).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
            except Exception:
                pass
            return

        if path.startswith("/gate/sessions"):
            out = json.dumps(_sessions_json(), ensure_ascii=False).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
            except Exception:
                pass
            return

        deadline = time.monotonic() + MAXWAIT
        attempt = 0
        last_code = 0
        last_headers = None
        last_body = b""
        metered = path.startswith("/v1")

        t0 = time.monotonic()
        jb = _body_json(body) if metered else None
        model = _model_of(jb) if metered else "-"
        label = f"{method} {path}" + (f" model={model}" if metered else "")
        _an = re.sub(r"^/v1/v1(?=/|$)", "/v1", path).startswith("/v1/messages")
        _st = bool((jb or {}).get("stream"))
        with _lock:
            _stats["total"] += 1
            rid = _stats["total"]
        sess = None
        if metered and jb is not None:
            skey, slabel, stitle = _session_of(jb, self.headers)
            sess = _sess_begin(skey, slabel, model, path, len(body or b""), stitle)
        self._sess = sess
        LOG.debug("req#%d %s len=%d", rid, label, len(body or b""))
        dump(">> REQUEST", f"#{rid} {label}", self.headers, body)
        probe_request(self.headers, f"{method} {path}", body)

        if POOL is not None and metered and model.startswith(POOL_PREFIX + "/"):
            _up = re.sub(r"^/v1/v1(?=/|$)", "/v1", path)
            _anth = _anth_to_openai(jb) if _up.startswith("/v1/messages") else None
            if "/chat/completions" in _up or _anth is not None:
                self._pool_proxy(method, path, body, jb, model, rid, label, t0,
                                 anth=_anth)
                return

        while True:
            attempt += 1
            if metered:
                if not wait_vpn(deadline):
                    with _lock:
                        _stats["other"] += 1
                    LOG.warn("req#%d %s -> nuot loi vpn_switching (>%.0fs, no IP in time)",
                             rid, label, MAXWAIT)
                    self._soft(_an, model, _st, "vpn switching")
                    return
                if wait_hold(deadline) < 0:
                    with _lock:
                        _stats["err429"] += 1
                    LOG.warn("req#%d %s -> nuot loi hold timeout (>%.0fs global cooldown)",
                             rid, label, MAXWAIT)
                    self._soft(_an, model, _st, "hold timeout")
                    return
                if not take_token(deadline):
                    with _lock:
                        _stats["throttled"] += 1
                    LOG.warn("req#%d %s -> nuot loi throttled (rpm=%.1f, waited %.1fs)",
                             rid, label, cur_rpm(time.time()), time.monotonic() - t0)
                    self._soft(_an, model, _st, "throttled")
                    return

            req = urllib.request.Request(UP + path, data=body if body else None, method=method)
            for k, v in self.headers.items():
                if k.lower() in HOP:
                    continue
                req.add_header(k, v)
            if body:
                req.add_header("Content-Length", str(len(body)))
            try:
                with urllib.request.urlopen(req, timeout=UP_TIMEOUT) as r:
                    code, hdrs, payload = r.status, r.headers, r
                    if code < 400 or attempt > RETRY:
                        with _lock:
                            _stats["ok" if code < 400 else "other"] += 1
                        if code < 400:
                            LOG.info("req#%d %s -> %d ok (attempts %d, %.0fms)",
                                     rid, label, code, attempt, (time.monotonic() - t0) * 1000)
                            dump("<< RESPONSE", f"#{rid} {label}", hdrs, None)
                        else:
                            LOG.warn("req#%d %s -> %d passthrough (attempts %d, %.0fms)",
                                     rid, label, code, attempt, (time.monotonic() - t0) * 1000)
                        try:
                            self._stream(code, hdrs, payload)
                        except Exception as se:
                            LOG.warn("req#%d %s stream cut %s (no retry)", rid, label,
                                     type(se).__name__)
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
                    LOG.warn("req#%d %s -> %d (nuot -> 200 rong, attempts %d, %.0fms) | body=%s",
                             rid, label, code, attempt, (time.monotonic() - t0) * 1000,
                             _body_text(payload))
                    dump("<< RESPONSE", f"#{rid} {label}", hdrs, payload)
                    if not self._pool_fb(method, path, body, jb, model, rid,
                                         label, t0, an=_an, why="upstream %d" % code):
                        self._soft(_an, model, _st, "upstream %d" % code)
                    return
                last_code = code
                last_headers = hdrs
                last_body = payload
            except Exception as e:
                if attempt > RETRY:
                    LOG.exc("req#%d %s upstream %s (attempt %d)", rid, label,
                            type(e).__name__, attempt)
                    if not self._pool_fb(method, path, body, jb, model, rid,
                                         label, t0, an=_an,
                                         why="upstream %s" % type(e).__name__):
                        with _lock:
                            _stats["other"] += 1
                        self._soft(_an, model, _st,
                                   "upstream %s" % type(e).__name__)
                    return
                last_code = 502
                last_headers = None
                last_body = json.dumps({"error": {"message": str(e)}}).encode()

            if metered:
                if last_code == 429:
                    wait = retry_after(last_headers, last_body)
                else:
                    wait = min(1.5 * (2 ** (attempt - 1)), 12.0)
                hold(min(wait, MAXHOLD))
                if last_code in (429, 503):
                    ask_rotate()
            else:
                wait = 0.5
            with _lock:
                _stats["retried"] += 1
            log(f"retry {attempt}/{RETRY} sau {wait:.0f}s (upstream {last_code})")
            LOG.info("req#%d %s retry %d/%d wait=%.0fs upstream=%s | body=%s",
                     rid, label, attempt, RETRY, wait, last_code, _body_text(last_body))
            if time.monotonic() + wait > deadline or attempt > RETRY:
                with _lock:
                    key = {429: "err429", 503: "err503"}.get(last_code, "other")
                    _stats[key] += 1
                hh = dict(last_headers.items()) if last_headers else {}
                hh = {k: v for k, v in hh.items() if k.lower() != "retry-after"}
                hh["Retry-After"] = str(out_rx_retry())
                LOG.warn("req#%d %s GAVE UP -> %d after %d attempts, %.0fs (nuot loi) | body=%s",
                         rid, label, last_code or 502, attempt,
                         time.monotonic() - t0, _body_text(last_body))
                dump("<< GIVEUP", f"#{rid} {label}", last_headers, last_body)
                if not self._pool_fb(method, path, body, jb, model, rid, label,
                                     t0, an=_an,
                                     why="gave up %s" % (last_code or 502)):
                    self._soft(_an, model, _st, "gave up %s" % (last_code or 502))
                return

    def _pool_fb(self, method, path, body, jb, model, rid, label, t0, an=False,
                 why=""):
        if POOL is None or not POOL_FALLBACK:
            return False
        if body and (b'"image_url"' in body or b'image/png' in body
                     or b'image/jpeg' in body):
            return False
        fb = POOL_PREFIX + "/" + POOL_FALLBACK
        LOG.warn("req#%d %s upstream failed (%s) -> pool fallback %s",
                 rid, label, why, fb)
        try:
            anth = _anth_to_openai(jb) if (an and jb is not None) else None
            self._pool_proxy(method, path, body, jb, fb, rid, label, t0,
                             anth=anth)
            return True
        except Exception as e:
            LOG.warn("req#%d %s pool fallback error %s", rid, label,
                     type(e).__name__)
            return False

    def _direct_fallback(self, method, path, body, rid, label):
        up_path = re.sub(r"^/v1/v1(?=/|$)", "/v1", path)
        req = urllib.request.Request(UP + up_path,
                                     data=body if body else None,
                                     method=method)
        for k, v in self.headers.items():
            if k.lower() in HOP:
                continue
            req.add_header(k, v)
        if body:
            req.add_header("Content-Length", str(len(body)))
        try:
            with urllib.request.urlopen(req, timeout=UP_TIMEOUT) as r:
                if r.status >= 400:
                    return False
                self._stream(r.status, r.headers, r)
                LOG.info("req#%d %s -> direct fallback %d", rid, label, r.status)
                return True
        except Exception as e:
            LOG.warn("req#%d %s direct fallback %s", rid, label,
                     type(e).__name__)
            return False

    def _pool_proxy(self, method, path, body, jb, model, rid, label, t0, anth=None):
        global _IP429_UNTIL, _HEDGE_NOW
        """Gui thang len POOL_UP bang key trong pool; 429 thi park + doi key ngay.

        anth != None: day la /v1/messages da dich sang OpenAI; tra loi dich nguoc
        ve dang Anthropic.
        """
        up_path = re.sub(r"^/v1/v1(?=/|$)", "/v1", path)
        is_anth = anth is not None
        want_stream = bool((jb or {}).get("stream"))
        is_msgs = up_path.startswith("/v1/messages")
        max_tries = POOL_TRIES
        skips = 0
        if is_msgs and not _NO_PLAN:
            _NO_PLAN.update(_load_noplan())
        b2 = body
        if anth is not None:
            up_path = "/v1/chat/completions"
            is_msgs = False
            max_tries = POOL_TRIES
            try:
                anth = dict(anth)
                anth["model"] = re.sub(r"\[[^\]]*\]\s*$", "",
                                       model.split("/", 1)[1]).strip()
                b2 = json.dumps(anth, ensure_ascii=False).encode()
            except Exception:
                anth = None
        jb2 = None
        fb_done = False
        _has_img = bool(body) and (b'"image_url"' in body or b'"image"' in body
                                   or b'image/png' in body or b'image/jpeg' in body)
        if jb is not None and anth is None:
            try:
                jb2 = dict(jb)
                _m = model.split("/", 1)[1]
                _m = re.sub(r"\[[^\]]*\]\s*$", "", _m).strip()
                jb2["model"] = _m
                b2 = json.dumps(jb2, ensure_ascii=False).encode()
            except Exception:
                b2 = body
        _net = net_pick()
        _net_cool = net_cool_left(_net)
        hedge = int(os.environ.get("GATE_HEDGE") or 2)
        hedge_max = int(os.environ.get("GATE_HEDGE_MAX") or 3)
        _HEDGE_NOW = hedge
        long_hedge = hedge
        local_hedge = hedge
        tries = 0
        waits = 0
        _nap = 1.0
        _ext = 0
        deadline = t0 + POOL_MAXWAIT
        inflight: dict = {}
        fatal = None
        _shrunk = 0
        _variants = None
        _vstage = 0
        _blk = 0
        ex = POOL_EX

        def _attempt(_kid, _key):
            hdrs = {}
            for _k, _v in self.headers.items():
                _lk = _k.lower()
                if _lk in HOP or _lk in ("authorization", "host", "content-length",
                                         "accept-encoding"):
                    continue
                hdrs[_k] = _v
            hdrs["Authorization"] = "Bearer " + _key
            hdrs["Host"] = _POOL_HOST
            if b2:
                hdrs["Content-Length"] = str(len(b2))
            _path = _POOL_BASE + up_path
            _last = None
            _v6 = (_net == "v6")
            if _v6:
                conn = None
                _TLS.conn = None
            elif vpn_switching():
                conn = None
                _TLS.conn = None
            else:
                conn = getattr(_TLS, "conn", None)
            for _t in range(2):
                try:
                    if _v6:
                        try:
                            if conn is not None:
                                conn.close()
                        except Exception:
                            pass
                        conn = _conn_v6(v6_take())
                    _px = "" if _v6 else pool_proxy()
                    if getattr(_TLS, "px", "") != _px:
                        try:
                            if conn is not None:
                                conn.close()
                        except Exception:
                            pass
                        conn = None
                        _TLS.conn = None
                        _TLS.px = _px
                    if conn is None:
                        if _px:
                            _ph, _, _pp = _px.partition(":")
                            conn = http.client.HTTPSConnection(
                                _ph, int(_pp or 80), timeout=POOL_TIMEOUT)
                            conn.set_tunnel(_POOL_HOST, _POOL_PORT)
                        else:
                            conn = _conn_v4()
                    conn.request(method, _path, body=(b2 if b2 else None),
                                 headers=hdrs)
                    r = conn.getresponse()
                    if r.status == 200:
                        _TLS.conn = None
                        return ("ok", _kid, r, r.status, None, None)
                    _p = b""
                    try:
                        _p = r.read()
                    except Exception:
                        pass
                    if not _v6:
                        _TLS.conn = conn
                    return ("http", _kid, _key, r.status,
                            dict(r.getheaders()), _p)
                except Exception as e:
                    _last = e
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = None
                    _TLS.conn = None
                    time.sleep(0.2)
            return ("net", _kid, None, 0, None, _last)

        def _reap(futs):
            for f2, k2 in futs.items():
                try:
                    res = f2.result(timeout=POOL_TIMEOUT + 5)
                    if res[0] == "ok" and res[2] is not None:
                        try:
                            res[2].close()
                        except Exception:
                            pass
                        POOL.ok(k2)
                    elif res[0] == "http" and res[3] == 429:
                        _pb = (res[5] or b"")[:300].decode("utf-8", "replace").lower()
                        if "network address" in _pb or "sharing this network" in _pb:
                            if PROXY_AUTO:
                                ask_proxy_rotate(60)
                        else:
                            POOL.park_key(k2, retry_after(res[4] or {}, res[5] or b"") + 2)
                except Exception:
                    pass

        try:
            while tries < POOL_TRIES and time.monotonic() < deadline:
                if net_cool_left(_net) > 0:
                    _other = "v4" if _net == "v6" else "v6"
                    if net_cool_left(_other) <= 0:
                        _net = _other
                        LOG.warn("req#%d %s doi network %s (het cooldown)",
                                 rid, label, _net)
                    else:
                        if self._direct_fallback(method, path, body, rid, label):
                            return
                        self._soft(is_anth, model, want_stream,
                                   "both networks cooling")
                        return
                if _IP429_UNTIL > time.monotonic() and not inflight:
                    if self._direct_fallback(method, path, body, rid, label):
                        return
                    self._soft(is_anth, model, want_stream, "network cooling")
                    return
                while len(inflight) < local_hedge and tries < max_tries:
                    got = POOL_UP_SEM.acquire(timeout=0.2 if inflight else 5.0)
                    if not got:
                        break
                    sel, wait_all = POOL.pick()
                    if sel is None:
                        POOL_UP_SEM.release()
                        break
                    tries += 1
                    k1, key1 = sel
                    if is_msgs and k1 in _NO_PLAN and skips < 200:
                        POOL_UP_SEM.release()
                        skips += 1
                        continue
                    _w = _tb_wait(2.0)
                    if _w:
                        time.sleep(_w)
                    fut1 = ex.submit(_attempt, k1, key1)
                    fut1.add_done_callback(lambda _f: POOL_UP_SEM.release())
                    inflight[fut1] = k1

                if not inflight:
                    if net_cool_left(_net) > 0:
                        _other = "v4" if _net == "v6" else "v6"
                        if net_cool_left(_other) <= 0:
                            _net = _other
                            LOG.warn("req#%d %s doi network %s (het cooldown)",
                                     rid, label, _net)
                            continue
                        _soon = min(net_cool_left("v6"), net_cool_left("v4"))
                        if _soon:
                            if self._direct_fallback(method, path, body, rid, label):
                                return
                            self._soft(is_anth, model, want_stream,
                                       "both networks cooling")
                            return
                    if self._direct_fallback(method, path, body, rid, label):
                        return
                    if waits % 8 == 0:
                        ask_rotate(45)
                    if time.monotonic() + 0.5 < deadline and tries < POOL_TRIES:
                        waits += 1
                        time.sleep(0.5)
                        continue
                    with _lock:
                        _stats["err429"] += 1
                        _stats["pool_empty"] = _stats.get("pool_empty", 0) + 1
                    LOG.warn("req#%d %s pool empty (%ds left)", rid, label, int(wait_all))
                    self._soft(is_anth, model, want_stream,
                               "pool empty, %ds left" % int(wait_all))
                    return

                done, _pending = cf.wait(list(inflight),
                                         timeout=max(0.5, min(deadline - time.monotonic(),
                                                              20.0)),
                                         return_when=cf.FIRST_COMPLETED)
                if not done:
                    break
                for fut in done:
                    kid = inflight.pop(fut)
                    try:
                        res = fut.result()
                    except Exception as e:
                        res = ("net", kid, None, 0, None, e)

                    if res[0] == "ok":
                        _, kid, r, status, _h, _p = res
                        POOL.ok(kid)
                        with _lock:
                            _stats["ok"] += 1
                            _stats["pool_ok"] = _stats.get("pool_ok", 0) + 1
                            _stats["pool_swaps"] = _stats.get("pool_swaps", 0) + max(0, tries - 1)
                        LOG.info("req#%d %s -> %d ok key=%s (try %d, %.0fms)",
                                 rid, label, status, kid, tries,
                                 (time.monotonic() - t0) * 1000)
                        if inflight:
                            threading.Thread(target=_reap, args=(dict(inflight),),
                                             daemon=True).start()
                            inflight = {}
                        try:
                            if anth is not None:
                                self._stream_anthropic(status, r.headers, r, model,
                                                       want_stream)
                            else:
                                self._stream(status, r.headers, r)
                        except Exception as se:
                            LOG.warn("req#%d %s stream cut %s (khong retry)", rid, label,
                                     type(se).__name__)
                        return

                    if res[0] == "http":
                        _, kid, key, code, hdrs, payload = res
                        if code == 429:
                            wait = retry_after(hdrs or {}, payload or b"")
                            _b429 = (payload or b"")[:300].decode("utf-8", "replace").lower()
                            _ip429 = ("network address" in _b429
                                      or "sharing this network" in _b429)
                            with _lock:
                                _stats["err429"] += 1
                                _stats["retried"] += 1
                                _stats["pool_429"] = _stats.get("pool_429", 0) + 1
                            if _ip429:
                                _cool = max(15.0, min(float(wait or 0) + 2.0, 180.0))
                                _NET_COOL[_net] = max(_NET_COOL.get(_net, 0.0),
                                                      time.monotonic() + _cool)
                                _net_cool_save()
                                _IP429_UNTIL = max(_IP429_UNTIL,
                                                   time.monotonic() + 5.0)
                                tries = max(0, tries - 1)
                                ask_rotate(45)
                                if PROXY_AUTO:
                                    ask_proxy_rotate(60)
                                with _lock:
                                    _stats["ip429_cool"] = _stats.get("ip429_cool", 0) + 1
                                LOG.warn("req#%d %s key=%s 429 THEO IP/net=%s "
                                         "(khong park key) -> cool %.0fs (Retry-After)",
                                         rid, label, kid, _net, _cool)
                                _other = "v4" if _net == "v6" else "v6"
                                if self._direct_fallback(method, path, body, rid, label):
                                    return
                                if net_cool_left(_other) <= 0:
                                    _net = _other
                                    LOG.warn("req#%d %s doi network %s",
                                             rid, label, _net)
                                continue
                            else:
                                POOL.park_key(kid, wait + 2)
                                LOG.warn("req#%d %s key=%s 429 -> park %.0fs, "
                                         "doi key (try %d)", rid, label, kid, wait, tries)
                                if tries >= 6:
                                    _nap = min(_nap * 2, 20.0)
                                    if time.monotonic() + _nap < deadline:
                                        LOG.warn("req#%d %s %d key 429 -> nghi %.0fs "
                                                 "cho park het roi thu lai",
                                                 rid, label, tries, _nap)
                                        time.sleep(_nap)
                                        tries = 0
                                        continue
                            continue
                        if code in (500, 502, 503, 504):
                            with _lock:
                                _stats["retried"] += 1
                            continue
                        if code in (401, 403):
                            _b = ""
                            try:
                                _b = (payload or b"")[:400].decode("utf-8", "replace")
                            except Exception:
                                pass
                            _low = _b.lower()
                            if code == 403 and ("from your region" in _low
                                                or "region is not available" in _low
                                                or "under sanctions" in _low):
                                ask_rotate(20)
                                with _lock:
                                    _stats["region_block"] = _stats.get("region_block", 0) + 1
                                    _stats["retried"] += 1
                                    _rh = _stats["region_block"]
                                LOG.warn("req#%d %s key=%s 403 REGION BLOCK -> doi IP,"
                                         " giu key | %s", rid, label, kid, _b[:90])
                                if _rh >= 2:
                                    self._soft(is_anth, model, want_stream,
                                               "region blocked")
                                    return
                                continue
                            if code == 403 and ("content-policy" in _b
                                                or "paused for this account" in _b
                                                or "refused requests from accounts like this" in _b):
                                _u = 0.0
                                _m = re.search(r"until (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", _b)
                                if _m:
                                    try:
                                        _u = calendar.timegm(time.strptime(
                                            _m.group(1), "%Y-%m-%dT%H:%M:%S"))
                                    except Exception:
                                        _u = 0.0
                                if POOL is not None:
                                    POOL.flag(kid, key, _u)
                                with _lock:
                                    _stats["retried"] += 1
                                    _stats["pool_pruned"] = _stats.get("pool_pruned", 0) + 1
                                LOG.warn("req#%d %s key=%s BI FLAG content-policy -> %s | %s",
                                         rid, label, kid,
                                         ("park toi %s" % time.strftime(
                                             "%m-%d %H:%M", time.gmtime(_u))) if _u else "xoa",
                                         _b[:110])
                                continue
                            if code == 403 and ("free_models_disabled" in _b
                                                or "free models are turned off" in _low
                                                or "verify your email" in _low):
                                POOL.park_key(kid, POOL_ACCOUNT_PARK)
                                with _lock:
                                    _stats["retried"] += 1
                                    _stats["pool_account"] = _stats.get("pool_account", 0) + 1
                                LOG.warn("req#%d %s key=%s can chu tk -> park %.0fs | %s",
                                         rid, label, kid, POOL_ACCOUNT_PARK, _b[:110])
                                continue
                            if code == 403 and ("paused for your account" in _b
                                                or "model_unavailable_for_account" in _b):
                                if not fb_done and POOL_FALLBACK and not _has_img:
                                    fb_done = True
                                    try:
                                        jb2 = dict(jb2 or jb or {})
                                        jb2["model"] = POOL_FALLBACK
                                        b2 = json.dumps(jb2, ensure_ascii=False).encode()
                                    except Exception:
                                        pass
                                    LOG.warn("req#%d %s key=%s model paused -> fallback %s",
                                             rid, label, kid, POOL_FALLBACK)
                                    continue
                                POOL.park_key(kid, 300)
                                with _lock:
                                    _stats["retried"] += 1
                                    _stats["pool_paused"] = _stats.get("pool_paused", 0) + 1
                                LOG.warn("req#%d %s key=%s model paused, park 300s | %s",
                                         rid, label, kid, _b[:120])
                                continue
                            POOL.park_key(kid, POOL_DEAD_PARK)
                            with _lock:
                                _stats["retried"] += 1
                                _stats["pool_dead"] = _stats.get("pool_dead", 0) + 1
                            LOG.warn("req#%d %s key=%s -> %d park %.0fs | %s",
                                     rid, label, kid, code, POOL_DEAD_PARK, _b[:150])
                            continue
                        if code == 402 and is_msgs:
                            _NO_PLAN.add(kid)
                            try:
                                with open(NOPLAN, "w", encoding="utf-8") as _f:
                                    json.dump(sorted(_NO_PLAN), _f)
                            except Exception:
                                pass
                            with _lock:
                                _stats["retried"] += 1
                                _stats["pool_noplan"] = _stats.get("pool_noplan", 0) + 1
                            LOG.warn("req#%d %s key=%s 402 (khong co plan cho /v1/messages)"
                                     " -> bo qua key nay", rid, label, kid)
                            continue
                        _bt = ""
                        try:
                            _bt = (payload or b"")[:600].decode(
                                "utf-8", "replace")
                        except Exception:
                            pass
                        _btl = _bt.lower()
                        _blocked = ("content safety review" in _btl
                                    or "blocked by" in _btl)
                        if code in (400, 413):
                            if _variants is None:
                                _variants = _trim_variants(b2)
                            if _vstage < len(_variants):
                                _lbl, _nb = _variants[_vstage]
                                _vstage += 1
                                _shrunk += 1
                                LOG.warn("req#%d %s key=%s -> %d: cat %s "
                                         "(%d->%d byte) roi thu lai",
                                         rid, label, kid, code, _lbl,
                                         len(b2), len(_nb))
                                b2 = _nb
                                continue
                            if _blocked:
                                _blk += 1
                                with _lock:
                                    _stats["blocked"] = _stats.get("blocked", 0) + 1
                                if _blk >= 20:
                                    LOG.warn("req#%d %s 400 content-block tren %d key "
                                             "-> bao client", rid, label, _blk)
                                    self._soft(is_anth, model, want_stream,
                                               "content blocked", SOFT_BLOCK)
                                    return
                                LOG.warn("req#%d %s key=%s 400 content-block "
                                         "(het phien ban cat) -> doi key (lan %d)",
                                         rid, label, kid, _blk)
                                continue
                        with _lock:
                            _stats["other"] += 1
                        LOG.warn("req#%d %s key=%s -> %d (nuot, tra 200 rong) | %s",
                                 rid, label, kid, code, _bt[:200])
                        self._soft(is_anth, model, want_stream,
                                   "upstream %d" % code)
                        return

                    with _lock:
                        _stats["pool_net"] = _stats.get("pool_net", 0) + 1
                    LOG.warn("req#%d %s upstream %s (try %d) -> next key",
                             rid, label, type(res[5]).__name__, tries)
                    continue

                if fatal is not None and not inflight:
                    code = fatal[0]
                    LOG.warn("req#%d %s -> %d (het key, nuot -> 200 rong)",
                             rid, label, code)
                    self._soft(is_anth, model, want_stream,
                               "fatal %d, out of keys" % code)
                    return
                if local_hedge < hedge_max:
                    local_hedge = min(local_hedge + 2, hedge_max)
        finally:
            pass

        if fatal is not None:
            self._soft(is_anth, model, want_stream,
                       "fatal %d at end" % fatal[0])
            return
        with _lock:
            _stats["other"] += 1
        if self._direct_fallback(method, path, body, rid, label):
            return
        LOG.warn("req#%d %s pool het %d luot thu -> 200 rong (nuot loi)",
                 rid, label, POOL_TRIES)
        self._soft(is_anth, model, want_stream, "pool exhausted")

    def _stream_anthropic(self, code, headers, r, model, sstream: bool = False):
        ct = str(headers.get("Content-Type") or "").lower()
        if code != 200 or "text/event-stream" not in ct:
            try:
                body = r.read()
            except Exception:
                body = b""
            if code != 200:
                return self._soft(True, model, sstream,
                                  "anth non-200 %d" % code)
            try:
                obj = json.loads(body)
            except Exception:
                return self._soft(True, model, sstream, "anth bad json")
            return self._reply(200, {"Content-Type": "application/json"},
                               json.dumps(_anth_from_openai(obj, model),
                                          ensure_ascii=False).encode())

        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
        except Exception:
            return

        mid = "msg_" + os.urandom(8).hex()
        st = {"idx": 0, "kind": None, "tool_i": None, "started": False,
              "stop": "end_turn", "in_tok": 0, "out_tok": 0}

        def ev(name, obj):
            self.wfile.write(("event: %s\ndata: %s\n\n"
                              % (name, json.dumps(obj, ensure_ascii=False))).encode())
            self.wfile.flush()

        def msg_start():
            ev("message_start", {"type": "message_start", "message": {
                "id": mid, "type": "message", "role": "assistant", "model": model,
                "content": [], "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": st["in_tok"], "output_tokens": 0}}})
            st["started"] = True

        def stop_block():
            if st["kind"] is not None:
                ev("content_block_stop", {"type": "content_block_stop",
                                          "index": st["idx"]})
                st["idx"] += 1
                st["kind"] = None

        try:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    ch = json.loads(data)
                except Exception:
                    continue
                u = ch.get("usage") or {}
                if u.get("prompt_tokens"):
                    st["in_tok"] = int(u["prompt_tokens"])
                if u.get("completion_tokens"):
                    st["out_tok"] = int(u["completion_tokens"])
                if not st["started"]:
                    msg_start()
                for chc in (ch.get("choices") or []):
                    d = chc.get("delta") or {}
                    if chc.get("finish_reason"):
                        st["stop"] = _STOP_MAP.get(chc["finish_reason"], "end_turn")
                    txt = d.get("content")
                    if txt:
                        if st["kind"] != "text":
                            stop_block()
                            ev("content_block_start", {"type": "content_block_start",
                                                       "index": st["idx"],
                                                       "content_block": {"type": "text",
                                                                         "text": ""}})
                            st["kind"] = "text"
                        ev("content_block_delta", {"type": "content_block_delta",
                                                   "index": st["idx"],
                                                   "delta": {"type": "text_delta",
                                                             "text": txt}})
                    for tcall in (d.get("tool_calls") or []):
                        ti = tcall.get("index", 0)
                        fn = tcall.get("function") or {}
                        if st["kind"] != "tool" or st["tool_i"] != ti:
                            stop_block()
                            st["kind"], st["tool_i"] = "tool", ti
                            ev("content_block_start", {"type": "content_block_start",
                                                       "index": st["idx"],
                                                       "content_block": {
                                                           "type": "tool_use",
                                                           "id": tcall.get("id") or ("toolu_" + os.urandom(6).hex()),
                                                           "name": fn.get("name") or "",
                                                           "input": {}}})
                        if fn.get("arguments"):
                            ev("content_block_delta", {"type": "content_block_delta",
                                                       "index": st["idx"],
                                                       "delta": {"type": "input_json_delta",
                                                                 "partial_json": fn["arguments"]}})
            if not st["started"]:
                msg_start()
            stop_block()
            ev("message_delta", {"type": "message_delta",
                                 "delta": {"stop_reason": st["stop"],
                                           "stop_sequence": None},
                                 "usage": {"output_tokens": st["out_tok"]}})
            ev("message_stop", {"type": "message_stop"})
        except Exception as e:
            LOG.warn("anthropic stream cut: %s", type(e).__name__)

    def _soft(self, is_anth: bool, model: str, stream: bool,
              why: str = "", text: str = "") -> None:
        try:
            body = (_soft_anthropic(model, stream, text) if is_anth
                    else _soft_openai(model, stream, text))
            self.send_response(200)
            self.send_header("Content-Type",
                             "text/event-stream" if stream else "application/json")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        except Exception:
            pass
        with _lock:
            _stats["soft_err"] = _stats.get("soft_err", 0) + 1
        LOG.warn("req -> tra 200 rong (nuot loi, client khong thay) | %s", why)

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
        self._sess_account(code, body)

    def _sess_account(self, code: int, body, nbytes: int | None = None) -> None:
        sess = getattr(self, "_sess", None)
        if not sess:
            return
        try:
            b = body if isinstance(body, (bytes, bytearray)) else b""
            i, o, cr, cw = _usage_scan(bytes(b))
            _sess_finish(sess, code, i, o, cr, cw,
                         len(b) if nbytes is None else nbytes)
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
            chunked = True
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
        else:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        capture = getattr(self, "_sess", None) is not None
        buf = bytearray() if capture else None
        sent = 0
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
                    sent += len(b)
                    if buf is not None:
                        buf += b
                        if len(buf) > SESS_BUF:
                            del buf[:len(buf) - SESS_BUF]
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            else:
                while True:
                    b = payload.read(8192)
                    if not b:
                        break
                    self.wfile.write(b)
                    self.wfile.flush()
                    sent += len(b)
                    if buf is not None:
                        buf += b
                        if len(buf) > SESS_BUF:
                            del buf[:len(buf) - SESS_BUF]
        except Exception:
            pass
        if buf is not None:
            self._sess_account(code, bytes(buf), sent)


    def do_GET(self):
        self._proxy("GET")

    def do_POST(self):
        self._proxy("POST")

    def do_PUT(self):
        self._proxy("PUT")

    def do_DELETE(self):
        self._proxy("DELETE")

    def do_OPTIONS(self):
        self._sess = None
        self._reply(204, {"Access-Control-Allow-Origin": "*",
                          "Access-Control-Allow-Headers": "*",
                          "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS"}, b"")


def main() -> int:
    if not UP.startswith("http"):
        log(f"[x] GATE_UP khong hop le: {UP}")
        return 2
    reglog.note_startup(LOG, up=UP, host=HOST, port=PORT, rpm=RPM, auto=AUTO,
                        rpm_max=RPM_MAX, burst=BURST, retry=RETRY, maxwait=MAXWAIT,
                        maxhold=MAXHOLD, vpn=VPN, dump=DUMP_N, level=os.environ.get("REG_LOG_LEVEL") or "info")
    reglog.install_excepthook(LOG)
    srv = ThreadingHTTPServer((HOST, PORT), Gate)
    srv.daemon_threads = True
    eff = cur_rpm(time.time())
    log(f"[ok] nine-gate {HOST}:{PORT} -> {UP} | {RPM:.0f} req/phut"
        + (f" (auto {eff:.1f} theo quota pool)" if AUTO else "")
        + f", burst {BURST}, retry {RETRY} | stats: http://{HOST}:{PORT}/gate/stats")
    if DUMP_N > 0:
        log(f"[i] GATE_DUMP={DUMP_N} -> {DUMP_PATH}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        _sess_save(force=True)
        log(f"[i] dung. {json.dumps({k: (round(v, 1) if isinstance(v, float) else v) for k, v in _stats.items()})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
