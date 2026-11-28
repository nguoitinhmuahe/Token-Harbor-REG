
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
FRONT = os.environ.get("FRONT_PORT") or "20130"
BACKENDS = [p.strip() for p in (os.environ.get("FRONT_BACKENDS") or "20140,20141").split(",")
            if p.strip()]
NAMES = ["a", "b", "c", "d"][:len(BACKENDS)]
PY = sys.executable or "python"
RESTART = "--restart" in sys.argv

PREFIX = os.environ.get("GATE_POOL_PREFIX") or "a"
FALLBACK = os.environ.get("GATE_POOL_FALLBACK") or "deepseek-v4-flash:free"
PROBE_MODEL = os.environ.get("GATE_PROBE_MODEL") or f"{PREFIX}/{FALLBACK}"


def http_json(url: str, timeout: float = 5.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def port_holders(port: str):
    if os.name == "nt":
        ps = (f"Get-NetTCPConnection -State Listen -LocalPort {port} "
              "-ErrorAction SilentlyContinue | Select-Object -ExpandProperty "
              "OwningProcess -Unique")
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=30)
        return [int(x) for x in (r.stdout or "").split() if x.strip().isdigit()]
    r = subprocess.run(["bash", "-lc", f"lsof -ti tcp:{port} 2>/dev/null || true"],
                       capture_output=True, text=True, timeout=30)
    return [int(x) for x in (r.stdout or "").split() if x.strip().isdigit()]


def kill(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue"],
                       capture_output=True, timeout=30)
    else:
        subprocess.run(["kill", "-9", str(pid)], capture_output=True, timeout=30)


def start_backend(port: str) -> None:
    env = dict(os.environ)
    env.update({
        "GATE_PORT": port,
        "GATE_KEYPOOL": "1",
        "GATE_POOL_PREFIX": PREFIX,
        "GATE_POOL_UP": "https://tokenharbor.ai",
        "GATE_VPN": "1",
        "GATE_LOG": os.path.join("logs", f"gate_{port}.log"),
    })
    flags = (0x00000008 | 0x00000200) if os.name == "nt" else 0
    subprocess.Popen([PY, "-u", os.path.join(HERE, "gate.py")], cwd=HERE, env=env,
                     creationflags=flags,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL)


def chat_ok(port: str, tries: int = 3) -> bool:
    body = json.dumps({"model": PROBE_MODEL, "max_tokens": 1,
                       "messages": [{"role": "user", "content": "hi"}]}).encode()
    for _ in range(tries):
        req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/chat/completions",
                                     data=body, headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                if r.status == 200:
                    return True
        except urllib.error.HTTPError as e:
            if e.code in (429, 503):
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def front_state():
    return http_json(f"http://127.0.0.1:{FRONT}/front/state")


def switch(to: str) -> bool:
    body = json.dumps({"to": to}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{FRONT}/front/switch", data=body,
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode()).get("ok") is True
    except Exception:
        return False


def main() -> int:
    st = front_state()
    if not st:
        print("[swap] front is not answering - start it first (gate_boot.py)")
        return 2
    active = st["active_name"]
    idle_i = [i for i, n in enumerate(NAMES) if n != active]
    if not idle_i:
        print("[swap] need at least two backends")
        return 2
    idle = NAMES[idle_i[0]]
    idle_port = BACKENDS[idle_i[0]]
    old_port = BACKENDS[NAMES.index(active)]
    print(f"[swap] active={active}:{old_port}  idle={idle}:{idle_port}")

    holders = port_holders(idle_port)
    if holders and not RESTART:
        print(f"[swap] idle backend already running (pid={holders}) - reusing it")
    else:
        for pid in holders:
            print(f"[swap] stopping idle backend pid={pid} for a clean start")
            kill(pid)
        start_backend(idle_port)
        ok = False
        for _ in range(30):
            time.sleep(1)
            if http_json(f"http://127.0.0.1:{idle_port}/gate/stats", timeout=2):
                ok = True
                break
        if not ok:
            print("[swap] new backend did not come up -> aborting, live one untouched")
            return 1

    if not chat_ok(idle_port):
        print("[swap] new backend not serving -> aborting, live one untouched")
        return 1

    if not switch(idle):
        print("[swap] switch request failed; leaving both backends running")
        return 1
    print(f"[swap] switch -> {idle}")

    st2 = front_state() or {}
    if st2.get("active_name") != idle:
        print("[swap] switch did not take effect; leaving both backends running")
        return 1

    for _ in range(3):
        if not chat_ok(FRONT):
            print("[swap] traffic check through the front failed; rolling back")
            switch(active)
            return 1
    for pid in port_holders(old_port):
        print(f"[swap] stopping old backend pid={pid} ({active}:{old_port})")
        kill(pid)
    print(f"[swap] done: {idle} is live, {active} stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
