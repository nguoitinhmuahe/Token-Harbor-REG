
from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
WEBUI_PORT = os.environ.get("WEBUI_PORT") or "8787"
GATE_PORT = os.environ.get("GATE_PORT") or "20130"
PY = sys.executable or "python"
DRY = "--dry" in sys.argv


def http_ok(url: str, timeout: float = 4.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def panel_up() -> bool:
    return http_ok(f"http://127.0.0.1:{WEBUI_PORT}/api/state")


def gate_up() -> bool:
    return http_ok(f"http://127.0.0.1:{GATE_PORT}/gate/stats")


def free_ports() -> None:
    if os.name == "nt":
        ps = (
            f"Get-NetTCPConnection -State Listen -LocalPort {WEBUI_PORT},{GATE_PORT} "
            "-ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess "
            "-Unique | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }"
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True, timeout=60)
    else:
        for port in (WEBUI_PORT, GATE_PORT):
            subprocess.run(["bash", "-lc",
                            f"fuser -k {port}/tcp 2>/dev/null || true"],
                           capture_output=True, timeout=60)


def spawn_bat() -> None:
    bat = os.path.join(HERE, "web.bat")
    subprocess.Popen(["cmd", "/c", "start", "", bat], cwd=HERE,
                     creationflags=0x00000008)


def spawn_script(script: str) -> None:
    env = dict(os.environ)
    env.setdefault("GATE_KEYPOOL", "1")
    env.setdefault("GATE_POOL_PREFIX", "a")
    env.setdefault("GATE_POOL_UP", "https://tokenharbor.ai")
    flags = 0x00000008 | 0x00000200 if os.name == "nt" else 0
    subprocess.Popen([PY, "-u", os.path.join(HERE, script)], cwd=HERE, env=env,
                     creationflags=flags, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)


def wait_until(fn, sec: float) -> bool:
    end = time.time() + sec
    while time.time() < end:
        if fn():
            return True
        time.sleep(2)
    return False


def main() -> int:
    force = int(os.environ.get("RESTART_FORCE") or 0)
    print(f"[restart] panel=:{WEBUI_PORT} gate=:{GATE_PORT} dry={DRY} force={force}")
    if panel_up() and gate_up() and not force:
        print("[restart] panel + gate already up -> leaving them alone")
        return 0
    if DRY:
        return 0

    free_ports()
    time.sleep(2)

    if force <= 1:
        print("[1/3] web.bat ...")
        spawn_bat()
        if wait_until(panel_up, 90):
            ok_gate = wait_until(gate_up, 60)
            print(f"[1/3] panel=ok gate={'ok' if ok_gate else 'fail'}")
            return 0
        print("[1/3] failed -> trying path 2")

    if force <= 2:
        print("[2/3] python -u webui.py ...")
        spawn_script("webui.py")
        if wait_until(panel_up, 90):
            ok_gate = wait_until(gate_up, 60)
            print(f"[2/3] panel=ok gate={'ok' if ok_gate else 'fail'}")
            return 0
        print("[2/3] failed -> trying path 3")

    print("[3/3] gate_boot.py + webui.py ...")
    spawn_script("gate_boot.py" if os.path.exists(os.path.join(HERE, "gate_boot.py"))
                 else "gate.py")
    time.sleep(4)
    spawn_script("webui.py")
    ok_gate = wait_until(gate_up, 60)
    ok_panel = wait_until(panel_up, 90)
    print(f"[3/3] gate={'ok' if ok_gate else 'fail'} panel={'ok' if ok_panel else 'fail'}")
    return 0 if (ok_gate or ok_panel) else 1


if __name__ == "__main__":
    sys.exit(main())
