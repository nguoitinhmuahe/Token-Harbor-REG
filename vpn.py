
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

MULTI = (
    "hong-kong", "south-korea", "united-kingdom", "united-states", "new-zealand",
    "sri-lanka", "south-africa", "costa-rica", "el-salvador", "saudi-arabia",
    "united-arab-emirates", "dominican-republic", "czech-republic", "isle-of-man",
    "the-netherlands", "australia", "singapore", "japan", "india", "canada",
)
MULTI = tuple(sorted(MULTI, key=len, reverse=True))

BLOCKED = ("china", "hong-kong", "macau", "cuba", "iran", "north-korea",
           "syria", "russia", "belarus", "myanmar", "venezuela", "sudan")

IP_URLS = ("https://ipwho.is/", "https://api.ipify.org", "https://ifconfig.me/ip")
IP_URLS4 = ("https://api4.ipify.org", "https://ipv4.icanhazip.com")
IP_RE = re.compile(r"[0-9a-fA-F:.]{7,45}")

_LOCK = threading.RLock()
_LOCK_PATH = os.path.join(tempfile.gettempdir(), "expressvpnctl.lock")
_LOCK_STALE = 240.0


class _FileLock:

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
                if time.monotonic() > end:
                    return self
                time.sleep(0.15)

    def __exit__(self, *exc):
        try:
            os.remove(_LOCK_PATH)
        except OSError:
            pass
        return False


def ctl(*args: str, timeout: float = 60.0) -> str:
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
    allow = [a.strip().lower() for a in
             (os.environ.get("VPN_REGIONS") or "").split(",") if a.strip()]
    out = []
    for r in regions():
        if is_blocked(r):
            continue
        if allow and family(r) not in allow:
            continue
        out.append(r)
    return out


def state() -> str:
    out = _first(ctl("get", "connectionstate", timeout=20), ("Unknown",))
    return out or "Unknown"


def status() -> str:
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
    return _first(ctl("get", "networklock", timeout=20)).lower()


def set_networklock(on: bool) -> bool:
    ctl("set", "networklock", "true" if on else "false", timeout=30)
    return networklock() == ("true" if on else "false")


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


def public_ip4(retries: int = 1) -> str | None:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    for _ in range(max(1, retries)):
        for url in IP_URLS4:
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "Mozilla/5.0"})
                raw = urllib.request.urlopen(req, timeout=10, context=ctx).read()
                text = raw.decode("utf-8", "replace").strip()
                if IP_RE.fullmatch(text) and ":" not in text:
                    return text
            except Exception:
                continue
        time.sleep(0.5)
    return None


def split_on() -> bool:
    return _first(ctl("get", "splittunnel", timeout=20)).lower() == "true"


def online(tries: int = 3) -> bool:
    for i in range(max(1, tries)):
        if public_ip(1):
            return True
        if i + 1 < tries:
            time.sleep(0.8)
    return False


def current_ip() -> str | None:
    return public_ip()


def _connect_once(target: str | None, wait: float) -> str | None:
    if target:
        ctl("set", "region", target, timeout=30)
    ctl("connect", timeout=90)
    if not wait_state("Connected", wait):
        return None
    ip = public_ip4(2) or public_ip(2)
    return ip


def connect(target: str | None = None, wait: float = 75.0, tries: int = 2,
            avoid_ip: str | None = None) -> bool:
    for attempt in range(1, tries + 1):
        ip = _connect_once(target, wait)
        if ip and (avoid_ip is None or ip != avoid_ip):
            return True
        if ip:
            print(f"[VPN] IP trùng ({ip}) -> reconnect lấy server khác")
        else:
            print(f"[VPN] connect không ra net (state={state()}) -> thử lại "
                  f"{attempt}/{tries}")
        ctl("disconnect", timeout=30)
        wait_state("Disconnected", 15)
        time.sleep(0.6)
    ip = _connect_once(target, wait)
    if ip and avoid_ip and ip == avoid_ip:
        print(f"[VPN] tunnel rò: IP ra = IP nhà ({ip}) -> FAIL")
        ctl("disconnect", timeout=30)
        wait_state("Disconnected", 15)
        return False
    return bool(ip)


def reconnect(target: str | None = None, avoid_ip: str | None = None,
              wait: float = 75.0) -> bool:
    ctl("disconnect", timeout=30)
    wait_state("Disconnected", 20)
    time.sleep(0.5)
    return connect(target, wait=wait, tries=2, avoid_ip=avoid_ip)


def disconnect() -> None:
    ctl("disconnect", timeout=30)
    wait_state("Disconnected", 20)


def ensure(target: str | None = None, wait: float = 60.0) -> bool:
    st = state()
    if st == "Connected" and online(3):
        return True
    if st in NETLOCK_TIMEOUT:
        wait_state("Connected", 10)
        if state() == "Connected" and online(2):
            return True
    print(f"[VPN] ensure: state={st} -> connect lại")
    return connect(target, wait=wait, tries=1)


def prepare() -> dict:
    info = {"background": False, "netlock_before": "", "netlock_off": False,
            "state_before": state().lower(), "region_before": region(),
            "split_before": False}
    ctl("background", "enable", timeout=30)
    info["background"] = True
    if split_on():
        info["split_before"] = True
        ctl("set", "splittunnel", "false", timeout=30)
        if split_on():
            print("[VPN] CẢNH BÁO: split tunnel vẫn BẬT (cần admin) -> tunnel "
                  "có thể rò ra IP nhà. Chạy: expressvpnctl set splittunnel false")
        else:
            print("[VPN] split tunnel đã tắt (tránh tunnel rò)")
    before = networklock()
    info["netlock_before"] = before
    if before == "true":
        info["netlock_off"] = set_networklock(False)
        print("[VPN] Network Lock đang BẬT -> tắt tạm thời (tránh mất mạng "
              "khi tool reconnect)")
    return info


def restore(info: dict | None) -> None:
    was_off = bool(info and info.get("state_before", "").startswith("disconnect"))
    if was_off:
        try:
            disconnect()
            print("[VPN] đã tắt VPN (trả về trạng thái trước reg)")
        except Exception as e:
            print(f"[VPN] không tắt được VPN: {type(e).__name__}")
    else:
        ok = ensure()
        if not ok:
            print("[VPN] cảnh báo: không khôi phục được kết nối")
    if info and info.get("netlock_before") == "true":
        if networklock() != "true":
            set_networklock(True)
            print("[VPN] đã bật lại Network Lock")
