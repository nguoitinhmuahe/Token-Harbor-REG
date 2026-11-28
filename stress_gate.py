
from __future__ import annotations

import json
import os
import re
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request

GATE = os.environ.get("GATE_URL") or "http://127.0.0.1:20130"
_KEY_CACHE: list[str] = []


def api_key() -> str:
    if _KEY_CACHE:
        return _KEY_CACHE[0]
    k = os.environ.get("GATE_KEY") or ""
    if not k:
        for p in (os.path.join(os.environ.get("USERPROFILE") or "", ".config",
                               "opencode", "opencode.json"),):
            try:
                cfg = json.load(open(p, encoding="utf-8"))
                prov = cfg.get("provider") or {}
                for v in prov.values():
                    k = ((v.get("options") or {}).get("apiKey")) or ""
                    if k:
                        break
                if k:
                    break
            except Exception:
                continue
    _KEY_CACHE.append(k)
    return k


def one(model: str) -> tuple:
    body = json.dumps({"model": model, "max_tokens": 1, "stream": False,
                       "messages": [{"role": "user", "content": "hi"}]}).encode()
    req = urllib.request.Request(GATE + "/v1/chat/completions", data=body,
                                headers={"content-type": "application/json",
                                         "authorization": "Bearer " + api_key()})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            r.read()
            return r.status, "", time.time() - t0
    except urllib.error.HTTPError as e:
        b = e.read().decode("utf-8", "replace")
        m = re.search(r"next request in ([^\"\\\\]+)", b)
        return e.code, (m.group(1).strip() if m else b[:110]), time.time() - t0
    except Exception as e:
        return 0, f"{type(e).__name__}: {str(e)[:80]}", time.time() - t0


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    th = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    model = sys.argv[3] if len(sys.argv) > 3 else "a/deepseek-v4.1-flash:free"
    out, lock = [], threading.Lock()

    def worker(k: int):
        for _ in range(k):
            with lock:
                out.append(one(model))
            time.sleep(0.3)

    per = max(1, n // th)
    t0 = time.time()
    ts = [threading.Thread(target=worker, args=(per,)) for _ in range(th)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    dt = time.time() - t0

    stats = {}
    for c, _, _ in out:
        stats[c] = stats.get(c, 0) + 1
    lat = sorted(x[2] for x in out)
    print(f"\n=== {len(out)} requests | model={model} | {th} workers | {dt:.0f}s "
          f"({len(out)/max(dt, 0.1):.2f} req/s)")
    print(f"    status: {dict(sorted(stats.items()))}")
    if lat:
        print(f"    latency: avg {statistics.mean(lat):.1f}s | p50 {lat[len(lat)//2]:.1f}s "
              f"| p95 {lat[int(len(lat) * 0.95)]:.1f}s | max {lat[-1]:.1f}s")
    bad = [(c, m) for c, m, _ in out if c >= 400 or c == 0]
    for c, m in bad[:8]:
        print(f"    FAIL {c}: {m}")
    if len(bad) > 8:
        print(f"    ... and {len(bad) - 8} more")
    print(f"    error rate: {100 * len(bad) / max(1, len(out)):.1f}%")


if __name__ == "__main__":
    main()
