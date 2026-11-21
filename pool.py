"""pool.py - make the tokenharbor pool in the router actually usable.

Measured problems:
  - 1 tokenharbor key: 2 back-to-back requests is 429 (~25-30s); if the key is
    out of quota then 429 "59 min 1 s" for EVERY model.
  - router defaults to `fill-first` -> dumps all requests into 1 conn -> 429 -> failover.
    3 times 429 = backoffLevel rises (2s,4s,...300s) -> conn locked for the whole model.
  - 40 requests / 8 threads across many different keys: 40/40 OK, ~184 req/min.

How to run: pool_fix.bat (menu) or env var POOL_MODE.
  status  - view pool health + current strategy
  rr      - enable round-robin for provider (each request 1 conn)
  refresh - clear old lock/backoff, re-enable conn turned off by watcher due to 429
  probe   - test-call each key, classify (takes ~2-3 min)
  quota   - read weekly quota of EVERY acc (free-tier) -> total remaining capacity
  fix     - rr + refresh + probe + disable dead keys (401)
  bench   - fire N requests through router, measure req/min + error rate
  watch   - periodically refresh/revive (Ctrl+C to stop)

Config (env):
  ROUTER_URL       http://127.0.0.1:20128
  POOL_PROVIDER  a
  POOL_MODEL     deepseek-v4.1-flash:free
  POOL_MODE      status
  POOL_WORKERS   6      (probe)
  POOL_REQ       60     (bench)
  POOL_CONC      8      (bench)
  POOL_APPLY     1      (probe: 1 = disable 401 keys, 2 = disable all unusable keys)
  POOL_POLL      10     (watch, minutes)
"""

from __future__ import annotations

import concurrent.futures
import datetime
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request

BASE = (os.environ.get("ROUTER_URL") or "http://127.0.0.1:20128").rstrip("/")
PROVIDER = (os.environ.get("POOL_PROVIDER") or "a").strip()
MODEL = (os.environ.get("POOL_MODEL") or "deepseek-v4.1-flash:free").strip()
MODE = (os.environ.get("POOL_MODE") or "status").strip().lower()
WORKERS = max(1, int(os.environ.get("POOL_WORKERS") or 6))
REQ = max(1, int(os.environ.get("POOL_REQ") or 60))
CONC = max(1, int(os.environ.get("POOL_CONC") or 8))
APPLY = int(os.environ.get("POOL_APPLY") or 1)
POLL = max(1.0, float(os.environ.get("POOL_POLL") or 10))

# Where the router keeps its data dir. Override with ROUTER_HOME.
HOME = os.environ.get("ROUTER_HOME") or (
    r"%APPDATA%\9router" if os.name == "nt" else os.path.expanduser("~/.config/9router"))
DB = os.path.join(os.path.expandvars(HOME), "db", "data.sqlite")
API_MODEL = "https://tokenharbor.ai/v1/chat/completions"

_lock = threading.Lock()


def out(*parts):
    with _lock:
        print(*parts, flush=True)


# ------------------------------------------------------------------ router


def api(path: str, method: str = "GET", body: dict | None = None):
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read().decode("utf-8", "replace")
    return json.loads(raw) if raw.strip().startswith(("{", "[")) else raw


def set_active(cid: str, active: bool) -> bool:
    try:
        api(f"/api/providers/{cid}", "PUT", {"isActive": active})
        return True
    except urllib.error.HTTPError as e:
        out(f"  ! PUT {cid[:8]} HTTP {e.code}")
    except Exception as e:
        out(f"  ! PUT {cid[:8]} {type(e).__name__}")
    return False


def node_id() -> str:
    nodes = api("/api/provider-nodes")
    nodes = nodes.get("nodes") if isinstance(nodes, dict) else nodes
    want = PROVIDER.lower()
    hit = [n for n in nodes if (n.get("name") or "").strip().lower() == want]
    if not hit:
        hit = [n for n in nodes
               if "tokenharbor" in str((n.get("data") or {}).get("baseUrl")
                                       or n.get("baseUrl") or "").lower()]
    if not hit:
        names = ", ".join(sorted((n.get("name") or "?") for n in nodes))
        raise SystemExit(f"[x] provider '{PROVIDER}' not found. currently have: {names}")
    return hit[0]["id"]


