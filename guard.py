from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable or "python"
WEBUI_PORT = int(os.environ.get("WEBUI_PORT") or 8787)
FRONT_PORT = int(os.environ.get("GATE_PORT") or os.environ.get("FRONT_PORT") or 20130)
BACKENDS = [int(p) for p in (os.environ.get("FRONT_BACKENDS") or "20140,20141").split(",")
            if p.strip()]
EVERY = float(os.environ.get("GUARD_EVERY") or 10)
GRACE = float(os.environ.get("GUARD_GRACE") or 15)
KILL_AFTER = float(os.environ.get("GUARD_KILL_AFTER") or 90)
LOCK_PORT = int(os.environ.get("GUARD_PORT") or 20199)
V6_PORT = int(os.environ.get("V6PROXY_PORT") or 21000)
LOGP = os.path.join(HERE, "logs", "guard.log")
PIDF = os.path.join(HERE, f".guard_pid_{LOCK_PORT}.json")
STOP = os.path.join(HERE, ".guard_stop")
LOG_MAX = 4 * 1024 * 1024


def log(msg: str) -> None:
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOGP), exist_ok=True)
        try:
            if os.path.getsize(LOGP) > LOG_MAX:
                os.replace(LOGP, LOGP + ".1")
        except OSError:
            pass
        with open(LOGP, "a", encoding="utf-8", errors="replace") as f:
            f.write(line + "\n")
    except Exception:
        pass


def up(url: str, timeout: float = 4.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.5):
            return True
    except Exception:
        return False


def holders(port: int) -> list[int]:
    try:
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
    except Exception:
        return []


def kill(pid: int) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=20)
        else:
            os.kill(pid, 9)
    except Exception:
        pass


def spawn(script: str, extra: dict) -> None:
    env = dict(os.environ)
    env.update(extra)
    flags = (0x00000008 | 0x00000200) if os.name == "nt" else 0
    try:
        subprocess.Popen([PY, "-u", os.path.join(HERE, script)], cwd=HERE, env=env,
                         creationflags=flags, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        log(f"start {script}")
    except Exception as e:
        log(f"start {script} FAILED: {e!r}")


def gateway_env() -> dict:
    return {"GATE_PORT": str(FRONT_PORT), "FRONT_PORT": str(FRONT_PORT),
            "FRONT_BACKENDS": ",".join(str(p) for p in BACKENDS),
            "GATE_KEYPOOL": "1", "GATE_POOL_PREFIX": "a",
            "GATE_POOL_UP": "https://tokenharbor.ai", "GATE_POOL_TRIES": "150",
            "GATE_VPN": "1", "VPN_REGIONS": os.environ.get("VPN_REGIONS", "usa")}


_BAD: dict[str, float] = {}


def verdict(key: str, ok: bool, port: int) -> bool:
    if ok:
        _BAD.pop(key, None)
        return False
    if listening(port):
        since = _BAD.setdefault(key, time.time())
        if time.time() - since < KILL_AFTER:
            return False
        _BAD.pop(key, None)
        return True
    _BAD.pop(key, None)
    return True


def ensure_gateway() -> None:
    front_ok = up(f"http://127.0.0.1:{FRONT_PORT}/front/state")
    front_act = verdict("front", front_ok, FRONT_PORT)
    bad_backs = [p for p in BACKENDS
                 if verdict(f"back{p}", up(f"http://127.0.0.1:{p}/gate/stats"), p)]
    if front_ok and not bad_backs:
        return
    if front_act:
        for pid in holders(FRONT_PORT):
            log(f"front {FRONT_PORT} hung pid={pid} -> kill")
            kill(pid)
    if front_act or bad_backs:
        log(f"gateway missing (front_act={front_act} bad_backs={bad_backs}) -> gate_boot")
        spawn("gate_boot.py", gateway_env())


def ensure_panel() -> None:
    ok = up(f"http://127.0.0.1:{WEBUI_PORT}/api/state")
    if not verdict("panel", ok, WEBUI_PORT):
        return
    for pid in holders(WEBUI_PORT):
        log(f"panel {WEBUI_PORT} hung pid={pid} -> kill")
        kill(pid)
    env = gateway_env()
    env.update({"WEBUI_PORT": str(WEBUI_PORT), "WEBUI_GATE": "1",
                "WEBUI_VPN": "0"})
    log(f"panel missing on {WEBUI_PORT} -> start webui.py")
    spawn("webui.py", env)


def ensure_v6proxy() -> None:
    if not os.path.exists(os.path.join(HERE, ".v6_addrs")):
        return
    if listening(V6_PORT):
        return
    for pid in holders(V6_PORT):
        kill(pid)
    log(f"v6proxy missing on {V6_PORT} -> start v6proxy.py")
    spawn("v6proxy.py", {})


def main() -> int:
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            lock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            lock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        lock.bind(("127.0.0.1", LOCK_PORT))
        lock.listen(1)
    except OSError:
        print(f"[guard] another guard is already running (lock :{LOCK_PORT})")
        return 0
    try:
        with open(PIDF, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass
    try:
        os.remove(STOP)
    except OSError:
        pass
    log(f"guard up pid={os.getpid()} | panel=:{WEBUI_PORT} front=:{FRONT_PORT} "
        f"backends={BACKENDS} every={EVERY:.0f}s")
    last_backup = 0.0
    time.sleep(GRACE)
    try:
        while True:
            if os.path.exists(STOP):
                log("stop file -> exit")
                break
            now = time.time()
            if now - last_backup > 6 * 3600:
                last_backup = now
                spawn("backup.py", {})
            try:
                ensure_gateway()
            except Exception as e:
                log(f"gateway check failed: {e!r}")
            try:
                ensure_panel()
            except Exception as e:
                log(f"panel check failed: {e!r}")
            try:
                ensure_v6proxy()
            except Exception as e:
                log(f"v6proxy check failed: {e!r}")
            time.sleep(EVERY)
    except KeyboardInterrupt:
        log("interrupted")
    finally:
        try:
            os.remove(PIDF)
        except OSError:
            pass
        lock.close()
        log("guard down")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
