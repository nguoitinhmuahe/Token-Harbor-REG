"""
vpn.py — control ExpressVPN CLI (expressvpnctl) for tokenharbor reg.

Principle: NEVER leave the machine in the state "Disconnected with Network Lock
on" (loses all internet). Therefore:

  * every ctl command goes through 1 lock (thread + file) -> main.py and vpn_pool.bat
    don't step on each other (2 overlapping connect/disconnect commands are what
    makes the daemon get stuck at Disconnected).
  * connect() waits for Connected THEN checks the real internet IP; Connected but
    unable to reach the net (netlock/DNS) still counts as fail and self-recovers.
  * ensure() is "rescue": weird state + no net -> connect again.
  * prepare()/restore(): turn Network Lock off while running (netlock + disconnect =
    lose network), turn it back on as before on exit if it was on before.

API:
    ctl(*args)                    raw call (already locked)
    regions() / family() / pool() host + country group + filtered host
    state() / region()            tunnel state / currently selected region
    public_ip() / online()        internet-facing IP / whether there is internet
    connect(region, avoid_ip=)    set region + connect + verify IP
    reconnect(region, avoid_ip=)  disconnect then connect (get a new IP)
    ensure(region)                Connected + has net? if not, connect
    prepare() / restore(prep)     enable background, turn Network Lock off/on
"""

from __future__ import annotations

import json
import os
import re
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.request

CTL = r"C:\Program Files\ExpressVPN\expressvpnctl.exe"

# Country names made of many words -> must match the longest first ("hong-kong-1" != "hong").
MULTI = (
    "hong-kong", "south-korea", "united-kingdom", "united-states", "new-zealand",
    "sri-lanka", "south-africa", "costa-rica", "el-salvador", "saudi-arabia",
    "united-arab-emirates", "dominican-republic", "czech-republic", "isle-of-man",
    "the-netherlands", "australia", "singapore", "japan", "india", "canada",
)
MULTI = tuple(sorted(MULTI, key=len, reverse=True))

# Unused regions: mainland China / Hong Kong / Macau + regions sanctioned or
# export-controlled by the US.
BLOCKED = ("china", "hong-kong", "macau", "cuba", "iran", "north-korea",
           "syria", "russia", "belarus", "myanmar", "venezuela", "sudan")

IP_URLS = ("https://ipwho.is/", "https://api.ipify.org", "https://ifconfig.me/ip")
IP_RE = re.compile(r"[0-9a-fA-F:.]{7,45}")

# ================= lock =================
_LOCK = threading.RLock()
_LOCK_PATH = os.path.join(tempfile.gettempdir(), "expressvpnctl.lock")
_LOCK_STALE = 240.0          # lock older than 4 minutes = previous process died


class _FileLock:
    """Inter-process lock: 2 tools calling ctl at the same time is the fastest
    way for the daemon to fall into Disconnected."""

    def __enter__(self):
        end = time.monotonic() + 120
        while True:
            try:
                fd = os.open(_LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return self
            except FileExistsError:
                try:
                    age = time.time() - os.path.getmtime(_LOCK_PATH)
                except OSError:
                    age = 0.0
                if age > _LOCK_STALE:
                    try:
                        os.remove(_LOCK_PATH)
                    except OSError:
                        pass
                    continue
                if time.monotonic() > end:      # if stuck too long, just run anyway
                    return self
                time.sleep(0.15)

    def __exit__(self, *exc):
        try:
            os.remove(_LOCK_PATH)
        except OSError:
            pass
        return False


def ctl(*args: str, timeout: float = 60.0) -> str:
    """Call expressvpnctl (already locked). Error/timeout -> return "" for the caller to handle."""
    with _LOCK:
        with _FileLock():
            try:
                p = subprocess.run([CTL, *args], capture_output=True, text=True,
                                   timeout=timeout, creationflags=0x08000000)
                return (p.stdout or "") + (p.stderr or "")
            except (OSError, subprocess.SubprocessError):
                return ""


def _first(out: str, skip: tuple[str, ...] = ()) -> str:
    for line in out.splitlines():
        line = line.strip()
        if line and not line.startswith("[") and line not in skip:
            return line
    return ""


# ================= host list =================
def regions() -> list[str]:
    out = ctl("get", "regions", timeout=30)
    names = []
    for line in out.splitlines():
        line = line.strip()
        if line and not line.startswith("[") and line != "smart":
            names.append(line)
    return names


def family(name: str) -> str:
    base = re.sub(r"-\(via-[^)]*\)$", "", name.lower())
    base = re.sub(r"-\d+$", "", base)
    for m in MULTI:
        if base == m or base.startswith(m + "-"):
            return m
    return base.split("-")[0]


def is_blocked(name: str) -> bool:
    return family(name) in BLOCKED


def pool() -> list[str]:
    """Usable hosts (blocked regions removed)."""
    return [r for r in regions() if not is_blocked(r)]


# ================= state =================
def state() -> str:
    out = _first(ctl("get", "connectionstate", timeout=20), ("Unknown",))
    return out or "Unknown"


def status() -> str:
    """Keep the old name for other code."""
    return state()


def region() -> str:
    return _first(ctl("get", "region", timeout=20), ("smart",))


def wait_state(want: str, sec: float = 60.0) -> bool:
    end = time.monotonic() + sec
    while time.monotonic() < end:
        if state() == want:
            return True
        time.sleep(0.8)
    return state() == want


NETLOCK_TIMEOUT = ("Disconnecting", "DisconnectingToReconnect", "Connecting",
                   "Reconnecting")


def networklock() -> str:
    """'true' | 'false' | '' (couldn't read)."""
    return _first(ctl("get", "networklock", timeout=20)).lower()


def set_networklock(on: bool) -> bool:
    ctl("set", "networklock", "true" if on else "false", timeout=30)
    return networklock() == ("true" if on else "false")


# ================= real internet =================
def public_ip(retries: int = 1) -> str | None:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    for _ in range(max(1, retries)):
        for url in IP_URLS:
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "Mozilla/5.0"})
                raw = urllib.request.urlopen(req, timeout=10, context=ctx).read()
                text = raw.decode("utf-8", "replace").strip()
                if text.startswith("{"):
                    text = (json.loads(text).get("ip") or "").strip()
                if IP_RE.fullmatch(text):
                    return text
            except Exception:
                continue
        time.sleep(0.5)
    return None


