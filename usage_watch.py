"""Check the free-tier quota of each tokenharbor key via the web API, disable/enable in router.

Key source: router's sqlite (provider "a" = tokenharbor.ai). No need for a
browser: call GET /api/cli/free-tier directly with a Bearer key.

Run with usage_check.bat (menu). Configure via env vars:
    USAGE_MODE      once (default) | watch | status | enable
                    once   = scan once, disable accs that hit bottom, re-enable accs whose disable expired
                    watch  = loop forever, every USAGE_POLL minutes
                    status = only print, does NOT change anything
                    enable = re-enable all accs disabled by this tool, no scan
    USAGE_POLL      minutes between 2 scans when watching (default 15)
    USAGE_THRESHOLD used_pct >= this level counts as hitting bottom (default 100)
    USAGE_WORKERS   number of keys scanned in parallel (default 10)
    USAGE_PROVIDER  provider name in router (default "a")
    USAGE_ADOPT     1 = adopt accs disabled earlier to auto re-enable after reset (0)
    ROUTER_URL        router address (default http://127.0.0.1:20128)
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = os.environ.get("ROUTER_URL", "http://127.0.0.1:20128").rstrip("/")
API_URL = "https://tokenharbor.ai/api/cli/free-tier"
MODE = (os.environ.get("USAGE_MODE") or "once").strip().lower()
POLL = float(os.environ.get("USAGE_POLL") or 15)
THRESHOLD = float(os.environ.get("USAGE_THRESHOLD") or 100)
WORKERS = max(1, int(os.environ.get("USAGE_WORKERS") or 10))
PROVIDER = (os.environ.get("USAGE_PROVIDER") or "a").strip()
# USAGE_ADOPT=1: also adopt accs disabled earlier (not by this tool) to
# auto re-enable when the quota resets. Default 0 = only manage accs disabled by this tool.
ADOPT = (os.environ.get("USAGE_ADOPT") or "0").strip().lower() in (
    "1", "true", "yes", "on")

# Where the router keeps its data dir. Override with ROUTER_HOME.
HOME = os.environ.get("ROUTER_HOME") or (
    r"%APPDATA%\9router" if os.name == "nt" else os.path.expanduser("~/.config/9router"))
DB = os.path.join(os.path.expandvars(HOME), "db", "data.sqlite")
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".usage_state.json")

_print_lock = threading.Lock()


def out(*parts):
    with _print_lock:
        print(*parts, flush=True)


def api(path: str, method: str = "GET", body: dict | None = None):
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read().decode("utf-8", "replace")
    return json.loads(raw) if raw.strip().startswith(("{", "[")) else raw


def set_active(cid: str, active: bool) -> bool:
    try:
        api(f"/api/providers/{cid}", "PUT", {"isActive": active})
        return True
    except urllib.error.HTTPError as e:
        out(f"  ! PUT {cid[:8]} HTTP {e.code}")
        return False
    except Exception as e:
        out(f"  ! PUT {cid[:8]} {type(e).__name__}")
        return False


# ---------------------------------------------------------------- router


def node_id() -> str:
    nodes = api("/api/provider-nodes")
    nodes = nodes.get("nodes") if isinstance(nodes, dict) else nodes
    want = PROVIDER.strip().lower()
    hit = [n for n in nodes if (n.get("name") or "").strip().lower() == want]
    if not hit:
        hit = [n for n in nodes
               if "tokenharbor" in (n.get("data") or {}).get("baseUrl", "").lower()
               or "tokenharbor" in str(n.get("baseUrl") or "").lower()]
    if not hit:
        names = ", ".join(sorted((n.get("name") or "?") for n in nodes))
        raise SystemExit(f"[x] provider '{PROVIDER}' not found. currently have: {names}")
    return hit[0]["id"]


def connections(nid: str) -> list[dict]:
    data = api("/api/providers")
    rows = data.get("connections") if isinstance(data, dict) else data
    return [c for c in rows if c.get("provider") == nid]


def read_keys(ids: set[str]) -> dict[str, str]:
    """cid -> apiKey, read the router's sqlite (read-only, copy if locked)."""
    if not os.path.exists(DB):
        raise SystemExit(f"[x] router db not found: {DB}")
    tmp = None
    try:
        c = sqlite3.connect("file:" + DB.replace("\\", "/") + "?mode=ro", uri=True,
                            timeout=5)
        rows = c.execute("select id, data from providerConnections").fetchall()
        c.close()
    except sqlite3.Error:
        tmp = os.path.join(tempfile.gettempdir(), "usage_keys.sqlite")
        for src in (DB, DB + "-wal"):
            if os.path.exists(src):
                try:
                    shutil.copy2(src, tmp + (".wal" if src.endswith("-wal") else ""))
                except Exception:
                    pass
        c = sqlite3.connect(tmp)
        rows = c.execute("select id, data from providerConnections").fetchall()
        c.close()
    got = {}
    for cid, data in rows:
        if cid not in ids:
            continue
        try:
            key = (json.loads(data) or {}).get("apiKey") or ""
        except Exception:
            continue
        if key.startswith("thk_"):
            got[cid] = key
    if tmp:
        for p in (tmp, tmp + ".wal"):
            try:
                os.remove(p)
            except Exception:
                pass
    return got


