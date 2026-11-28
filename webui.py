
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import zipfile
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import usage_watch as uw

try:
    PORT = int(os.environ.get("WEBUI_PORT") or 8787)
except ValueError:
    PORT = 8787
TOKENS = os.path.join(HERE, "tokens.txt")
ACCOUNTS_DB = os.path.join(HERE, "accounts.db")
STOP_FILE = os.path.join(HERE, ".reg_stop")
INDEX = os.path.join(HERE, "webui.html")
LOG_DIR = os.path.join(HERE, "logs")

def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except Exception:
        return ""


_NINE_TOK: dict = {"v": None}


def nine_cli_token() -> str:
    if _NINE_TOK["v"] is not None:
        return _NINE_TOK["v"]
    home = os.environ.get("ROUTER_HOME") or (
        r"%APPDATA%\9router" if os.name == "nt" else "~/.config/9router")
    home = os.path.expandvars(os.path.expanduser(home))
    mid = _read_text(os.path.join(home, "machine-id"))
    if not mid and os.name == "nt":
        try:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                               r"SOFTWARE\Microsoft\Cryptography", 0,
                               winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
            mid = str(winreg.QueryValueEx(k, "MachineGuid")[0]).strip()
        except Exception:
            mid = ""
    sec = _read_text(os.path.join(home, "auth", "cli-secret"))
    tok = ""
    if mid:
        tok = hashlib.sha256(
            (mid + "9r-cli-auth" + sec).encode("utf-8")).hexdigest()[:16]
    _NINE_TOK["v"] = tok
    return tok


ROUTER_CFG_FILE = os.path.join(HERE, "router.json")


