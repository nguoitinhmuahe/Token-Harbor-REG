from __future__ import annotations

import os
import subprocess
import sys

CTL = r"C:\Program Files\ExpressVPN\expressvpnctl.exe"

KEEP = ("python.exe", "pythonw.exe", "camoufox.exe", "expressvpn", "lightway")
BYPASS_DEFAULT = (
    "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe",
    "Code.exe", "Code - Insiders.exe", "devenv.exe", "pycharm64.exe",
    "discord.exe", "slack.exe", "Spotify.exe", "Telegram.exe", "zalo.exe",
    "OneDrive.exe", "Dropbox.exe", "steam.exe", "explorer.exe",
)


def ctl(*args: str, timeout: float = 60.0) -> str:
    try:
        p = subprocess.run([CTL, *args], capture_output=True, text=True,
                           timeout=timeout)
        return ((p.stdout or "") + (p.stderr or "")).strip()
    except Exception as e:
        return f"<ctl error: {e}>"


def split_on() -> bool:
    return ctl("get", "splittunnel").strip().lower().startswith("true")


def rules() -> list[str]:
    out = ctl("get", "split-app")
    out = out.strip().strip("[]")
    return [r.strip() for r in out.replace("\n", ",").split(",") if r.strip()]


def add_bypass(path: str) -> str:
    return ctl("set", "split-app", f"bypass:{path}")


def remove(path: str) -> str:
    return ctl("set", "split-app", f"remove:{path}")


def running_paths() -> dict[str, str]:
    ps = ("Get-Process -ErrorAction SilentlyContinue | "
          "Where-Object { $_.Path } | "
          "Select-Object -Property ProcessName,Path | "
          "ForEach-Object { $_.ProcessName + '|' + $_.Path }")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=60)
    except Exception:
        return {}
    out = {}
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if "|" in line:
            name, path = line.split("|", 1)
            out.setdefault(name.strip().lower(), path.strip())
    return out


def enabled_split():
    cur = split_on()
    if not cur:
        print("  splittunnel: dang TAT -> bat len")
        print("  " + ctl("set", "splittunnel", "true"))
    return True


def main() -> int:
    args = sys.argv[1:]
    cmd = (args[0] if args else "status").lower()

    if cmd == "status":
        print(f"splittunnel   : {ctl('get', 'splittunnel')}")
        rs = rules()
        print(f"rules ({len(rs)}):")
        for r in rs:
            print("   ", r)
        print(f"keep (luon VPN): {', '.join(KEEP)}")
        return 0

    if cmd == "keep":
        print("\n".join(KEEP))
        return 0

    if cmd == "on":
        print(ctl("set", "splittunnel", "true"))
        print(ctl("connect"))
        return 0

    if cmd == "off":
        print(ctl("set", "splittunnel", "false"))
        return 0

    if cmd == "add":
        if len(args) < 2:
            print("usage: add <path.exe>")
            return 2
        print(add_bypass(args[1]))
        print(ctl("connect"))
        return 0

    if cmd == "remove":
        if len(args) < 2:
            print("usage: remove <path>")
            return 2
        print(remove(args[1]))
        return 0

    if cmd == "auto":
        enabled_split()
        have = running_paths()
        cur = " ".join(rules()).lower()
        print(f"app dang chay: {len(have)}")
        added = 0
        for name in BYPASS_DEFAULT:
            path = have.get(name)
            if not path:
                continue
            if any(k in name for k in KEEP):
                continue
            if path.lower() in cur:
                continue
            print(f"  bypass {name}: {ctl('set', 'split-app', f'bypass:{path}')}")
            added += 1
        for name, path in have.items():
            if any(name.startswith(k.split('.')[0]) for k in ("python", "camoufox")):
                if path.lower() in cur:
                    print(f"  !! {name} dang bi bypass -> go rule: "
                          f"{ctl('set', 'split-app', f'remove:{path}')}")
        print(f"them {added} rule. ap dung (reconnect)...")
        print(ctl("connect"))
        print("xong. kiem tra: python split_tunnel.py status")
        return 0

    print("split_tunnel.py status|auto|on|off|keep|add <path.exe>|remove <path>")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