# ---------------------------------------------------------------- web check


def check_key(key: str) -> dict:
    req = urllib.request.Request(
        API_URL,
        headers={"Authorization": "Bearer " + key,
                 "Accept": "application/json",
                 "User-Agent": "curl/8.4.0"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            j = json.loads(r.read().decode("utf-8", "replace"))
        return {"ok": True, "data": j}
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:120]
        except Exception:
            pass
        return {"ok": False, "code": e.code, "body": body}
    except Exception as e:
        return {"ok": False, "code": 0, "body": type(e).__name__}


def classify(res: dict) -> tuple[str, dict]:
    """-> (group, info). group: dead | exhausted | low | zero | ok | unknown"""
    if not res.get("ok"):
        code = res.get("code") or 0
        if code in (401, 403):
            return "dead", {"code": code}
        return "unknown", {"code": code, "body": res.get("body", "")}
    d = res.get("data") or {}
    used = d.get("used_pct")
    used = float(used) if isinstance(used, (int, float)) else 0.0
    if d.get("exhausted") or used >= THRESHOLD:
        return "exhausted", d
    if used <= 0:
        return "zero", d
    return "ok", d


def fmt_reset(d: dict) -> str:
    raw = d.get("reset_at") or d.get("period_ends_at")
    if not raw:
        return "?"
    try:
        t = datetime.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return t.astimezone().strftime("%d/%m %H:%M")
    except Exception:
        return str(raw)[:16]


def load_state() -> dict:
    try:
        with open(STATE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st: dict) -> None:
    try:
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception as e:
        out(f"  ! couldn't write state: {e}")


_KEYMAP: dict[str, str] = {}


def name_of(c: dict) -> str:
    """Acc name + key suffix: names in router are all '1' so the key must be included."""
    psd = c.get("providerSpecificData") or {}
    nm = c.get("name") or c.get("email") or psd.get("prefix") or c["id"][:8]
    tail = (_KEYMAP.get(c["id"]) or "")[-6:]
    return f"{nm}·{tail}" if tail else str(nm)


# ---------------------------------------------------------------- main


def scan(apply: bool) -> dict:
    nid = node_id()
    rows = connections(nid)
    if not rows:
        raise SystemExit(f"[x] provider '{PROVIDER}' has no accs")
    keys = read_keys({c["id"] for c in rows})
    _KEYMAP.update(keys)
    live = [c for c in rows if c["id"] in keys]
    out(f"[i] provider {PROVIDER!r} | {len(rows)} acc, {len(live)} key thk_ "
        f"| scanning {WORKERS} threads")
    if not live:
        raise SystemExit("[x] couldn't read any apiKey from router sqlite")

    t0 = time.monotonic()
    results: dict[str, tuple[str, dict]] = {}

    def work(c: dict):
        res = check_key(keys[c["id"]])
        results[c["id"]] = classify(res)

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        list(ex.map(work, live))

    st = load_state()
    now = time.time()
    counts = {"dead": 0, "exhausted": 0, "low": 0, "zero": 0, "ok": 0, "unknown": 0}
    kill, revive, detail = [], [], []
    for c in live:
        cid = c["id"]
        grp, d = results.get(cid, ("unknown", {}))
        counts[grp] = counts.get(grp, 0) + 1
        used = d.get("used_pct") if grp in ("ok", "zero", "exhausted", "low") else None
        if grp in ("dead", "exhausted", "low"):
            detail.append((c, grp, d))
            if (apply and not c.get("isActive") and ADOPT
                    and cid not in st):
                st[cid] = {"name": name_of(c), "at": now,
                           "why": grp, "reset": d.get("reset_at") or ""}
                out(f"[~] adopting {name_of(c)} ({grp}) -> will re-enable after reset")
            if apply and c.get("isActive") and set_active(cid, False):
                kill.append(cid)
                st[cid] = {"name": name_of(c), "at": now,
                           "why": grp, "reset": d.get("reset_at") or ""}
                out(f"[-] disabled {name_of(c)} | {grp}"
                    + (f" | used {used:.0f}% | reset {fmt_reset(d)}" if used is not None else ""))
        elif grp in ("ok", "zero"):
            rec = st.get(cid)
            if not rec:
                continue
            reset_raw = rec.get("reset") or ""
            due = True
            if reset_raw:
                try:
                    rt = datetime.datetime.fromisoformat(
                        str(reset_raw).replace("Z", "+00:00")).timestamp()
                    due = now >= rt
                except Exception:
                    due = True
            if not due:
                continue
            if apply and not c.get("isActive") and set_active(cid, True):
                st.pop(cid, None)
                revive.append(cid)
                out(f"[+] re-enabled {name_of(c)} | used {used:.0f}%")

    elapsed = time.monotonic() - t0
    out("")
    out(f"[=] {len(live)} key | ok {counts['ok']} | empty {counts['zero']} | "
        f"hit bottom {counts['exhausted']} | dead {counts['dead']} | error {counts['unknown']}"
        f"  ({elapsed:.1f}s)")
    if apply:
        on = sum(1 for c in conns_cache(rows, kill, revive) if c.get("isActive"))
        out(f"[=] enabled {on} / disabled {len(rows) - on}")
        out(f"[=] this run: disabled {len(kill)}, enabled {len(revive)}")
    else:
        out("[i] view mode: nothing changed")
    # detail table when there are accs nearly empty / just disabled (only print on first scan)
    if detail and apply:
        out("")
        out("[details] accs disabled:")
        for c, grp, d in detail[:40]:
            used = d.get("used_pct")
            u = f"{used:.0f}%" if isinstance(used, (int, float)) else "?"
            out(f"   {name_of(c):<12} {grp:<9} {u:>5}  reset {fmt_reset(d)}")
        if len(detail) > 40:
            out(f"   ... +{len(detail) - 40}")
    # warn about accs nearly empty (>=80%)
    near = []
    for c in live:
        grp, d = results.get(c["id"], ("", {}))
        used = d.get("used_pct")
        if grp in ("ok", "zero") and isinstance(used, (int, float)) and used >= 80:
            near.append((used, name_of(c), fmt_reset(d)))
    if near:
        near.sort(reverse=True)
        out("")
        out(f"[!] {len(near)} keys nearly empty (>=80%):")
        for used, nm, rs in near[:20]:
            out(f"   {nm:<12} {used:5.0f}%  reset {rs}")
    if apply:
        save_state(st)
    return {"kill": len(kill), "revive": len(revive), "counts": counts}


def conns_cache(rows: list[dict], kill: list[str], revive: list[str]) -> list[dict]:
    k, r = set(kill), set(revive)
    out_rows = []
    for c in rows:
        d = dict(c)
        if c["id"] in k:
            d["isActive"] = False
        if c["id"] in r:
            d["isActive"] = True
        out_rows.append(d)
    return out_rows


def reenable_all() -> int:
    st = load_state()
    if not st:
        out("[i] state empty: no accs disabled by this tool yet")
        return 0
    nid = node_id()
    rows = {c["id"]: c for c in connections(nid)}
    n = 0
    for cid, rec in list(st.items()):
        c = rows.get(cid)
        if not c:
            st.pop(cid, None)
            continue
        if set_active(cid, True):
            st.pop(cid, None)
            n += 1
            out(f"[+] enabled {rec.get('name') or cid[:8]}")
    save_state(st)
    out(f"[=] re-enabled {n} accs")
    return n


def main() -> int:
    try:
        api("/api/provider-nodes")
    except Exception as e:
        print(f"[x] can't reach {BASE} ({e}). is the router running?")
        return 2

    if MODE == "enable":
        reenable_all()
        return 0
    if MODE in ("status", "dry"):
        scan(False)
        return 0
    if MODE == "watch":
        out(f"[i] watch: scan every {POLL:g} min, Ctrl+C to stop")
        try:
            while True:
                out("")
                out(f"=== {time.strftime('%H:%M:%S')} ===")
                try:
                    scan(True)
                except SystemExit as e:
                    out(str(e))
                time.sleep(max(30.0, POLL * 60))
        except KeyboardInterrupt:
            out("\n[i] stopped")
        return 0
    scan(True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
