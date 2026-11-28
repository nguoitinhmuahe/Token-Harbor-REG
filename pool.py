import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("GATE_KEYPOOL", "1")
os.environ.setdefault("GATE_POOL_PREFIX", "a")
os.environ.setdefault("GATE_POOL_UP", "https://tokenharbor.ai")

import nine_gate as g

p = g.POOL
now = time.time()
used = sorted(p.used.values())
hits = sorted(p.hits.values())
lines = []
lines.append("items=%d parked=%d used=%d hits=%d" %
             (len(p.items), len(p.park), len(p.used), len(p.hits)))
if used:
    lines.append("used: min=%.0fs med=%.0fs max=%.0fs (so voi now)" %
                 (now - used[0], now - used[len(used) // 2], now - used[-1]))
if hits:
    lines.append("hits: min=%d med=%d max=%d" %
                 (hits[0], hits[len(hits) // 2], hits[-1]))
    lines.append("hits>0: %d | >=2: %d | >=5: %d" % (
        sum(1 for v in hits if v > 0),
        sum(1 for v in hits if v >= 2),
        sum(1 for v in hits if v >= 5)))
lines.append("parked keys=%d (mau: %s)" % (
    len(p.park), sorted((now - v) for v in p.park.values())[:5]))
for i in range(5):
    sel = p.pick()
    if not sel or not sel[0]:
        lines.append("pool empty")
        break
    kid, key = sel[0]
    lines.append("pick %s used=%.0fs hits=%d" %
                 (kid[:8], now - p.used.get(kid, 0), p.hits.get(kid, 0)))
with open("pool_out.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")
print(lines[0])