def connections(nid: str) -> list[dict]:
    data = api("/api/providers")
    rows = data.get("connections") if isinstance(data, dict) else data
    return [c for c in rows if c.get("provider") == nid]


def get_settings() -> dict:
    d = api("/api/settings")
    return d if isinstance(d, dict) else {}


def set_strategy(nid: str, strategy: str = "round-robin", sticky: int = 1) -> dict:
    cur = get_settings()
    ps = dict(cur.get("providerStrategies") or {})
    old = ps.get(nid)
    ps[nid] = {"fallbackStrategy": strategy, "stickyRoundRobinLimit": sticky}
    api("/api/settings", "PATCH", {"providerStrategies": ps})
    now = get_settings()
    return {"before": old, "after": (now.get("providerStrategies") or {}).get(nid)}


# ------------------------------------------------------------------ sqlite


def db_conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB, timeout=15)
    c.execute("pragma busy_timeout=8000")
    return c


def db_rows() -> dict[str, dict]:
    """id -> data json (read directly from sqlite, not via API)."""
    c = db_conn()
    try:
        rows = c.execute("select id, isActive, data from providerConnections").fetchall()
    finally:
        c.close()
    out_map = {}
    for cid, act, raw in rows:
        try:
            j = json.loads(raw)
        except Exception:
            continue
        j["_active"] = bool(act)
        out_map[cid] = j
    return out_map


def clear_locks(ids: set[str] | None = None, dry: bool = False) -> int:
    """Clear expired modelLock / old backoffLevel / errorCode. -> number of conns fixed."""
    c = db_conn()
    n = 0
    try:
        rows = c.execute("select id, data from providerConnections").fetchall()
        now = time.time()
        for cid, raw in rows:
            if ids is not None and cid not in ids:
                continue
            try:
                j = json.loads(raw)
            except Exception:
                continue
            changed = False
            for k in [k for k in j if k.startswith("modelLock_")]:
                try:
                    until = datetime.datetime.fromisoformat(str(j[k]).replace("Z", "+00:00"))
                    fresh = until.timestamp() > now
                except Exception:
                    fresh = False
                if not fresh:
                    j.pop(k, None)
                    changed = True
            if j.get("errorCode") not in (None, 0):
                j["errorCode"] = None
                changed = True
            if j.get("backoffLevel"):
                j["backoffLevel"] = 0
                changed = True
            if j.get("lastError"):
                j["lastError"] = None
                j["lastErrorAt"] = None
                changed = True
            if j.get("testStatus") in ("unavailable", "error"):
                j["testStatus"] = "active"
                changed = True
            if not changed:
                continue
            n += 1
            if not dry:
                c.execute("update providerConnections set data=? where id=?",
                          (json.dumps(j, ensure_ascii=False), cid))
        if not dry:
            c.commit()
    finally:
        c.close()
    return n


def pending_locks(rows: dict[str, dict]) -> tuple[int, int]:
    now = time.time()
    conns = 0
    locks = 0
    for cid, j in rows.items():
        if not j.get("_active"):
            continue
        for k, v in j.items():
            if not k.startswith("modelLock_") or not v:
                continue
            try:
                until = datetime.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            except Exception:
                continue
            if until.timestamp() > now:
                locks += 1
                conns += 1
                break
    return conns, locks


def keys_of(rows: dict[str, dict], active_only: bool = False) -> dict[str, str]:
    got = {}
    for cid, j in rows.items():
        if active_only and not j.get("_active"):
            continue
        k = j.get("apiKey") or ""
        if k.startswith("thk_"):
            got[cid] = k
    return got


# ------------------------------------------------------------------ upstream


def call_key(key: str, model: str = MODEL, timeout: float = 45.0) -> dict:
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": "hi"}],
                       "max_tokens": 1, "stream": False}).encode()
    req = urllib.request.Request(API_MODEL, data=body,
                                 headers={"Content-Type": "application/json",
                                          "Accept": "application/json",
                                          "Authorization": "Bearer " + key})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
            return {"code": 200, "dt": time.time() - t0, "body": ""}
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:
            raw = ""
        return {"code": e.code, "dt": time.time() - t0, "body": raw,
                "retry": e.headers.get("Retry-After") if e.headers else None}
    except Exception as e:
        return {"code": 0, "dt": time.time() - t0, "body": f"{type(e).__name__}: {e}"}


