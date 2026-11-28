
from __future__ import annotations

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import vpn

GAP = float(os.environ.get("VPN_GAP") or 0)
MIN_GAP = float(os.environ.get("VPN_MIN_GAP") or 60)
MAX_FAIL = int(os.environ.get("VPN_MAX_FAIL") or 5)
DRY = (os.environ.get("VPN_DRY") or "0").strip().lower() not in ("0", "false", "no", "off")
MAXS = float(os.environ.get("VPN_MAX_S") or 0)
LOGP = os.environ.get("VPN_LOG") or os.path.join(HERE, "logs", "vpn.log")

TRIG = os.path.join(HERE, ".vpn_rotate_req")
SWITCH = os.path.join(HERE, ".vpn_switching")
STOP = os.path.join(HERE, ".vpn_stop")
STATE = os.path.join(HERE, ".vpn_state.json")
ARMED = os.path.join(HERE, ".vpn_armed")


def log(*parts) -> None:
    line = time.strftime("%H:%M:%S ") + " ".join(str(p) for p in parts)
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOGP), exist_ok=True)
        with open(LOGP, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def save_state(d: dict) -> None:
    try:
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def load_state() -> dict:
    try:
        with open(STATE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def touch(path: str) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
    except Exception:
        pass


def untouch(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def pick_target(hosts: list[str], cur: str, idx: int) -> tuple[str, int]:
    for _ in range(len(hosts) + 1):
        idx = (idx + 1) % max(1, len(hosts))
        t = hosts[idx]
        if t != cur:
            return t, idx
    return (hosts[0] if hosts else "smart"), 0


def do_switch(hosts: list[str], idx: int) -> tuple[bool, int]:
    cur = vpn.region()
    ip0 = vpn.current_ip()
    target, idx = pick_target(hosts, cur, idx)
    log(f"[i] change IP: {cur} ({ip0}) -> {target}")
    if DRY:
        log("[i] VPN_DRY=1 -> only log, do not actually change")
        return True, idx

    touch(SWITCH)
    try:
        t0 = time.time()
        ok = vpn.reconnect(target=target, avoid_ip=ip0)
        if not ok:
            log(f"    ! connect {target} fail -> rescue")
            ok = vpn.ensure(target)
        if not ok:
            t2, idx2 = pick_target(hosts, target, idx)
            log(f"    ! try another host: {t2}")
            ok = vpn.reconnect(target=t2, avoid_ip=ip0)
            target = t2
            idx = idx2
        dt = time.time() - t0
        ip = vpn.current_ip()
        log(f"    {'OK' if ok else 'FAIL'} in {dt:.0f}s | "
            f"region={vpn.region()} ip={ip} state={vpn.state()}")
        st = load_state()
        st.update({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "region": vpn.region(),
                   "ip": ip, "switches": int(st.get("switches", 0)) + 1,
                   "last_switch_s": round(time.time(), 0), "idx": idx,
                   "ok": bool(ok)})
        if ok:
            st["last_ok"] = st["ts"]
        save_state(st)
        return bool(ok), idx
    finally:
        untouch(SWITCH)


def main() -> int:
    hosts = vpn.pool()
    if not hosts:
        log("[x] could not get the region list")
        return 2

    st = load_state()
    idx = int(st.get("idx") or 0)
    last = float(st.get("last_switch_s") or 0)

    log(f"[ok] rotator | {len(hosts)} usable hosts | "
        f"now: {vpn.region()} {vpn.current_ip()} | "
        f"cycle={'%.0fs' % GAP if GAP > 0 else 'only when requested'} "
        f"| min {MIN_GAP:.0f}s{' | DRY' if DRY else ''}")
    log(f"     change request: create file {TRIG}  (the gate does this itself on 429)")

    info = None if DRY else vpn.prepare()
    fails = 0
    t_start = time.time()
    next_periodic = time.time() + GAP if GAP > 0 else None
    try:
        os.remove(STOP)
    except OSError:
        pass

    touch(ARMED)
    untouch(SWITCH)

    try:
        while True:
            time.sleep(2)
            now = time.time()
            if os.path.exists(STOP):
                log("[i] stop requested (.vpn_stop) -> exit")
                break
            if MAXS > 0 and now - t_start > MAXS:
                log(f"[i] ran out the simulated {MAXS:.0f}s -> exit")
                break

            want = os.path.exists(TRIG)
            due = next_periodic is not None and now >= next_periodic
            if not (want or due):
                continue

            if now - last < MIN_GAP:
                continue

            reason = "429" if want else "cycle"
            if want:
                untouch(TRIG)
            log(f"[i] rotate IP (reason: {reason})")
            ok, idx = do_switch(hosts, idx)
            last = time.time()

            if due:
                next_periodic = now + GAP

            if ok:
                fails = 0
            else:
                fails += 1
                if fails >= MAX_FAIL:
                    log(f"[!] {fails} consecutive IP change fails - check "
                        f"the ExpressVPN daemon")
    except KeyboardInterrupt:
        log("[i] stopped on request")
    finally:
        untouch(SWITCH)
        untouch(TRIG)
        untouch(ARMED)
        if info is not None:
            vpn.restore(info)
        log(f"[i] finished | region={vpn.region()} ip={vpn.current_ip()} "
            f"state={vpn.state()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
