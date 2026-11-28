
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
FRONT = os.environ.get("FRONT_PORT") or "20130"
BACKENDS = [p.strip() for p in (os.environ.get("FRONT_BACKENDS") or "20140,20141").split(",")
            if p.strip()]
PY = sys.executable or "python"


def up(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def listening(port: str) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=1.5):
            return True
    except Exception:
        return False


def start(args: list[str], extra_env: dict) -> None:
    env = dict(os.environ)
    env.update(extra_env)
    flags = (0x00000008 | 0x00000200) if os.name == "nt" else 0
    subprocess.Popen([PY, "-u"] + args, cwd=HERE, env=env,
                     creationflags=flags,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL)


def main() -> int:
    for port in BACKENDS:
        if listening(port) and up(f"http://127.0.0.1:{port}/gate/stats"):
            print(f"[boot] backend {port} already up")
            continue
        start(["gate.py"], {
            "GATE_PORT": port, "GATE_KEYPOOL": "1", "GATE_POOL_PREFIX": "a",
            "GATE_POOL_UP": "https://tokenharbor.ai", "GATE_VPN": "1",
            "GATE_LOG": os.path.join("logs", f"gate_{port}.log")})
        print(f"[boot] backend {port} starting")

    if not up(f"http://127.0.0.1:{FRONT}/front/state"):
        start(["gate_front.py"], {"FRONT_PORT": FRONT,
                                  "FRONT_BACKENDS": ",".join(BACKENDS)})
        print(f"[boot] front door starting on {FRONT}")
    else:
        print(f"[boot] front door already up on {FRONT}")

    for _ in range(30):
        time.sleep(1)
        if up(f"http://127.0.0.1:{FRONT}/front/state", timeout=2):
            break
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{FRONT}/front/state", timeout=3) as r:
            st = json.loads(r.read().decode())
        print(f"[boot] front active={st.get('active_name')} healthy={st.get('healthy')} "
              f"backends={list((st.get('ports') or {}).items())}")
    except Exception as e:
        print(f"[boot] front not answering: {type(e).__name__}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