def classify(res: dict) -> str:
    code = res.get("code")
    if code == 200:
        return "ok"
    body = (res.get("body") or "").lower()
    if code == 401:
        return "dead"
    if code == 403:
        return "no_model"
    if code == 402:
        return "no_quota"
    if code == 429:
        if "used" in body or "quota" in body or "59 min" in body or "hour" in body:
            return "limited_long"
        if "network address" in body or "sharing" in body:
            return "ip_limit"
        return "limited"
    if code == 0:
        return "net"
    return f"http{code}"


# ------------------------------------------------------------------ modes


def show_status() -> None:
    nid = node_id()
    rows = connections(nid)
    db = db_rows()
    active = [c for c in rows if c.get("isActive")]
    keys = keys_of({c["id"]: db.get(c["id"], {}) for c in rows}, active_only=True)
    by_code: dict[str, int] = {}
    for c in rows:
        j = db.get(c["id"], {})
        code = str(j.get("errorCode") or "-")
        by_code[code] = by_code.get(code, 0) + 1
    lock_conns, lock_n = pending_locks({c["id"]: db.get(c["id"], {}) for c in rows})

    st = get_settings()
    strat = (st.get("providerStrategies") or {}).get(nid)
    out(f"[i] provider {PROVIDER!r}  node={nid}")
    out(f"    conn: {len(rows)} | active: {len(active)} | has thk_ key: {len(keys)}")
    out(f"    by errorCode: {by_code}")
    out(f"    modelLock still valid: {lock_conns} conn ({lock_n} lock)")
    out(f"    strategy: {strat or ('DEFAULT (fill-first)' if not st.get('fallbackStrategy') else st.get('fallbackStrategy'))}")
    out(f"    model probe: {MODEL}")
    out(f"    db: {DB}")


def mode_rr() -> None:
    nid = node_id()
    r = set_strategy(nid, "round-robin", 1)
    out(f"[i] {r['before']} -> {r['after']}")
    if not r["after"]:
        raise SystemExit("[x] patch did not take effect. check /api/settings")
    out("[ok] round-robin on: each request goes to a different conn (sticky 1)")


def mode_refresh(revive: bool = True) -> None:
    nid = node_id()
    db = db_rows()
    conns = {c["id"]: c for c in connections(nid)}
    n = clear_locks({cid for cid in conns})
    out(f"[ok] cleared old lock/backoff/errorCode: {n} conn")

    if revive:
        back = 0
        for cid, c in conns.items():
            if c.get("isActive"):
                continue
            j = db.get(cid, {})
            if str(j.get("errorCode")) != "429":
                continue
            if "you've used" in (j.get("lastError") or "").lower():
                continue
            if set_active(cid, True):
                back += 1
        out(f"[ok] re-enabled conns disabled due to 429: {back}")