def _load_router_cfg() -> dict:
    cfg = {"url": (os.environ.get("ROUTER_URL")
                   or "http://127.0.0.1:20128").rstrip("/"),
           "token": os.environ.get("ROUTER_TOKEN") or "",
           "provider": (os.environ.get("USAGE_PROVIDER")
                        or os.environ.get("REG_ROUTER_PROVIDER") or "a").strip()}
    try:
        with open(ROUTER_CFG_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        for k in ("url", "token", "provider"):
            if isinstance(saved.get(k), str) and saved[k]:
                cfg[k] = saved[k]
        cfg["url"] = cfg["url"].rstrip("/")
    except Exception:
        pass
    return cfg


ROUTER_CFG = _load_router_cfg()

uw.BASE = ROUTER_CFG["url"]
uw.PROVIDER = ROUTER_CFG["provider"]
uw.TOKEN = ROUTER_CFG["token"]
uw.NINE_TOKEN = nine_cli_token()


def _auth_headers(token: str, extra: dict | None = None) -> dict:
    h = {"Accept": "application/json"}
    nt = nine_cli_token()
    if nt:
        h["x-9r-cli-token"] = nt
    if token:
        h["Authorization"] = "Bearer " + token
        h["x-api-key"] = token
    if extra:
        h.update(extra)
    return h


def _router_headers(extra: dict | None = None) -> dict:
    return _auth_headers(ROUTER_CFG.get("token") or "", extra)


def _router_get(base: str, token: str, path: str, timeout: float = 8.0):
    req = urllib.request.Request(base.rstrip("/") + path,
                                 headers=_auth_headers(token))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
    return json.loads(raw) if raw.strip().startswith(("{", "[")) else {}


def _router_probe_error(e: Exception) -> str:
    if isinstance(e, urllib.error.HTTPError):
        if e.code in (401, 403):
            return (f"HTTP {e.code} - the router's admin API wants a token. "
                    "If the router (9router) runs on this machine the panel "
                    "auto-detects it; otherwise set ROUTER_HOME to its data dir "
                    "or paste the router API key into API token and press Load nodes.")
        return f"HTTP {e.code} {e.reason}"
    return f"{type(e).__name__}: {e}"


def router_call(path: str, method: str = "GET", body: dict | None = None,
              timeout: float = 20.0):
    data = json.dumps(body).encode() if body is not None else None
    headers = _router_headers({"Content-Type": "application/json"}
                              if data is not None else None)
    req = urllib.request.Request(uw.BASE + path, data=data, method=method,
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        status = r.status
        raw = r.read().decode("utf-8", "replace")
    try:
        body = json.loads(raw) if raw.strip().startswith(("{", "[")) else {}
    except ValueError:
        body = {"raw": raw[:300]}
    return status, body


VERSION_FILE = os.path.join(HERE, "VERSION")
REPO = os.environ.get("UPDATE_REPO") or "nguoitinhmuahe/Token-Harbor-REG"
REPO_URL = "https://github.com/" + REPO
_RAW_VERSION = "https://raw.githubusercontent.com/" + REPO + "/main/VERSION"
_VER_CACHE: dict = {"at": 0.0, "data": None}


def _cur_version() -> str:
    try:
        with open(VERSION_FILE, "r", encoding="utf-8") as f:
            return f.read().strip() or "?"
    except Exception:
        return "?"


def _git(args, timeout: float = 30.0):
    try:
        r = subprocess.run(["git", "-C", HERE] + args, capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=timeout)
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except Exception as e:
        return 1, "", f"{type(e).__name__}: {e}"


NOTICE_FILE = os.path.join(HERE, "NOTICE.json")


def notice() -> dict:
    cur = _cur_version()
    out = {"version": cur, "changes": [], "discord": "", "welcome": ""}
    try:
        with open(NOTICE_FILE, "r", encoding="utf-8") as f:
            j = json.load(f)
        out["discord"] = str(j.get("discord") or "")
        out["welcome"] = str(j.get("welcome") or "")
        out["changes"] = list((j.get("changes") or {}).get(cur) or [])
    except Exception:
        pass
    return out


def check_version(force: bool = False) -> dict:
    now = time.time()
    if not force and _VER_CACHE["data"] and now - _VER_CACHE["at"] < 600:
        return _VER_CACHE["data"]
    cur = _cur_version()
    is_git = os.path.isdir(os.path.join(HERE, ".git"))
    out = {"current": cur, "latest": cur, "update_available": False,
           "git": is_git, "repo": REPO, "url": REPO_URL, "checked": int(now),
           "note": ""}
    try:
        req = urllib.request.Request(_RAW_VERSION,
                                     headers={"User-Agent": "token-harbor-reg"})
        with urllib.request.urlopen(req, timeout=5) as r:
            out["latest"] = r.read().decode("utf-8", "replace").strip() or cur
    except Exception as e:
        out["note"] = "remote check failed: " + type(e).__name__
    if is_git:
        rc, local, _ = _git(["rev-parse", "HEAD"])
        if rc == 0:
            out["local_sha"] = local[:8]
        try:
            req = urllib.request.Request(
                f"https://api.github.com/repos/{REPO}/commits/main",
                headers={"Accept": "application/vnd.github+json",
                         "User-Agent": "token-harbor-reg"})
            with urllib.request.urlopen(req, timeout=6) as r:
                j = json.loads(r.read().decode("utf-8", "replace"))
            out["remote_sha"] = (j.get("sha") or "")[:8]
        except Exception:
            pass
        if out.get("local_sha") and out.get("remote_sha"):
            out["update_available"] = out["local_sha"] != out["remote_sha"]
    if out["latest"] != cur:
        out["update_available"] = True
    _VER_CACHE["at"] = now
    _VER_CACHE["data"] = out
    return out


def do_update() -> dict:
    if os.path.isdir(os.path.join(HERE, ".git")):
        rc, out, err = _git(["pull", "--ff-only"])
        _VER_CACHE["data"] = None
        tail = (out + ("\n" + err if err else "")).strip()[-500:]
        return {"ok": rc == 0, "restart": rc == 0, "how": "git",
                "msg": tail or ("git pull rc=" + str(rc))}

    url = f"https://github.com/{REPO}/archive/refs/heads/main.zip"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "token-harbor-reg"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
    except Exception as e:
        return {"ok": False, "restart": False, "how": "zip",
                "msg": f"download failed: {type(e).__name__}: {e}"}

    keep = {"router.json", "tokens.txt", "accounts.txt", ".env", ".env.bak"}
    skip_dirs = ("logs/", "debug/")
    n = 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for m in z.namelist():
                parts = m.split("/", 1)
                if len(parts) < 2 or not parts[1] or m.endswith("/"):
                    continue
                rel = parts[1]
                base = os.path.basename(rel)
                if base in keep or rel.startswith(skip_dirs) or base.endswith(".db"):
                    continue
                dst = os.path.join(HERE, *rel.split("/"))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with z.open(m) as src, open(dst, "wb") as out:
                    shutil.copyfileobj(src, out)
                n += 1
    except Exception as e:
        return {"ok": False, "restart": False, "how": "zip",
                "msg": f"extract failed: {type(e).__name__}: {e}"}
    _VER_CACHE["data"] = None
    _NINE_TOK["v"] = None
    return {"ok": True, "restart": True, "how": "zip",
            "msg": f"downloaded and wrote {n} files from {REPO}@main"}


def _int(v, default: int) -> int:
    try:
        return int(v)
    except Exception:
        return default


LOG_MAX_MB = float(os.environ.get("PANEL_LOG_MAX_MB") or 8)


def _open_log(name: str):
    path = os.path.join(LOG_DIR, name)
    try:
        if os.path.getsize(path) > LOG_MAX_MB * 1024 * 1024:
            try:
                os.replace(path, path + ".1")
            except Exception:
                pass
    except OSError:
        pass
    try:
        return open(path, "a", encoding="utf-8", errors="replace")
    except Exception:
        return None


def _wdog(msg: str, *args) -> None:
    try:
        line = time.strftime("%H:%M:%S ") + (msg % args if args else msg)
    except Exception:
        line = f"{time.strftime('%H:%M:%S')} {msg}"
    print("[wdog] " + line, flush=True)
    try:
        with open(os.path.join(LOG_DIR, "watchdog.log"), "a",
                  encoding="utf-8", errors="replace") as f:
            f.write(line + "\n")
    except Exception:
        pass


def tokens_keys() -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    try:
        with open(TOKENS, "r", encoding="utf-8") as f:
            for line in f:
                k = line.strip()
                if k.startswith("thk_") and k not in seen:
                    seen.add(k)
                    out.append(k)
    except Exception:
        pass
    return out


_STATE_LOCK = threading.Lock()


def state_put(cid: str, entry: dict | None) -> None:
    try:
        with _STATE_LOCK:
            st = uw.load_state()
            if entry is None:
                if cid not in st:
                    return
                st.pop(cid, None)
            else:
                st[cid] = entry
            uw.save_state(st)
    except Exception:
        pass


def accounts_rows(limit: int = 500) -> list[dict]:
    if not os.path.exists(ACCOUNTS_DB):
        return []
    try:
        c = sqlite3.connect("file:" + ACCOUNTS_DB.replace("\\", "/") + "?mode=ro",
                            uri=True, timeout=5)
        cur = c.execute(
            "select email, api_key, status, created_at from accounts "
            "order by created_at desc limit ?", (limit,))
        rows = [{"email": r[0], "key": r[1] or "", "status": r[2] or "",
                 "at": r[3] or ""} for r in cur.fetchall()]
        c.close()
        return rows
    except Exception:
        return []


class Runner:
    def __init__(self):
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.lines: deque = deque(maxlen=6000)
        self.seq = 0
        self.started = 0.0
        self.cfg: dict = {}
        self.ok = 0
        self.fail = 0
        self.total = 0
        self.remaining = None
        self.stats: dict[str, str] = {}
        self.rate = ""
        self.logfile = ""
        self.done_at = 0.0

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, cfg: dict) -> tuple[bool, str]:
        with self.lock:
            if self.running():
                return False, "already running"
            try:
                os.remove(STOP_FILE)
            except Exception:
                pass
            env = dict(os.environ)
            env.update({
                "PYTHONIOENCODING": "utf-8",
                "REG_COUNT": str(_int(cfg.get("count"), 10)),
                "REG_CONCURRENT": str(max(1, _int(cfg.get("concurrent"), 5))),
                "REG_PER_IP": str(max(1, _int(cfg.get("per_ip"), 5))),
                "REG_NET": str(cfg.get("net") or "express"),
                "REG_APPEND": "1",
                "REG_VERBOSE": "1",
                "REG_WEB": "1" if cfg.get("web", True) else "0",
                "REG_ROUTER": "1" if cfg.get("prov", True) else "0",
                "REG_HEADLESS": "1" if cfg.get("headless", True) else "0",
                "MAIL_PROVIDER": str(cfg.get("mail") or "mailtm"),
            })
            if cfg.get("router_provider"):
                env["REG_ROUTER_PROVIDER"] = str(cfg["router_provider"])
            env["ROUTER_URL"] = ROUTER_CFG["url"]
            env["ROUTER_TOKEN"] = ROUTER_CFG["token"]
            env["ROUTER_CLI_TOKEN"] = nine_cli_token()
            env["REG_ROUTER_PROVIDER"] = ROUTER_CFG["provider"]
            os.makedirs(LOG_DIR, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            self.logfile = os.path.join(LOG_DIR, f"reg_{stamp}.log")
            try:
                f = open(self.logfile, "w", encoding="utf-8", errors="replace")
            except Exception:
                f = None
            creation = 0x00000200 if os.name == "nt" else 0
            try:
                self.proc = subprocess.Popen(
                    [sys.executable, "-u", "main.py"],
                    cwd=HERE, env=env, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                    errors="replace", bufsize=1, creationflags=creation,
                    stdin=subprocess.DEVNULL)
            except Exception as e:
                try:
                    if f:
                        f.close()
                except Exception:
                    pass
                self.proc = None
                return False, f"could not spawn: {e}"
            self._logf = f
            self.lines.clear()
            self.seq = 0
            self.ok = self.fail = 0
            self.stats = {}
            self.total = _int(cfg.get("count"), 0)
            self.remaining = self.total
            self.rate = ""
            self.started = time.time()
            self.done_at = 0.0
            self.cfg = dict(cfg)
            threading.Thread(target=self._reader, args=(self.proc,),
                             daemon=True).start()
            return True, "ok"

    def _reader(self, proc: subprocess.Popen):
        try:
            for line in proc.stdout:
                self._ingest(line.rstrip("\r\n"))
        except Exception:
            pass
        finally:
            try:
                rc = proc.wait(timeout=5)
            except Exception:
                rc = None
            self._ingest(f"[WEB] process exited (exit {rc})")
            self.done_at = time.time()
            try:
                if self._logf:
                    self._logf.close()
            except Exception:
                pass

    def _ingest(self, line: str):
        self.seq += 1
        self.lines.append((self.seq, line))
        f = getattr(self, "_logf", None)
        if f:
            try:
                f.write(line + "\n")
                f.flush()
            except Exception:
                pass
        m = re.match(r"\[STAT\] (\S+) .*-> (\S+)\s*$", line)
        if m:
            self.stats[m.group(1)] = m.group(2)
            self.ok = sum(1 for v in self.stats.values() if v == "live")
        m = re.match(r"\[DONE\] (\S+)", line)
        if m:
            self.stats[m.group(1)] = "live"
            self.ok = sum(1 for v in self.stats.values() if v == "live")
        m = re.match(r"\[FAIL\] (\S+)", line)
        if m:
            self.stats.setdefault(m.group(1), "fail")
        m = re.search(r"\[START\] Total: (\d+)", line)
        if m:
            self.total = int(m.group(1))
            self.remaining = self.total
        m = re.search(r"\[BATCH\] left (\d+)/(\d+)", line)
        if m:
            self.remaining = int(m.group(1))
            self.total = int(m.group(2))
        m = re.search(r"elapsed (\d+):(\d+) \| ([\d.]+) acc/h", line)
        if m:
            self.rate = f"{m.group(3)} acc/h"

    def stop(self) -> tuple[bool, str]:
        if not self.running():
            return False, "not running"
        try:
            with open(STOP_FILE, "w", encoding="utf-8") as f:
                f.write("stop")
            return True, "stop requested (current batch will finish)"
        except Exception as e:
            return False, f"could not write sentinel: {e}"

    def kill(self) -> tuple[bool, str]:
        p = self.proc
        if p is None or p.poll() is not None:
            return False, "not running"
        try:
            p.kill()
            return True, "force-killed"
        except Exception as e:
            return False, str(e)

    def state(self) -> dict:
        run = self.running()
        el = (time.time() - self.started) if self.started else 0
        left = self.remaining
        done = max(0, self.total - left) if (self.total and left is not None) else 0
        return {
            "running": run, "ok": self.ok,
            "fail": len(self.stats) - self.ok,
            "attempts": len(self.stats),
            "total": self.total, "done_n": done,
            "remaining": left,
            "elapsed": int(el), "rate": self.rate,
            "cfg": self.cfg, "logfile": self.logfile,
            "done": (not run and self.started > 0),
        }

    def tail(self, since: int, limit: int = 400) -> dict:
        out = [ln for (s, ln) in list(self.lines) if s > since][-limit:]
        return {"seq": self.seq, "lines": out}


RUNNER = Runner()


RANK = {"exhausted": 0, "ok": 1, "zero": 2, "dead": 3, "unknown": 4}


class Usage:

    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.rows: list[dict] = []
        self.at = 0.0
        self.err = ""
        self.progress = 0
        self.total = 0
        self.threshold = 100.0
        self.started = 0.0
        self.finished = False

    def scan(self, workers: int = 12, threshold: float = 100.0):
        with self.lock:
            if self.running:
                return False, "already scanning"
            self.running = True
            self.finished = False
            self.err = ""
            self.rows = []
            self.progress = 0
            self.total = 0
            self.at = 0.0
            self.started = time.time()
            self.threshold = threshold if 0 < threshold <= 100 else 100.0
        threading.Thread(target=self._run, args=(max(1, workers),),
                         daemon=True).start()
        return True, "ok"

    @staticmethod
    def _classify(res: dict, threshold: float) -> tuple[str, dict]:
        grp, d = uw.classify(res)
        if grp != "exhausted":
            used = d.get("used_pct")
            if isinstance(used, (int, float)) and float(used) >= threshold:
                grp = "exhausted"
        return grp, d

    def _run(self, workers: int):
        try:
            nid = uw.node_id()
            conns = uw.connections(nid)
            keys = uw.read_keys({c["id"] for c in conns})
            uw._KEYMAP.update(keys)
            alive = [c for c in conns if keys.get(c["id"])]
            if not alive:
                raise RuntimeError("no keys found in the router")
            by_key: dict[str, list[dict]] = {}
            for c in alive:
                by_key.setdefault(keys[c["id"]], []).append(c)
            with self.lock:
                self.total = len(alive)

            threshold = self.threshold

            def probe(key: str):
                try:
                    return key, uw.check_key(key)
                except Exception as e:
                    return key, {"ok": False, "code": 0,
                                 "body": type(e).__name__}

            def push(key: str, res: dict):
                grp, d = self._classify(res, threshold)
                raw_used = d.get("used_pct")
                used = float(raw_used) if isinstance(raw_used, (int, float)) else None
                reset_h = ""
                try:
                    reset_h = uw.fmt_reset(d) if d else ""
                except Exception:
                    reset_h = ""
                out = [{
                    "id": c["id"], "name": uw.name_of(c), "tail": key[-6:],
                    "key": key,
                    "active": bool(c.get("isActive")),
                    "grp": grp, "used": used,
                    "req": d.get("req_used"),
                    "reset": d.get("reset_at") or "",
                    "reset_h": reset_h,
                    "exhausted": bool(d.get("exhausted")) or grp == "exhausted",
                    "code": d.get("code"),
                } for c in by_key[key]]
                with self.lock:
                    self.rows.extend(out)
                    self.progress += len(out)

            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(probe, k): k for k in by_key}
                for fut in as_completed(futs):
                    try:
                        key, res = fut.result()
                    except Exception:
                        key = futs[fut]
                        res = {"ok": False, "code": 0, "body": "future"}
                    push(key, res)

            with self.lock:
                self.rows.sort(key=lambda r: (RANK.get(r["grp"], 9),
                                              -(r["used"] or 0), r["name"]))
                self.at = time.time()
                self.finished = True
        except Exception as e:
            self.err = f"{type(e).__name__}: {e}"
            traceback.print_exc()
        finally:
            with self.lock:
                self.running = False

    def snapshot(self, since: int = 0) -> dict:
        with self.lock:
            n = len(self.rows)
            since = max(0, min(since, n))
            return {"running": self.running, "finished": self.finished,
                    "at": self.at, "started": self.started, "now": time.time(),
                    "err": self.err, "progress": self.progress,
                    "total": self.total, "count": n, "since": since,
                    "threshold": self.threshold, "rows": self.rows[since:]}


USAGE = Usage()


import pool as npool

GATE_PORT = _int(os.environ.get("GATE_PORT"), 20130)
GATE_URL = f"http://127.0.0.1:{GATE_PORT}"
GATE_SCRIPT = os.path.join(HERE, "gate.py")
VPN_SCRIPT = os.path.join(HERE, "rotator.py")
POOL_SCRIPT = os.path.join(HERE, "pool.py")
QUOTA_JSON = os.path.join(HERE, ".pool_quota.json")
VPN_TRIG = os.path.join(HERE, ".vpn_rotate_req")
VPN_STATE = os.path.join(HERE, ".vpn_state.json")
VPN_STOP = os.path.join(HERE, ".vpn_stop")


def read_json(path: str, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def touch_file(path: str) -> bool:
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
        return True
    except Exception:
        return False


class Gate:

    def __init__(self):
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.rot: subprocess.Popen | None = None
        self.quota_proc: subprocess.Popen | None = None
        self.own = False
        self.wanted = False
        self.rpm = os.environ.get("GATE_RPM") or "0"
        self.vpn = "1" if (os.environ.get("WEBUI_VPN") or "0") not in (
            "0", "", "false", "no", "off") else "0"
        os.makedirs(LOG_DIR, exist_ok=True)

    def stats(self) -> dict | None:
        try:
            with urllib.request.urlopen(GATE_URL + "/gate/stats", timeout=2.5) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
            d["running"] = True
            return d
        except Exception:
            return None

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> tuple[bool, str]:
        with self.lock:
            self.wanted = True
            if self.stats():
                self.own = True
                return True, "gateway already running"
            env = dict(os.environ)
            env.update({"PYTHONIOENCODING": "utf-8", "GATE_RPM": str(self.rpm),
                        "GATE_VPN": self.vpn})
            env.setdefault("GATE_KEYPOOL", "1")
            boot = os.path.join(HERE, "gate_boot.py")
            if os.path.exists(boot):
                try:
                    f = _open_log("gate_web.log")
                    self.proc = subprocess.Popen(
                        [sys.executable, "-u", "gate_boot.py"], cwd=HERE, env=env,
                        stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                        creationflags=0x00000200 if os.name == "nt" else 0)
                    for _ in range(24):
                        time.sleep(0.5)
                        if self.stats():
                            self.own = True
                            return True, "front door started"
                except Exception:
                    pass
            try:
                f = _open_log("gate_web.log")
                self.proc = subprocess.Popen(
                    [sys.executable, "-u", GATE_SCRIPT], cwd=HERE, env=env,
                    stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                    creationflags=0x00000200 if os.name == "nt" else 0)
                self.own = True
            except Exception as e:
                return False, f"could not start: {e}"
            return True, "gateway started"

    def stop(self) -> tuple[bool, str]:
        with self.lock:
            self.wanted = False
            if self.proc and self.proc.poll() is None:
                try:
                    self.proc.terminate()
                except Exception:
                    pass
            self.proc = None
            self.own = False
        return True, "gateway stopped"

    def watch(self, every: float = 5.0, misses: int = 2):
        def loop():
            down = 0
            since = 0.0
            while True:
                time.sleep(every)
                try:
                    if self.stats():
                        if down:
                            _wdog("gateway answered again on %s (was down %.0fs)",
                                  GATE_URL, time.time() - since)
                        down = 0
                        continue
                    if not self.wanted:
                        down = 0
                        continue
                    down += 1
                    if down < misses:
                        continue
                    if not since:
                        since = time.time()
                    _wdog("gateway silent on %s (%d probes) -> restarting",
                          GATE_URL, down)
                    with self.lock:
                        self.proc = None
                        self.own = False
                    ok, msg = self.start()
                    _wdog("restart: %s", msg)
                    if self.stats():
                        _wdog("gateway up on %s (down %.0fs)", GATE_URL,
                              time.time() - since)
                        down = 0
                        since = 0.0
                except Exception as e:
                    _wdog("watchdog iteration failed: %r", e)
        threading.Thread(target=loop, daemon=True).start()

    def set_rpm(self, rpm) -> None:
        self.rpm = str(_int(rpm, 0))

    def rotate(self) -> tuple[bool, str]:
        if touch_file(VPN_TRIG):
            return True, "IP rotation requested"
        return False, "could not write rotate request"

    def rot_alive(self) -> bool:
        return self.rot is not None and self.rot.poll() is None

    def rot_start(self) -> tuple[bool, str]:
        with self.lock:
            if self.rot_alive():
                return True, "rotator already running"
            self.vpn = "1"
            try:
                os.remove(VPN_STOP)
            except OSError:
                pass
            env = dict(os.environ)
            env.update({"PYTHONIOENCODING": "utf-8", "VPN_GAP": "0"})
            try:
                f = _open_log("vpn_rotate.log")
                self.rot = subprocess.Popen(
                    [sys.executable, "-u", VPN_SCRIPT], cwd=HERE, env=env,
                    stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                    creationflags=0x00000200 if os.name == "nt" else 0)
            except Exception as e:
                return False, f"could not start: {e}"
            return True, "IP rotator started"

    def rot_stop(self) -> tuple[bool, str]:
        with self.lock:
            p = self.rot
            if p is None or p.poll() is not None:
                self.rot = None
                return True, "rotator not running"
            touch_file(VPN_STOP)
        for _ in range(20):
            if p.poll() is not None:
                break
            time.sleep(0.5)
        if p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass
        with self.lock:
            self.rot = None
        try:
            os.remove(VPN_STOP)
        except OSError:
            pass
        return True, "IP rotator stopped"


GATE = Gate()


class Handler(BaseHTTPRequestHandler):
    server_version = "tokenharbor-panel/1.0"

    def log_message(self, *a):
        pass

    def _json(self, obj, code: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _file(self, path: str, ctype: str):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except Exception:
            self._json({"error": "missing " + os.path.basename(path)}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8", "replace"))
        except Exception:
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)
        try:
            if p in ("/", "/index.html"):
                return self._file(INDEX, "text/html; charset=utf-8")
            if p == "/api/state":
                st = RUNNER.state()
                u = USAGE.snapshot(len(USAGE.rows))
                st["usage"] = {"running": u["running"], "at": u["at"],
                               "count": u["count"], "err": u["err"],
                               "progress": u["progress"], "total": u["total"],
                               "finished": u["finished"],
                               "elapsed": int(u["now"] - u["started"])
                               if u["started"] else 0}
                st["counts"] = self._counts()
                st["vpn"] = getattr(self.server, "vpn_cache", {"state": "?"})
                return self._json(st)
            if p == "/api/overview":
                return self._json(self._overview())
            if p == "/api/log":
                since = int((q.get("seq") or ["0"])[0])
                return self._json(RUNNER.tail(since))
            if p == "/api/keys":
                return self._json(self._keys())
            if p == "/api/providers":
                return self._json(self._providers())
            if p == "/api/router":
                return self._json(self._router_view())
            if p == "/api/version":
                return self._json(check_version("force" in q))
            if p == "/api/usage":
                since = _int((q.get("since") or ["0"])[0], 0)
                return self._json(USAGE.snapshot(since))
            if p == "/api/accounts":
                return self._json({"rows": accounts_rows()})
            if p == "/api/notice":
                return self._json(notice())
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        b = self._body()
        try:
            if p == "/api/gate/start":
                rpm = b.get("rpm")
                if rpm is not None:
                    GATE.set_rpm(rpm)
                ok, msg = GATE.start()
                return self._json({"ok": ok, "msg": msg})
            if p == "/api/gate/stop":
                ok, msg = GATE.stop()
                return self._json({"ok": ok, "msg": msg})
            if p == "/api/gate/rpm":
                GATE.set_rpm(b.get("rpm"))
                if GATE.stats():
                    GATE.stop()
                    time.sleep(0.4)
                    ok, msg = GATE.start()
                else:
                    ok, msg = True, f"set {GATE.rpm} req/min"
                return self._json({"ok": ok, "msg": msg, "rpm": GATE.rpm})
            if p == "/api/gate/rotate":
                ok, msg = GATE.rotate()
                return self._json({"ok": ok, "msg": msg})
            if p == "/api/vpn/start":
                ok, msg = GATE.rot_start()
                return self._json({"ok": ok, "msg": msg})
            if p == "/api/vpn/stop":
                ok, msg = GATE.rot_stop()
                return self._json({"ok": ok, "msg": msg})
            if p == "/api/vpn/rotate":
                ok, msg = GATE.rotate()
                return self._json({"ok": ok, "msg": msg})
            if p == "/api/pool/rr":
                return self._json(self._pool_rr())
            if p == "/api/pool/refresh":
                return self._json(self._pool_refresh(bool(b.get("revive", True))))
            if p == "/api/quota/scan":
                return self._json(self._quota_scan())
            if p == "/api/router":
                return self._json(self._router_save(b))
            if p == "/api/router/probe":
                return self._json(self._router_view(
                    {"url": b.get("url") or ROUTER_CFG["url"],
                     "token": b.get("token", "")}))
            if p == "/api/update":
                return self._json(do_update())
            if p == "/api/reg":
                if _int(b.get("count"), 0) < 1:
                    return self._json({"ok": False, "msg": "accounts must be >= 1"})
                if str(b.get("net") or "express") not in ("express", "direct"):
                    return self._json({"ok": False, "msg": "invalid network"})
                ok, msg = RUNNER.start(b)
                return self._json({"ok": ok, "msg": msg})
            if p == "/api/reg/stop":
                ok, msg = RUNNER.stop()
                return self._json({"ok": ok, "msg": msg})
            if p == "/api/reg/kill":
                ok, msg = RUNNER.kill()
                return self._json({"ok": ok, "msg": msg})
            if p == "/api/usage/scan":
                try:
                    th = float(b.get("threshold") or 100)
                except (TypeError, ValueError):
                    th = 100.0
                ok, msg = USAGE.scan(_int(b.get("workers"), 12), th)
                return self._json({"ok": ok, "msg": msg})
            if p == "/api/providers/toggle":
                cid = str(b.get("id") or "")
                active = bool(b.get("active"))
                if not cid:
                    return self._json({"ok": False, "msg": "missing id"})
                ok = uw.set_active(cid, active)
                if ok:
                    if active:
                        state_put(cid, None)
                    else:
                        row = next((r for r in USAGE.snapshot(0)["rows"]
                                    if r["id"] == cid), None)
                        state_put(cid, {
                            "name": (row or {}).get("name") or cid[:8],
                            "at": time.time(),
                            "why": (row or {}).get("grp") or "manual",
                            "reset": (row or {}).get("reset") or ""})
                return self._json({"ok": ok})
            if p == "/api/providers/delete":
                cid = str(b.get("id") or "")
                router_call("/api/providers/" + cid, "DELETE")
                state_put(cid, None)
                return self._json({"ok": True})
            if p == "/api/providers/add":
                key = str(b.get("key") or "").strip()
                if not key.startswith("thk_"):
                    return self._json({"ok": False, "msg": "invalid key"})
                name = str(b.get("name") or ("web-" + key[-6:]))
                st, res = router_call("/api/providers", "POST", {
                    "provider": self._node_id(), "apiKey": key, "name": name,
                    "priority": 1, "isActive": True, "testStatus": "unknown",
                    "providerSpecificData": {}})
                return self._json({"ok": st in (200, 201),
                                   "msg": "ok" if st in (200, 201) else str(st)})
            if p == "/api/providers/add_missing":
                return self._json(self._add_missing(
                    bool(b.get("verify", True)), _int(b.get("workers"), 12)))
            if p == "/api/providers/disable_exhausted":
                return self._json(self._disable_bad())
            return self._json({"error": "not found"}, 404)
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                detail = ""
            return self._json({"ok": False, "msg": f"HTTP {e.code} {detail}"}, 200)
        except Exception as e:
            return self._json({"ok": False, "msg": f"{type(e).__name__}: {e}"}, 500)

    def _router_view(self, override: dict | None = None) -> dict:
        base = (override.get("url") if override else None) or ROUTER_CFG["url"]
        if override is not None and "token" in override:
            token = override.get("token") or ""
        else:
            token = ROUTER_CFG["token"]
        out = {"url": base, "provider": ROUTER_CFG["provider"],
               "token_set": bool(token), "nine_token_set": bool(nine_cli_token()),
               "nodes": [], "ok": False, "error": None}
        try:
            d = _router_get(base, token, "/api/provider-nodes")
            nodes = d.get("nodes") if isinstance(d, dict) else d
            cnt: dict = {}
            try:
                pv = _router_get(base, token, "/api/providers")
                conns = pv.get("connections") if isinstance(pv, dict) else pv
                for c in (conns or []):
                    cnt[c.get("provider")] = cnt.get(c.get("provider"), 0) + 1
            except Exception:
                pass
            out["nodes"] = [{"id": n.get("id"), "name": n.get("name") or "?",
                             "connections": cnt.get(n.get("id"), 0),
                             "base": ((n.get("data") or {}).get("baseUrl")
                                      or n.get("baseUrl") or "")}
                            for n in (nodes or [])]
            out["ok"] = True
        except Exception as e:
            out["error"] = _router_probe_error(e)
        return out

    def _router_save(self, b: dict) -> dict:
        url = str(b.get("url") or "").strip().rstrip("/")
        if url and not url.startswith(("http://", "https://")):
            return {"saved": False,
                    "msg": "router URL must start with http:// or https://"}
        prov = str(b.get("provider") or "").strip()
        tok = b.get("token")
        if url:
            ROUTER_CFG["url"] = url
        if prov:
            ROUTER_CFG["provider"] = prov
        if isinstance(tok, str) and tok.strip():
            ROUTER_CFG["token"] = tok.strip()
        elif b.get("clear_token"):
            ROUTER_CFG["token"] = ""
        try:
            with open(ROUTER_CFG_FILE, "w", encoding="utf-8") as f:
                json.dump(ROUTER_CFG, f, indent=2)
        except Exception as e:
            return {"saved": False, "msg": f"could not save: {e}"}
        uw.BASE = ROUTER_CFG["url"]
        uw.PROVIDER = ROUTER_CFG["provider"]
        uw.TOKEN = ROUTER_CFG["token"]
        self.server.__dict__.pop("_node", None)
        out = self._router_view()
        out["saved"] = True
        out["msg"] = "saved"
        return out

    def _node_id(self) -> str:
        if not hasattr(self.server, "_node"):
            self.server._node = uw.node_id()
        return self.server._node

    def _overview(self) -> dict:
        out: dict = {"reg": RUNNER.state(),
                     "counts": self._counts(),
                     "usage": {"running": USAGE.snapshot(0)["running"]}}
        try:
            nid = self._node_id()
            nodes = uw.api("/api/provider-nodes")
            nodes = nodes.get("nodes") if isinstance(nodes, dict) else nodes
            node = next((n for n in (nodes or []) if n.get("id") == nid), {}) or {}
            conns = uw.connections(nid)
            keys = uw.read_keys({c["id"] for c in conns}) if conns else {}
            codes: dict[str, int] = {}
            for c in conns:
                k = str(c.get("errorCode") or "-")
                codes[k] = codes.get(k, 0) + 1
            st = uw.api("/api/settings")
            ps = (st.get("providerStrategies") or {}).get(nid) or {}
            d = node.get("data") or {}
            out["pool"] = {
                "provider": uw.PROVIDER, "nid": nid,
                "node": node.get("name") or uw.PROVIDER,
                "baseUrl": d.get("baseUrl") or node.get("baseUrl") or "",
                "model": npool.MODEL,
                "strategy": ps.get("fallbackStrategy") or "fill-first",
                "sticky": ps.get("stickyRoundRobinLimit"),
                "conns": len(conns), "active": sum(1 for c in conns if c.get("isActive")),
                "keys": len(keys), "codes": codes,
            }
        except Exception as e:
            out["pool"] = {"error": f"{type(e).__name__}: {e}"}
        g = GATE.stats()
        if g:
            g["managed"] = GATE.alive()
        else:
            g = {"running": False, "managed": GATE.alive(), "rpm_set": float(GATE.rpm or 0)}
        g["rotator"] = GATE.rot_alive()
        g["vpn_on"] = GATE.vpn == "1"
        g["url"] = GATE_URL
        out["gate"] = g
        q = read_json(QUOTA_JSON, {}) or {}
        out["quota"] = {k: q.get(k) for k in
                        ("ts", "accs", "total_quota", "total_left", "per_acc",
                         "hist", "buckets")} if q else {}
        out["vpn"] = {"cache": getattr(self.server, "vpn_cache", {}),
                      "file": read_json(VPN_STATE, {}) or {}}
        return out

    def _pool_rr(self) -> dict:
        try:
            nid = self._node_id()
            res = npool.set_strategy(nid, "round-robin", 1)
            after = res.get("after") or {}
            return {"ok": after.get("fallbackStrategy") == "round-robin",
                    "msg": f"strategy={after.get('fallbackStrategy')} "
                           f"sticky={after.get('stickyRoundRobinLimit')}",
                    "after": after}
        except Exception as e:
            return {"ok": False, "msg": f"{type(e).__name__}: {e}"}

    def _pool_refresh(self, revive: bool = True) -> dict:
        try:
            nid = self._node_id()
            rows = npool.db_rows()
            n_lock = npool.clear_locks()
            act = on = 0
            if revive:
                for cid, j in rows.items():
                    if str(j.get("errorCode") or "-") != "-":
                        if not j.get("_active"):
                            if npool.set_active(cid, True):
                                act += 1
                        on += 1
            return {"ok": True, "locks": n_lock, "revived": act,
                    "msg": f"cleared {n_lock} locks · re-enabled {act} accounts"}
        except Exception as e:
            return {"ok": False, "msg": f"{type(e).__name__}: {e}"}

    def _quota_scan(self) -> dict:
        with GATE.lock:
            p = GATE.quota_proc
            if p is not None and p.poll() is None:
                return {"ok": True, "msg": "quota scan already running (background)"}
            env = dict(os.environ)
            env.update({"PYTHONIOENCODING": "utf-8", "POOL_MODE": "quota"})
            try:
                f = _open_log("quota_web.log")
                GATE.quota_proc = subprocess.Popen(
                    [sys.executable, "-u", POOL_SCRIPT], cwd=HERE, env=env,
                    stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                    creationflags=0x00000200 if os.name == "nt" else 0)
            except Exception as e:
                return {"ok": False, "msg": f"{type(e).__name__}: {e}"}
        return {"ok": True, "msg": "scanning full pool quota (background, see logs/quota_web.log)"}

    def _providers(self) -> dict:
        try:
            nid = self._node_id()
            conns = uw.connections(nid)
            keys = uw.read_keys({c["id"] for c in conns})
            uw._KEYMAP.update(keys)
            rows = [{"id": c["id"], "name": uw.name_of(c),
                     "active": bool(c.get("isActive")),
                     "tail": (keys.get(c["id"]) or "")[-6:],
                     "code": c.get("errorCode"),
                     "err": (c.get("lastError") or "")[:140]} for c in conns]
            rows.sort(key=lambda r: (not r["active"], r["name"]))
            return {"rows": rows, "active": sum(1 for r in rows if r["active"]),
                    "total": len(rows), "provider": uw.PROVIDER}
        except Exception as e:
            return {"rows": [], "error": f"{type(e).__name__}: {e}"}

    def _keys(self) -> dict:
        tk = tokens_keys()
        acc = accounts_rows(5000)
        router = {}
        try:
            for c in uw.connections(self._node_id()):
                router[c["id"]] = c
            km = uw.read_keys(set(router))
            in_router = set(km.values())
        except Exception:
            in_router = set()
        rows = [{"key": k, "tail": k[-6:], "in_router": k in in_router}
                for k in tk]
        return {"rows": rows, "tokens": len(tk),
                "accounts": len(acc),
                "live": sum(1 for a in acc if a["status"] == "live"),
                "in_router": sum(1 for r in rows if r["in_router"])}

    def _add_missing(self, verify: bool = True, workers: int = 12) -> dict:
        try:
            nid = self._node_id()
            conns = uw.connections(nid)
            have = set(uw.read_keys({c["id"] for c in conns}).values())
            todo = [k for k in tokens_keys() if k not in have]
            if not todo:
                return {"ok": True, "added": 0, "todo": 0, "dead": 0,
                        "exhausted": 0, "unknown": 0, "errs": []}

            verdict: dict[str, str] = {}
            if verify:
                from concurrent.futures import ThreadPoolExecutor

                def probe(k: str):
                    grp, _ = uw.classify(uw.check_key(k))
                    return k, grp

                with ThreadPoolExecutor(max_workers=max(1, min(16, workers))) as ex:
                    for k, grp in ex.map(probe, todo):
                        verdict[k] = grp
            else:
                verdict = {k: "ok" for k in todo}

            added = dead = exh = unk = 0
            errs: list[str] = []
            for k in todo:
                grp = verdict.get(k, "unknown")
                if grp == "dead":
                    dead += 1
                    continue
                if grp == "exhausted":
                    exh += 1
                    continue
                if grp == "unknown":
                    unk += 1
                    continue
                try:
                    st, _ = router_call("/api/providers", "POST", {
                        "provider": nid, "apiKey": k,
                        "name": "add-" + k[-8:], "priority": 1,
                        "isActive": True, "testStatus": "unknown",
                        "providerSpecificData": {}})
                    if st in (200, 201):
                        added += 1
                except urllib.error.HTTPError as e:
                    if e.code == 409:
                        added += 1
                    else:
                        errs.append(f"{k[-6:]}: HTTP {e.code}")
                except Exception as e:
                    errs.append(f"{k[-6:]}: {type(e).__name__}")
            return {"ok": True, "added": added, "todo": len(todo), "dead": dead,
                    "exhausted": exh, "unknown": unk, "errs": errs[:10]}
        except Exception as e:
            return {"ok": False, "msg": f"{type(e).__name__}: {e}"}

    def _counts(self) -> dict:
        c = getattr(self.server, "_counts_cache", None)
        if c and time.time() - c[0] < 5:
            return c[1]
        acc = accounts_rows(5000)
        tk = tokens_keys()
        val = {"db": len(acc),
               "live": sum(1 for a in acc if a["status"] == "live"),
               "tokens": len(tk)}
        self.server._counts_cache = (time.time(), val)
        return val

    def _disable_bad(self) -> dict:
        todo = [r for r in USAGE.snapshot(0)["rows"]
                if r["grp"] in ("exhausted", "dead") and r["active"]]
        if not todo:
            return {"ok": True, "n": 0, "errs": []}
        from concurrent.futures import ThreadPoolExecutor

        def off(r):
            try:
                return r["id"], uw.set_active(r["id"], False)
            except Exception:
                return r["id"], False

        n = 0
        errs: list[str] = []
        done_ids: list[str] = []
        with ThreadPoolExecutor(max_workers=min(8, len(todo))) as ex:
            for cid, ok in ex.map(off, todo):
                if ok:
                    n += 1
                    done_ids.append(cid)
                else:
                    errs.append(cid[:8])
        if done_ids:
            now = time.time()
            with _STATE_LOCK:
                st = uw.load_state()
                for r in todo:
                    if r["id"] in done_ids:
                        st[r["id"]] = {"name": r["name"], "at": now,
                                       "why": r["grp"],
                                       "reset": r.get("reset") or ""}
                uw.save_state(st)
        if n:
            with USAGE.lock:
                for r in USAGE.rows:
                    if r["id"] in {t["id"] for t in todo}:
                        r["active"] = False
        return {"ok": True, "n": n, "total": len(todo), "errs": errs[:10]}


def vpn_poller(srv, every: float = 15.0):
    import vpn

    def loop():
        while True:
            try:
                lock = vpn.networklock()
                srv.vpn_cache = {"state": vpn.state(),
                                 "lock": None if lock == "" else (lock == "true"),
                                 "ip": vpn.current_ip()}
            except Exception:
                srv.vpn_cache = {"state": "?", "lock": None, "ip": None}
            time.sleep(every)
    threading.Thread(target=loop, daemon=True).start()


def prewarm_browser() -> None:
    code = ("import sys; sys.path.insert(0, r'%s'); import main; "
            "main.ensure_foxserve(True, 180)" % HERE)
    try:
        subprocess.run([sys.executable, "-u", "-c", code], cwd=HERE,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=240)
    except Exception:
        pass


GUARD_PORT = _int(os.environ.get("GUARD_PORT"), 20199)


def ensure_guard() -> str:
    if (os.environ.get("WEBUI_GUARD") or "1").strip().lower() in (
            "0", "false", "no", "off"):
        return "off"
    guard = os.path.join(HERE, "guard.py")
    if not os.path.exists(guard):
        return "guard.py missing"
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", GUARD_PORT))
        probe.close()
    except OSError:
        try:
            probe.close()
        except Exception:
            pass
        return "already running"
    env = dict(os.environ)
    env.setdefault("WEBUI_PORT", str(PORT))
    env.setdefault("GATE_PORT", str(GATE_PORT))
    try:
        subprocess.Popen([sys.executable, "-u", "guard.py"], cwd=HERE, env=env,
                         creationflags=0x00000008 if os.name == "nt" else 0,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL)
        return "started"
    except Exception as e:
        return f"could not start: {e}"


def main() -> int:
    if not os.path.exists(INDEX):
        print(f"[x] missing {INDEX}")
        return 2
    os.makedirs(LOG_DIR, exist_ok=True)
    if (os.environ.get("WEBUI_PREWARM") or "1").strip().lower() not in (
            "0", "false", "no", "off"):
        threading.Thread(target=prewarm_browser, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    srv.vpn_cache = {"state": "?"}
    vpn_poller(srv)
    if (os.environ.get("WEBUI_GATE") or "1").strip().lower() not in (
            "0", "false", "no", "off"):
        ok, msg = GATE.start()
        print(f"[i] throttle gateway: {msg}  ({GATE_URL})")
        GATE.watch()
    if str(GATE.vpn).strip().lower() not in ("0", "false", "no", "off", ""):
        ok, msg = GATE.rot_start()
        print(f"[i] IP rotator: {msg}")
    print(f"[i] supervisor (guard): {ensure_guard()}")
    print(f"[i] dashboard: http://127.0.0.1:{PORT}")
    print("[i] Ctrl+C to stop")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[i] stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
