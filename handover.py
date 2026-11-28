
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("FRONT_PORT") or 20130)
BACKENDS = os.environ.get("FRONT_BACKENDS") or "20140,20141"
PY = sys.executable or "python"


def holders(port: int) -> list[int]:
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


def front_up(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/front/state", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def start_front() -> None:
    env = dict(os.environ)
    env["FRONT_PORT"] = str(PORT)
    env["FRONT_BACKENDS"] = BACKENDS
    flags = (0x00000008 | 0x00000200) if os.name == "nt" else 0
    subprocess.Popen([PY, "-u", os.path.join(HERE, "gate_front.py")], cwd=HERE, env=env,
                     creationflags=flags,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL)


def main() -> int:
    for attempt in range(1, 6):
        if front_up(PORT):
            print(f"[handover] front already serving :{PORT}")
            return 0
        for pid in holders(PORT):
            print(f"[handover] attempt {attempt}: killing holder pid={pid}")
            kill(pid)
        start_front()
        for _ in range(20):
            time.sleep(0.5)
            if front_up(PORT):
                print(f"[handover] front is up on :{PORT} -> {BACKENDS}")
                return 0
        print(f"[handover] attempt {attempt} failed, retrying")
    print("[handover] FAILED - port still held by someone else")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