def mode_quota() -> None:
    """Read /api/cli/free-tier of each acc -> total remaining capacity of the pool."""
    db = db_rows()
    keys = keys_of(db)                       # even disabled accs (can still query quota)
    act = {cid for cid, j in db.items() if j.get("_active")}
    out(f"[i] reading quota {len(keys)} acc ({len(act & set(keys))} enabled), {WORKERS} threads")

    res: dict[str, dict] = {}
    done = [0]
    t0 = time.time()

    def job(item):
        cid, key = item
        req = urllib.request.Request(
            "https://tokenharbor.ai/api/cli/free-tier",
            headers={"Authorization": "Bearer " + key,
                     "Accept": "application/json",
                     "User-Agent": "curl/8.4.0"})
        d = None
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            d = None
        with _lock:
            res[cid] = d or {}
            done[0] += 1
            n = done[0]
        if n % 50 == 0:
            out(f"    {n}/{len(keys)} ({time.time()-t0:.0f}s)")

    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        list(ex.map(job, keys.items()))

    bad = sum(1 for d in res.values() if not d)
    hist: dict[int, int] = {}
    tot_q = 0.0
    tot_left = 0.0
    soon: list[tuple[str, float, str]] = []
    buckets: dict[str, float] = {}
    for cid, d in res.items():
        if not d:
            continue
        up = float(d.get("used_pct") or 0)
        ru = float(d.get("req_used") or 0)
        q = (ru / up * 100.0) if up > 0 else 600.0
        q = max(q, ru, 100.0)
        left = max(0.0, q - ru)
        active = cid in act
        tot_q += q
        if active:
            tot_left += left
        hist[int(up // 10) * 10] = hist.get(int(up // 10) * 10, 0) + 1
        day = str(d.get("reset_at") or "")[:10]
        buckets[day] = buckets.get(day, 0.0) + left
        soon.append(((keys.get(cid) or cid)[-6:], up, day))

    out(f"\n[i] read {len(res)-bad}/{len(keys)} acc (errors {bad})")
    out(f"    usage level: " + "  ".join(
        f"{k}-{k+10}%:{v}" for k, v in sorted(hist.items())))
    out(f"    weekly quota/acc ~{tot_q/max(1,len(res)-bad):,.0f} request")
    out(f"    TOTAL remaining pool  ~{tot_left:,.0f} request")
    days = sorted(buckets)
    if days:
        horizon = 7.0
        out(f"    -> average {tot_left/horizon/24:,.0f} req/hour "
            f"= {tot_left/horizon/24/60:,.1f} req/min continuously for 1 week")
    out(f"    reset day (remaining): " + "  ".join(
        f"{d}:{buckets[d]:,.0f}" for d in days[:4]))

    over = sorted([x for x in soon if x[1] >= 80], key=lambda x: -x[1])
    if over:
        out(f"    {len(over)} acc >=80% (e.g.: " +
            ", ".join(f"{k[-6:]}·{int(u)}%·{d}" for k, u, d in over[:6]) + ")")

    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".pool_quota.json")
        left_by_acc = {}
        for cid, d in res.items():
            if not d:
                continue
            up = float(d.get("used_pct") or 0)
            ru = float(d.get("req_used") or 0)
            q = (ru / up * 100.0) if up > 0 else 600.0
            q = max(q, ru, 100.0)
            left_by_acc[cid] = round(max(0.0, q - ru), 1)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"ts": datetime.datetime.now().isoformat(timespec="seconds"),
                       "accs": len(res) - bad, "total_quota": round(tot_q, 1),
                       "total_left": round(tot_left, 1),
                       "per_acc": round(tot_q / max(1, len(res) - bad), 1),
                       "hist": hist, "buckets": buckets,
                       "left_by_acc": left_by_acc},
                      f, ensure_ascii=False, indent=1)
        out(f"    wrote {path}")
    except Exception as e:
        out(f"    ! couldn't write .pool_quota.json: {e}")


def mode_probe() -> None:
    db = db_rows()
    now_active = {c["id"]: c for c in connections(node_id()) if c.get("isActive")}
    keys = keys_of({cid: db[cid] for cid in now_active if cid in db})
    out(f"[i] probe {len(keys)} key, {WORKERS} threads, model {MODEL}")

    results: dict[str, dict] = {}
    done = [0]
    t0 = time.time()
    ids = list(keys.items())

    def job(item):
        cid, key = item
        res = call_key(key)
        res["kind"] = classify(res)
        with _lock:
            results[cid] = res
            done[0] += 1
            n = done[0]
        if n % 25 == 0 or n == len(ids):
            out(f"    {n}/{len(ids)}  ({time.time()-t0:.0f}s)")

    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        list(ex.map(job, ids))

    agg: dict[str, int] = {}
    for cid, r in results.items():
        agg[r["kind"]] = agg.get(r["kind"], 0) + 1
    out(f"\n[i] results: {agg}")

    bad = [cid for cid, r in results.items() if r["kind"] == "dead"]
    nomodel = [cid for cid, r in results.items() if r["kind"] == "no_model"]
    for cid in bad:
        j = db.get(cid, {})
        out(f"    dead  {(j.get('name') or cid[:8])}·{(j.get('apiKey') or '')[-6:]}"
            f"  {results[cid]['code']}")
    if nomodel:
        out(f"    {len(nomodel)} conn 403 (this model can't be used right now) - kept as is")

    if APPLY >= 1 and bad:
        n = sum(1 for cid in bad if set_active(cid, False))
        out(f"[ok] disabled {n} dead keys (401)")

    if APPLY >= 2:
        # all keys unusable with this model (except 429 since it's just a rate limit)
        n = 0
        for cid, r in results.items():
            if r["kind"] in ("no_model", "no_quota"):
                n += 1 if set_active(cid, False) else 0
        out(f"[ok] disabled {n} conn 403/402")


