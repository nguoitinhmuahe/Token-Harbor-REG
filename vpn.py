
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request

CTL = os.environ.get("EXPRESSVPNCTL") or r"C:\Program Files\ExpressVPN\expressvpnctl.exe"


def _run(*args, timeout: float = 60.0) -> str:
    p = subprocess.run([CTL, *args], capture_output=True, text=True,
                       timeout=timeout, creationflags=0x08000000)
    return (p.stdout or "") + (p.stderr or "")


def installed() -> bool:
    return os.path.exists(CTL)


def status() -> str:
    try:
        out = _run("get", "connectionstate", timeout=20)
        for line in out.splitlines():
            line = line.strip()
            if line and not line.startswith("["):
                return line
    except Exception:
        pass
    return "Unknown"


def connect(region: str = "vietnam", wait: float = 90.0) -> bool:
    try:
        _run("set", "region", region, timeout=30)
        _run("connect", timeout=60)
    except Exception:
        return False
    end = time.monotonic() + wait
    while time.monotonic() < end:
        if status() == "Connected":
            return True
        time.sleep(1.5)
    return status() == "Connected"


def disconnect() -> None:
    try:
        _run("disconnect", timeout=30)
    except Exception:
        pass


def current_ip() -> str | None:
    try:
        req = urllib.request.Request("https://ipwho.is/",
                                     headers={"User-Agent": "Mozilla/5.0"})
        ctx = __import__("ssl").create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = __import__("ssl").CERT_NONE
        d = json.loads(urllib.request.urlopen(req, timeout=20, context=ctx)
                       .read().decode("utf-8", "replace"))
        return d.get("ip")
    except Exception:
        return None