def online(tries: int = 3) -> bool:
    """Real internet? This tunnel hands out a different IP on each connection (the
    server's NAT pool), so 1 failed request doesn't mean the network is down - must
    try several times before concluding, to avoid needless reconnect."""
    for i in range(max(1, tries)):
        if public_ip(1):
            return True
        if i + 1 < tries:
            time.sleep(0.8)
    return False


def current_ip() -> str | None:
    """Keep the old name."""
    return public_ip()


# ================= connect / recover =================
def _connect_once(target: str | None, wait: float) -> str | None:
    if target:
        ctl("set", "region", target, timeout=30)
    ctl("connect", timeout=90)
    if not wait_state("Connected", wait):
        return None
    ip = public_ip(2)
    return ip          # None = Connected but can't reach the net


def connect(target: str | None = None, wait: float = 75.0, tries: int = 2,
            avoid_ip: str | None = None) -> bool:
    """Ensure Connected + has internet, with an IP different from avoid_ip if possible.

    On each fail: fully disconnect then connect again (connecting on top of an
    already-Connected tunnel will always eventually have the daemon report
    Connected while the network is down).
    """
    for attempt in range(1, tries + 1):
        ip = _connect_once(target, wait)
        if ip and (avoid_ip is None or ip != avoid_ip):
            return True
        if ip:
            print(f"[VPN] duplicate IP ({ip}) -> reconnect to get another server")
        else:
            print(f"[VPN] connect didn't reach the net (state={state()}) -> retry "
                  f"{attempt}/{tries}")
        ctl("disconnect", timeout=30)
        wait_state("Disconnected", 15)
        time.sleep(0.6)
    # last time: plain connect, no need for a new IP, as long as there IS network
    ip = _connect_once(target, wait)
    return bool(ip)


def reconnect(target: str | None = None, avoid_ip: str | None = None,
              wait: float = 75.0) -> bool:
    """Disconnect then connect (change server/IP)."""
    ctl("disconnect", timeout=30)
    wait_state("Disconnected", 20)
    time.sleep(0.5)
    return connect(target, wait=wait, tries=2, avoid_ip=avoid_ip)


def disconnect() -> None:
    ctl("disconnect", timeout=30)


def ensure(target: str | None = None, wait: float = 60.0) -> bool:
    """Rescue: if already Connected + has internet, leave it; otherwise connect again."""
    st = state()
    if st == "Connected" and online(3):
        return True
    if st in NETLOCK_TIMEOUT:
        wait_state("Connected", 10)
        if state() == "Connected" and online(2):
            return True
    print(f"[VPN] ensure: state={st} -> connecting again")
    return connect(target, wait=wait, tries=1)


# ================= prepare / restore =================
def prepare() -> dict:
    """Prepare the environment for reg: daemon running in background + Network Lock off.

    Network Lock on + tunnel disconnected = lose all internet (even when the
    tool intends to reconnect). Reg needs a few dozen reconnects -> turn off, turn
    back on as before at the end of the session.
    """
    info = {"background": False, "netlock_before": "", "netlock_off": False}
    ctl("background", "enable", timeout=30)
    info["background"] = True
    before = networklock()
    info["netlock_before"] = before
    if before == "true":
        info["netlock_off"] = set_networklock(False)
        print("[VPN] Network Lock is ON -> temporarily turning off (avoid losing "
              "network when the tool reconnects)")
    return info


def restore(info: dict | None) -> None:
    """End of session: ensure there is network, then restore Network Lock to as before."""
    ok = ensure()
    if not ok:
        print("[VPN] warning: couldn't restore the connection")
    if info and info.get("netlock_before") == "true":
        if ok:
            set_networklock(True)
            print("[VPN] turned Network Lock back on")
        else:
            print("[VPN] no network yet -> KEEPING Network Lock off so you can get "
                  "online; turn it back on with: expressvpnctl set networklock true")