def bench() -> None:
    nid = node_id()
    c = db_conn()
    try:
        row = c.execute("select key from apiKeys where isActive=1 limit 1").fetchone()
    finally:
        c.close()
    if not row:
        raise SystemExit("[x] router api key not found")
    key = row[0]
    url = BASE + "/v1/chat/completions"
    model = f"{PROVIDER}/{MODEL}" if not MODEL.startswith(PROVIDER + "/") else MODEL
    c2 = db_conn()
    try:
        before = c2.execute("select coalesce(max(id),0) from usageHistory").fetchone()[0]
    finally:
        c2.close()

    out(f"[i] bench {REQ} requests, {CONC} threads -> {url} model {model}")

    def one(i):
        body = json.dumps({"model": model,
                           "messages": [{"role": "user", "content": "hi"}],
                           "max_tokens": 4, "stream": False}).encode()
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json",
                                              "Authorization": "Bearer " + key})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                r.read()
                return 200, time.time() - t0, ""
        except urllib.error.HTTPError as e:
            return e.code, time.time() - t0, e.read()[:140].decode("utf-8", "replace")
        except Exception as e:
            return 0, time.time() - t0, str(e)[:80]

    res = []
    q = list(range(REQ))
    qlock = threading.Lock()

    def worker():
        while True:
            with qlock:
                if not q:
                    return
                i = q.pop()
            res.append(one(i))

    t0 = time.time()
    ths = [threading.Thread(target=worker) for _ in range(CONC)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    wall = max(0.001, time.time() - t0)
    ok = sum(1 for r in res if r[0] == 200)
    out(f"\n[ok] {ok}/{REQ} succeeded  |  {wall:.1f}s  |  {REQ/wall*60:.0f} req/min")
    agg: dict[str, int] = {}
    for code, dt, msg in res:
        agg[str(code)] = agg.get(str(code), 0) + 1
    out(f"    return codes: {agg}")
    for code, dt, msg in res:
        if code != 200:
            out(f"    ! {code}: {msg[:120]}")
            break
    time.sleep(2)
    c3 = db_conn()
    try:
        rows = c3.execute("select connectionId, count(*) from usageHistory where id>? "
                          "group by connectionId", (before,)).fetchall()
    finally:
        c3.close()
    out(f"    conns used: {len(rows)} (top: {rows[:5]})")


def mode_watch() -> None:
    out(f"[i] watch every {POLL:g} min. Ctrl+C to stop.")
    try:
        while True:
            stamp = time.strftime("%H:%M:%S")
            try:
                n = clear_locks()
                nid = node_id()
                conns = {c["id"]: c for c in connections(nid)}
                db = db_rows()
                back = 0
                for cid, c in conns.items():
                    if c.get("isActive"):
                        continue
                    j = db.get(cid, {})
                    if str(j.get("errorCode")) == "429" and \
                            "you've used" not in (j.get("lastError") or "").lower():
                        back += 1 if set_active(cid, True) else 0
                lock_conns, lock_n = pending_locks({cid: db.get(cid, {}) for cid in conns})
                act = sum(1 for c in conns.values() if c.get("isActive"))
                out(f"[{stamp}] cleared {n} conn | re-enabled {back} | active {act} | "
                    f"locked {lock_conns}")
            except Exception as e:
                out(f"[{stamp}] error: {type(e).__name__}: {e}")
            time.sleep(POLL * 60)
    except KeyboardInterrupt:
        out("\n[i] stopped watch")


def main() -> int:
    try:
        api("/api/provider-nodes")
    except Exception as e:
        out(f"[x] can't reach {BASE} ({type(e).__name__}). is the router running?")
        return 2

    if MODE == "status":
        show_status()
    elif MODE == "rr":
        mode_rr()
    elif MODE == "refresh":
        mode_refresh()
    elif MODE == "probe":
        mode_probe()
    elif MODE == "quota":
        mode_quota()
    elif MODE == "bench":
        bench()
    elif MODE == "fix":
        mode_rr()
        mode_refresh()
        mode_probe()
        bench()
    elif MODE == "watch":
        mode_watch()
    else:
        out(f"[x] invalid POOL_MODE: {MODE}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
