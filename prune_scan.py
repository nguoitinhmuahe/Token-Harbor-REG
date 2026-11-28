from __future__ import annotations

import concurrent.futures as cf
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
UP = (os.environ.get("GATE_POOL_UP") or "https://tokenharbor.ai").rstrip("/")
MODEL = os.environ.get("PRUNE_MODEL") or "deepseek-v4.1-flash:free"
PRUNED = os.path.join(HERE, ".gate_pruned.txt")
FILES = [os.path.join(HERE, "alive_tokens.txt"), os.path.join(HERE, "tokens.txt")]

WORKERS = 12
LIMIT = 0
args = sys.argv[1:]
for i, a in enumerate(args):
    if a == "--workers" and i + 1 < len(args):
        WORKERS = max(1, int(args[i + 1]))
    if a == "--limit" and i + 1 < len(args):
        LIMIT = int(args[i + 1])

lock = threading.Lock()
seen: set = set()
keys: list[str] = []
for p in FILES:
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            for ln in f:
                k = ln.strip()
                if k and len(k) >= 20 and " " not in k and k not in seen:
                    seen.add(k)
                    keys.append(k)
    except FileNotFoundError:
        pass
if LIMIT:
    keys = keys[:LIMIT]
try:
    with open(PRUNED, encoding="utf-8", errors="replace") as f:
        already = {ln.strip() for ln in f if ln.strip()}
except FileNotFoundError:
    already = set()
keys = [k for k in keys if k not in already]
print(f"[scan] {len(keys)} key can kiem (model {MODEL}, {WORKERS} luong)")

BODY = json.dumps({"model": MODEL, "max_tokens": 1,
                   "messages": [{"role": "user", "content": "hi"}]}).encode()

res = {"200": 0, "429": 0, "403_flag": 0, "401": 0, "other": 0, "net": 0}
pruned: list[str] = []


def one(key: str) -> None:
    req = urllib.request.Request(UP + "/v1/chat/completions", data=BODY,
                                 method="POST",
                                 headers={"content-type": "application/json",
                                          "authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            r.read(64)
        kind = "200"
    except urllib.error.HTTPError as e:
        try:
            b = e.read()[:400].decode("utf-8", "replace")
        except Exception:
            b = ""
        if e.code == 429:
            kind = "429"
        elif e.code in (401, 403) and ("content-policy" in b
                                       or "paused for this account" in b
                                       or "refused requests from accounts like this" in b):
            kind = "403_flag"
            pruned.append(key)
        elif e.code == 401:
            kind = "401"
            pruned.append(key)
        else:
            kind = "other"
    except Exception:
        kind = "net"
    with lock:
        res[kind] = res.get(kind, 0) + 1


t0 = time.time()
with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
    list(ex.map(one, keys))

if pruned:
    with open(PRUNED, "a", encoding="utf-8") as f:
        for k in pruned:
            f.write(k + "\n")
print(f"[scan] xong {time.time()-t0:.0f}s -> {res}")
print(f"[scan] xoa {len(pruned)} key (ghi {PRUNED})")
