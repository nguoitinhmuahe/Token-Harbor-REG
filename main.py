import os
import re
import sys
import time
import json
import socket
import random
import string
import shutil
import sqlite3
import subprocess
import urllib.error
import urllib.request
import asyncio
from datetime import datetime, timezone
from urllib.parse import urlparse

from fastfox import wait_for_verify_link, FAST_OPTS

_HUMAN = bool(FAST_OPTS.get("humanize"))
import v6proxy
import tempmail
import vpn
from chromedrv import open_chrome, release_shared
import reglog


PANEL = os.environ.get("REG_LAUNCHED_BY") or ""
LOG = reglog.get("engine", os.path.join(
    reglog.LOG_DIR, time.strftime("engine_%Y%m%d_%H%M%S.log")))
SELF_LOG = not PANEL
reglog.install_excepthook(LOG)


async def open_browser(headless: bool):
    return await open_chrome(headless=headless)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

if sys.stdout.isatty():
    os.system('cls' if os.name == 'nt' else 'clear')

WORKER_API = ""
DOMAIN = ""
MAIL_PROVIDER = (os.environ.get("MAIL_PROVIDER") or "tempmail").strip().lower()
if MAIL_PROVIDER not in tempmail.PROVIDERS:
    print(f"[MAIL] provider lạ {MAIL_PROVIDER!r} -> dùng custom")
    MAIL_PROVIDER = "custom"
PASSWORD = os.environ.get("REG_PASSWORD", "MatKhauManh123!")
ONE_PASSWORD = (os.environ.get("REG_ONE_PASSWORD") or "1").strip().lower() not in (
    "0", "false", "off", "no")
_PW_SYM = "!@#$%^&*-_=+"


def gen_password(n: int = 16) -> str:
    n = max(12, n)
    body = random.choices(string.ascii_letters + string.digits, k=n - 3)
    body += [random.choice(string.ascii_uppercase),
             random.choice(string.digits),
             random.choice(_PW_SYM)]
    random.shuffle(body)
    return "".join(body)


_GEO_CACHE = {"t": 0.0, "val": None}


async def _exit_locale_tz():
    now = time.monotonic()
    c = _GEO_CACHE
    if c["val"] is not None and now - c["t"] < 30:
        return c["val"]
    val = None
    try:
        ip = await asyncio.to_thread(vpn.current_ip)
    except Exception:
        ip = None
    if ip:
        try:
            req = urllib.request.Request("https://ipwho.is/" + ip)
            with urllib.request.urlopen(req, timeout=5) as r:
                g = json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            g = None
        if g and g.get("timezone", {}).get("id"):
            val = (g.get("country_code", "US"), g["timezone"]["id"])
    c.update({"t": now, "val": val})
    return val
TARGET_URL = "https://tokenharbor.ai/login?mode=signup"
KEYS_URL = "https://tokenharbor.ai/dashboard/api-keys"
BASE_HOST = "tokenharbor.ai"
NINE_URL = (os.environ.get("NINE_URL") or "http://127.0.0.1:20128").rstrip("/")
NINE_PROVIDER = (os.environ.get("REG_NINE_PROVIDER") or "a").strip()
NINE_ADD = True
NINE_NODE = {"id": None}
WEB_MODE = False
WEB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) "
          "Gecko/20100101 Firefox/133.0")

TOTAL_ACCOUNTS = 30
CONCURRENT_TASKS = 5
HEADLESS = True

NET_MODE = "direct"
VALID_NET = ("direct", "express", "nest", "v6")
ACCS_PER_IP = 4
ACC_TIMEOUT = 180
PROXY_PER_IP = ACCS_PER_IP
MAIL_TIMEOUT = 75
VPN_REGIONS = [
    "usa-new-jersey-2", "usa-oklahoma-city", "usa-little-rock",
    "usa-los-angeles-1", "usa-phoenix", "usa-new-york",
]
vpn_state = {"region": None}
VPN_IP = {"ip": None}
HOME_IP = {"ip": None}
VPN_GATE = asyncio.Lock()

HARD_BLOCK = ("too_many_signups", "ip_not_supported")
REGION_DEAD = HARD_BLOCK
IP_ROTATE = ("captcha_missing", "network_blocked", "signup_page_timeout",
             "cf_timeout", "cf_blocked", "ip_bad_skip")
NET_RETRY = 1
MAX_TRIES = 6

STOP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".reg_stop")
DB_PATH = "accounts.db"

HERE = os.path.dirname(os.path.abspath(__file__))
FOX_WS_FILE = os.path.join(HERE, ".camoufox_ws")
FOX_LOG = os.path.join(HERE, "logs", "foxserve.log")
FOX_UP = {"proc": None, "log": None}


def _ws_alive(url: str) -> bool:
    try:
        p = urlparse(url)
        with socket.create_connection(
                (p.hostname or "127.0.0.1", p.port or 80), timeout=1.0):
            return True
    except Exception:
        return False


def _fox_meta() -> dict | None:
    try:
        with open(FOX_WS_FILE, "r", encoding="utf-8") as f:
            raw = f.read().strip()
    except OSError:
        return None
    if not raw:
        return None
    if raw.startswith("{"):
        try:
            m = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return m if m.get("ws") else None
    return {"ws": raw, "headless": True}


def _fox_kill(pid) -> None:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=15)
        else:
            os.kill(pid, 9)
    except Exception:
        pass


def ensure_foxserve(headless: bool, timeout: float = 60.0) -> bool:
    env_ws = (os.environ.get("CAMOUFOX_WS") or "").strip()
    meta = {"ws": env_ws, "headless": headless} if env_ws else _fox_meta()
    if meta and bool(meta.get("headless", True)) == headless \
            and _ws_alive(meta["ws"]):
        print(f"[FOX] dung lai server {meta['ws']} (headless={headless})")
        return True
    if meta:
        print("[FOX] server cu chet/lech headless -> tat + start moi")
        _fox_kill(meta.get("pid"))
        try:
            os.remove(FOX_WS_FILE)
        except OSError:
            pass
    try:
        os.makedirs(os.path.dirname(FOX_LOG), exist_ok=True)
        log = open(FOX_LOG, "a", encoding="utf-8", errors="replace")
    except Exception:
        log = None
    env = dict(os.environ)
    env["CAMOUFOX_HEADLESS"] = "1" if headless else "0"
    env["PYTHONIOENCODING"] = "utf-8"
    creation = 0x00000200 if os.name == "nt" else 0
    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", "foxserve.py"], cwd=HERE, env=env,
            stdout=log or subprocess.DEVNULL,
            stderr=subprocess.STDOUT if log else None,
            stdin=subprocess.DEVNULL, creationflags=creation)
    except Exception as e:
        print(f"[FOX] khong start duoc server ({e}) -> moi acc launch rieng")
        if log:
            log.close()
        return False
    FOX_UP["proc"], FOX_UP["log"] = proc, log
    print(f"[FOX] dang start browser server (headless={headless}, log {FOX_LOG})")
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        meta = _fox_meta()
        if meta and _ws_alive(meta["ws"]):
            print(f"[FOX] server san sang {meta['ws']}")
            return True
        if proc.poll() is not None:
            print(f"[FOX] foxserve thoat (code {proc.returncode}) -> xem {FOX_LOG}")
            return False
        time.sleep(0.4)
    print(f"[FOX] cho {int(timeout)}s khong thay server -> moi acc launch rieng")
    return False
TOKEN_FILE = "tokens.txt"
ACCOUNT_FILE = "accounts.txt"

VERBOSE = os.environ.get("REG_VERBOSE", "0").strip().lower() in (
    "1", "true", "yes", "on")

_ORIG_PRINT = print
UI = None


def _enable_vt():
    if os.name != "nt":
        return True
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if k.GetConsoleMode(h, ctypes.byref(mode)):
            k.SetConsoleMode(h, mode.value | 0x0004)
        return True
    except Exception:
        return False


class _Progress:
    def __init__(self, total: int):
        self.total = total
        self.ok = 0
        self.fail = 0
        self.run = 0
        self.done = 0
        self.region = "-"
        self.t0 = time.monotonic()
        self.live = False
        self.reasons = {}

    def _bar(self, width: int = 18) -> str:
        frac = self.done / self.total if self.total else 0.0
        n = max(0, min(width, int(frac * width)))
        return "▰" * n + "▱" * (width - n)

    @staticmethod
    def mmss(sec: float) -> str:
        sec = int(max(0.0, sec))
        return f"{sec // 60:02d}:{sec % 60:02d}"

    def _rate(self, el: float, settle: float = 10.0) -> float:
        if el < settle or not self.done:
            return 0.0
        return self.done / el * 3600.0

    def line(self) -> str:
        el = time.monotonic() - self.t0
        rate = self._rate(el)
        left = self.total - self.done
        eta = left * 3600.0 / rate if rate > 0 else 0.0
        return (f"{self._bar()} {self.done}/{self.total} "
                f"ok {self.ok} fail {self.fail} run {self.run} | "
                f"{self.region} | {self.mmss(el)} eta {self.mmss(eta)} | "
                f"{rate:.1f}/h")

    def _write(self, s: str) -> None:
        try:
            sys.stdout.write(s)
        except UnicodeEncodeError:
            sys.stdout.write(s.encode("ascii", "replace").decode("ascii"))
        try:
            sys.stdout.flush()
        except Exception:
            pass

    def draw(self) -> None:
        if not self.live:
            return
        self._write("\r\x1b[K" + self.line())

    def event(self, msg: str) -> None:
        if not self.live:
            _ORIG_PRINT(msg)
            return
        self._write("\r\x1b[K" + msg + "\n")
        self.draw()

    def start(self) -> None:
        self.run += 1
        self.draw()

    def finish(self, ok: bool, status: str = "") -> None:
        self.run = max(0, self.run - 1)
        self.done += 1
        if ok:
            self.ok += 1
        else:
            self.fail += 1
            key = (status or "unknown").split(":")[0].strip()
            self.reasons[key] = self.reasons.get(key, 0) + 1
        self.draw()

    def requeue(self, status: str = "") -> None:
        self.done = max(0, self.done - 1)
        self.fail = max(0, self.fail - 1)
        key = (status or "unknown").split(":")[0].strip()
        if self.reasons.get(key):
            self.reasons[key] -= 1
            if not self.reasons[key]:
                del self.reasons[key]
        self.draw()

    def summary(self) -> None:
        self.live = False
        el = time.monotonic() - self.t0
        rate = self.done / el * 3600.0 if el > 0 and self.done else 0.0

        def emit(line: str) -> None:
            _ORIG_PRINT(line)
            if SELF_LOG:
                LOG.line(line)

        emit("")
        emit("=" * 58)
        emit(f"  XONG  ok {self.ok} | fail {self.fail} | "
             f"tổng {self.done}/{self.total}")
        emit(f"  thời gian {self.mmss(el)} | {rate:.1f} acc/h")
        if self.reasons:
            emit("  lý do fail:")
            for k, v in sorted(self.reasons.items(), key=lambda x: -x[1]):
                emit(f"    {v:>3}  {k}")
        emit("=" * 58)


_NOISE_SUB = (
    "[CF]", "[TIME]", "[FORM]", "[SIGNUP] POST", "resend #",
    "[MAIL] ", "fetching", "bấm '+ New key'", "nhập tên ok",
    "bấm 'Create key'", "chưa thấy key", "Enter (fallback)",
    "mở trang keys lỗi", "chat test...",
    "[CHECK] ", "[VERIFY] ", "browser mở", " = thk_",
    "[NET] ", "[SIGNUP] ", "[ENABLE] ", "tạo key qua UI",
)


def print(*args, **kwargs):
    msg = " ".join(str(a) for a in args)
    if SELF_LOG:
        LOG.line(msg)
    if not VERBOSE:
        if any(s in msg for s in _NOISE_SUB):
            return
        if msg.startswith("[REG]") and (msg.endswith("xong") or " <" in msg):
            return
    if UI is not None and UI.live:
        UI.event(msg)
    else:
        _ORIG_PRINT(*args, **kwargs)


SEL_EMAIL = 'input[name="email"]'
SEL_PASSWORD = 'input[name="password"]'
CREATE_BTN_TEXT = "Create account"
ENABLE_SEL = 'button[data-analytics="freemodel-consent-enable"]'
VERIFY_SEL = 'button:has-text("Verify email")'
FORM_ERROR_FLAGS = ("invalid email", "not allowed", "disposable",
                    "already registered", "already exists", "already in use")
AUTH_PATHS = ("/login", "/signup")
CF_BODY_MARKERS = ("just a moment", "checking your browser",
                   "verify you are human", "attention required",
                   "enable javascript and cookies", "cf-chl")
KEY_RE = re.compile(r"thk_[A-Za-z0-9_\-]{20,}")

file_lock = asyncio.Lock()
LAST_POST = {"body": "", "url": "", "status": 0}
DEBUG_DUMP = os.environ.get("REG_DEBUG", "1").strip().lower() not in (
    "0", "false", "off", "no")


async def _dump_debug(page, rand_name, status):
    try:
        html = await page.content()
    except Exception:
        return
    try:
        os.makedirs("debug", exist_ok=True)
        fn = os.path.join("debug", f"{rand_name}_{int(time.time())}.html")
        head = (f"<!-- status={status} url={page.url} "
                f"post={LAST_POST['status']} -->\n"
                f"<!-- body={ (LAST_POST['body'] or '')[:3000] } -->\n")
        with open(fn, "w", encoding="utf-8") as f:
            f.write(head + html)
        def _mt(fn):
            try:
                return os.path.getmtime(os.path.join("debug", fn))
            except OSError:
                return 0.0
        files = sorted(os.listdir("debug"), key=_mt)
        for old in files[:-40]:
            try:
                os.remove(os.path.join("debug", old))
            except Exception:
                pass
        print(f"[DBG] {rand_name} dump {fn} ({len(html)//1024}KB)")
    except Exception:
        pass


def init_db():
    last = None
    for _ in range(10):
        conn = None
        try:
            conn = sqlite3.connect(DB_PATH, timeout=15)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS accounts ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE, password TEXT, "
                "api_key TEXT, status TEXT, created_at TEXT)"
            )
            conn.commit()
            return conn
        except sqlite3.OperationalError as e:
            last = e
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            time.sleep(1.0)
    raise last


def reset_outputs() -> bool:
    if os.environ.get("REG_APPEND"):
        return True
    for p in (TOKEN_FILE, ACCOUNT_FILE):
        for _ in range(5):
            try:
                if os.path.exists(p):
                    try:
                        shutil.copyfile(p, p + ".bak")
                    except Exception:
                        pass
                    os.remove(p)
                break
            except OSError:
                time.sleep(0.5)
        else:
            return False
    return True


def gen_random_string(length=8):
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))


async def export_results(email, password, api_key):
    if not api_key:
        return
    async with file_lock:
        with open(TOKEN_FILE, "a", encoding="utf-8") as f:
            f.write(f"{api_key}\n")
        with open(ACCOUNT_FILE, "a", encoding="utf-8") as f:
            f.write(f"{email}:{password}:{api_key}\n")


CREATE_JS = '''(text) => {
    const btns = document.querySelectorAll('button[type="submit"]');
    for (const btn of btns) {
        if (btn.innerText.includes(text)) { btn.click(); return true; }
    }
    return false;
}'''


async def _click(page, selector, timeout):
    try:
        el = await page.wait_for_selector(selector, timeout=timeout)
    except Exception:
        return False, "notfound"
    if not el:
        return False, "notfound"
    try:
        await el.click(force=True)
        return True, "ok"
    except Exception as e:
        return False, f"clickfail: {e}"


CF_IFRAME = 'iframe[src*="challenges.cloudflare.com"]'


async def _cf_state(page) -> str:
    try:
        return await page.evaluate("""() => {
            const el = document.querySelector('input[name="cf-turnstile-response"]');
            if (el && el.value && el.value.length > 20) return 'solved';
            if (el) return 'pending';
            const f = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
            return f ? 'pending' : 'absent';
        }""")
    except Exception:
        return "unknown"


async def _click_turnstile(page) -> bool:
    try:
        fl = page.frame_locator(CF_IFRAME)
        for sel in ('input[type="checkbox"]', 'label', '#challenge-stage',
                    'div.cb-c', 'span.ctp-checkbox-label'):
            loc = fl.locator(sel)
            try:
                if await loc.count():
                    await loc.first.click(timeout=2000, force=True)
                    return True
            except Exception:
                pass
    except Exception:
        pass

    try:
        el = await page.query_selector(CF_IFRAME)
        if el:
            box = await el.bounding_box()
            if box:
                x = box["x"] + 28
                y = box["y"] + box["height"] / 2
                await page.mouse.click(x, y)
                return True
    except Exception:
        pass
    return False


async def _wait_cf_clear(page, rand_name, timeout: float = 60.0) -> str:
    end = time.monotonic() + timeout
    last_click = 0.0
    reloaded = False
    while time.monotonic() < end:
        _check_stop()
        st = await _cf_state(page)
        if st in ("absent", "solved"):
            return st
        now = time.monotonic()
        if now - last_click > 10:
            last_click = now
            if await _click_turnstile(page):
                print(f"[CF] {rand_name} click Turnstile")
                await page.wait_for_timeout(600)
        if not reloaded and now > end - timeout + 20:
            reloaded = True
            print(f"[CF] {rand_name} kẹt 20s -> reload")
            try:
                await page.reload(wait_until="domcontentloaded", timeout=20000)
                await page.wait_for_timeout(1200)
            except Exception:
                pass
        await page.wait_for_timeout(400)
    return "timeout"


async def _human_type(loc, text: str, lo: int, hi: int) -> None:
    try:
        await loc.click(timeout=4000)
    except Exception:
        try:
            await loc.focus(timeout=2000)
        except Exception:
            pass
    try:
        await loc.press("Control+a")
        await loc.press("Delete")
    except Exception:
        pass
    try:
        await loc.press_sequentially(text, delay=random.randint(lo, hi))
        return
    except Exception:
        pass
    for ch in text:
        try:
            await loc.press(ch)
        except Exception:
            pass
        await asyncio.sleep(random.uniform(lo, hi) / 1000.0)


async def _fill_form(page, email, password=None) -> bool:
    password = password or PASSWORD
    lo, hi = (12, 34) if _HUMAN else (55, 150)
    for attempt in range(1, 4):
        try:
            await page.wait_for_selector(SEL_EMAIL, timeout=15000)
            await page.wait_for_selector(SEL_PASSWORD, timeout=15000)
            try:
                banner = page.get_by_role("button", name=re.compile(
                    "essential only|accept analytics|accept", re.I))
                if await banner.count():
                    try:
                        await banner.first.click(timeout=1500)
                    except Exception:
                        pass
            except Exception:
                pass

            pw = page.locator(SEL_PASSWORD)
            em = page.locator(SEL_EMAIL)
            await _human_type(pw, password, lo, hi)
            await _human_type(em, email, lo, hi)
            ev = await em.input_value()
            pv = await pw.input_value()
            if ev != email or pv != password:
                print(f"[FORM] gõ lệch (lần {attempt}) -> thử lại")
                await page.wait_for_timeout(random.randint(300, 700))
                continue

            await page.wait_for_timeout(random.randint(350, 1100))

            boxes = page.locator('input[type="checkbox"]')
            n = await boxes.count()
            ticked = ""
            for i in range(n):
                el = boxes.nth(i)
                try:
                    if await el.is_checked():
                        ticked = f"#{i} đã tick"
                        break
                    await el.check(timeout=3000, force=True)
                    ticked = f"#{i} vừa tick"
                    break
                except Exception as e:
                    print(f"[FORM] tick #{i} fail: {type(e).__name__}: {str(e)[:80]}")
            print(f"[FORM] consent: {ticked or 'KHÔNG tick được'}")
            return True
        except Exception as e:
            print(f"[FORM] fill fail (lần {attempt}): "
                  f"{type(e).__name__}: {str(e)[:100]}")
            await page.wait_for_timeout(400)
    return False


async def _click_create(page) -> bool:
    try:
        loc = page.locator('button[type="submit"]', has_text=CREATE_BTN_TEXT)
        if await loc.count():
            await loc.first.click(timeout=5000)
            return True
    except Exception as e:
        print(f"[SIGNUP] click create lỗi: {type(e).__name__}: {str(e)[:60]}")
    try:
        return bool(await page.evaluate(CREATE_JS, CREATE_BTN_TEXT))
    except Exception as e:
        print(f"[SIGNUP] js create lỗi: {type(e).__name__}")
        return False


async def _is_cf_interstitial(page) -> bool:
    try:
        has_iframe = await page.query_selector(CF_IFRAME)
        if has_iframe:
            return True
        t = (await page.evaluate(
            "() => (document.body ? document.body.innerText : '').slice(0,400)")).lower()
    except Exception:
        return False
    return any(k in t for k in CF_BODY_MARKERS)


async def _clear_browsing(page) -> None:
    try:
        await page.context.clear_cookies()
    except Exception:
        pass
    try:
        await page.evaluate("() => { try { localStorage.clear(); } catch(e) {}"
                            " try { sessionStorage.clear(); } catch(e) {} }")
    except Exception:
        pass


async def _load_signup(page, rand_name) -> str:
    for attempt in range(1, 4):
        t_goto = time.monotonic()
        try:
            await page.goto(TARGET_URL, wait_until="domcontentloaded",
                            timeout=30000)
        except Exception as e:
            print(f"[SIGNUP] {rand_name} goto try{attempt}: {str(e)[:90]}")
            await page.wait_for_timeout(400)
            continue

        end = time.monotonic() + 20
        clicks = 0
        last_click = 0.0
        while time.monotonic() < end:
            if await page.query_selector(SEL_EMAIL):
                print(f"[CF] {rand_name} form hiện sau "
                      f"{int(time.monotonic()-t_goto)}s")
                return "ok"

            info = await _ts_probe(page)
            if info.get("err"):
                print(f"[CF] {rand_name} Turnstile báo lỗi -> IP này hỏng")
                return "captcha_missing"

            now = time.monotonic()
            if now - last_click > 2:
                last_click = now
                rect = None
                try:
                    rect = await page.evaluate(TS_RECT_JS)
                except Exception:
                    pass
                if rect:
                    x = rect["x"] + (28 if clicks % 2 == 0 else rect["w"] / 2)
                    try:
                        await page.mouse.click(x, rect["y"] + rect["h"] / 2)
                        clicks += 1
                    except Exception:
                        pass
                elif await _click_turnstile(page):
                    clicks += 1
                if clicks and clicks <= 3:
                    print(f"[CF] {rand_name} click Turnstile #{clicks}")
            await page.wait_for_timeout(250)

        try:
            info = await page.evaluate("""() => ({
                url: location.href,
                title: document.title,
                bodyLen: document.body ? document.body.innerText.length : -1,
                head: (document.body ? document.body.innerText : '').slice(0,150),
                cf: !!document.querySelector('iframe[src*="challenges.cloudflare.com"]')
            })""")
            print(f"[CF] {rand_name} try{attempt} thất bại: {info}")
        except Exception as e:
            print(f"[CF] {rand_name} try{attempt}: eval lỗi "
                  f"{type(e).__name__}")
        await _clear_browsing(page)
        await page.wait_for_timeout(400)
    return "signup_page_timeout"


def _on_auth_page(url: str) -> bool:
    try:
        p = urlparse(url)
    except Exception:
        return True
    if p.scheme not in ("http", "https"):
        return True
    path = p.path.rstrip("/").lower()
    return path in AUTH_PATHS or path.endswith("/signup")


async def _ts_probe(page) -> dict:
    try:
        return await page.evaluate("""() => {
            const el = document.querySelector('input[name="cf-turnstile-response"]');
            const tok = el && el.value ? el.value.length : 0;
            const f = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
            const w = !!document.querySelector('.cf-turnstile') || !!f || !!el;
            const body = (document.body && document.body.innerText || '').toLowerCase();
            const err = body.includes('too many attempts')
                     || body.includes('site is busy')
                     || body.includes('human check failed');
            return {tok, widget: w, resp: !!el, err};
        }""")
    except Exception:
        return {}


TS_RECT_JS = """() => {
  const i = document.querySelector('input[name="cf-turnstile-response"]');
  if (!i) return null;
  let el = i;
  for (let k = 0; k < 6 && el; k++) {
    const r = el.getBoundingClientRect();
    if (r.width > 150 && r.height > 30)
      return {x: r.x, y: r.y, w: r.width, h: r.height};
    el = el.parentElement;
  }
  return null;
}"""


async def _solve_turnstile(page, rand_name, timeout: float = 30.0) -> bool:
    end = time.monotonic() + timeout
    started = time.monotonic()
    clicks = 0
    while time.monotonic() < end:
        info = await _ts_probe(page)
        if info.get("tok", 0) > 20:
            return True
        if info.get("err"):
            return False
        if info.get("resp") and time.monotonic() - started > 12 and clicks < 3:
            rect = None
            try:
                rect = await page.evaluate(TS_RECT_JS)
            except Exception:
                pass
            if rect:
                x = rect["x"] + (28 if clicks % 2 == 0 else rect["w"] / 2)
                try:
                    await page.mouse.click(x, rect["y"] + rect["h"] / 2)
                    clicks += 1
                    print(f"[CF] {rand_name} click Turnstile widget #{clicks}")
                except Exception:
                    pass
        await page.wait_for_timeout(1500)
    return (await _ts_probe(page)).get("tok", 0) > 20


CF_CSP_DROP = ("content-security-policy", "content-security-policy-report-only")


async def _strip_cf_csp(context) -> None:
    async def _route(route):
        req = route.request
        if req.resource_type == "document":
            try:
                resp = await route.fetch()
                headers = {k: v for k, v in resp.headers.items()
                           if k.lower() not in CF_CSP_DROP}
                await route.fulfill(response=resp, headers=headers)
                return
            except Exception:
                pass
        try:
            await route.continue_()
        except Exception:
            pass

    for pat in ("https://challenges.cloudflare.com/**",
                "http://challenges.cloudflare.com/**"):
        await context.route(pat, _route)


async def _ts_wait(page, timeout: float = 12.0) -> str:
    end = time.monotonic() + timeout
    seen = False
    while time.monotonic() < end:
        info = await _ts_probe(page)
        if info.get("err"):
            return "missing"
        if info.get("tok", 0) > 20:
            return "ok"
        if info.get("widget"):
            seen = True
        await page.wait_for_timeout(400)
    return "missing" if seen else "none"


async def _signup(page, rand_name, email, password=None,
                  timeout: float = 60.0) -> str:
    MAX_ATTEMPTS = 3
    end = time.monotonic() + timeout
    last_post = {"body": "", "status": 0}
    reloads = 0
    cf_retry = 0

    async def _on_resp(resp):
        try:
            if resp.request.method == "POST" and BASE_HOST in resp.url:
                last_post["body"] = (await resp.text())[:4000]
                last_post["status"] = resp.status
                LAST_POST["body"] = last_post["body"]
                LAST_POST["url"] = resp.url
                LAST_POST["status"] = resp.status
                print(f"[SIGNUP] POST {resp.url.split('?')[0]} -> "
                      f"{resp.status} {last_post['body'][:160]!r}")
        except Exception:
            pass

    page.on("response", _on_resp)

    cf = await _wait_cf_clear(page, rand_name, 25)
    print(f"[CF] {rand_name} trước fill: {cf}")
    if cf == "timeout":
        return "cf_timeout"

    for attempt in range(1, MAX_ATTEMPTS + 1):
        if time.monotonic() > end:
            return "signup_failed"
        last_post["body"] = ""

        if await _cf_state(page) in ("pending", "unknown"):
            if await _wait_cf_clear(page, rand_name, 20) == "timeout":
                return "cf_timeout"

        if not await _fill_form(page, email, password):
            return "signup_failed"
        info = await _ts_probe(page)
        if info.get("err"):
            return "captcha_missing"
        if info.get("tok", 0) <= 20 and info.get("widget"):
            await _solve_turnstile(page, rand_name, 20)
        if not await _click_create(page):
            print(f"[SIGNUP] {rand_name} không click được Create, thử lại")
            continue
        print(f"[SIGNUP] {rand_name} submit #{attempt}")

        post_err = ""
        for _ in range(16):
            await page.wait_for_timeout(500)
            if not _on_auth_page(page.url):
                return "ok"
            low = last_post["body"].lower()
            if low:
                if (last_post.get("status") == 429 or "just a moment" in low
                        or "cf-chl" in low or "checking your browser" in low):
                    cf_retry += 1
                    if cf_retry > 1:
                        return "cf_blocked"
                    print(f"[CF] {rand_name} 429 -> thử qua challenge, giữ IP")
                    try:
                        await page.reload(wait_until="domcontentloaded",
                                          timeout=20000)
                    except Exception:
                        pass
                    try:
                        await page.wait_for_timeout(800)
                    except Exception:
                        pass
                    if await _wait_cf_clear(page, rand_name, 30) == "timeout":
                        return "cf_blocked"
                    continue
                if "needcaptcha" in low or "bot check failed" in low:
                    post_err = "captcha_failed"
                    break
                if ("another account from this network" in low
                        or "network or device" in low):
                    return "network_blocked"
                if "too many sign-ups from this network" in low:
                    return "too_many_signups"
                if "not supported for token harbor accounts" in low:
                    return "ip_not_supported"
                if '"error"' in low:
                    post_err = f"server_error: {last_post['body'][:140]}"
                    break
            txt = ""
            try:
                txt = (await page.evaluate(
                    "() => document.body ? document.body.innerText : ''")).lower()
            except Exception:
                txt = ""
            for flag in FORM_ERROR_FLAGS:
                if flag in txt:
                    return f"form_error: {flag}"
            if await _cf_state(page) in ("pending", "unknown"):
                break

        if post_err == "captcha_failed":
            info = await _ts_probe(page)
            print(f"[CF] {rand_name} needCaptcha -> {info}")
            if info.get("err"):
                return "captcha_missing"
            if await _solve_turnstile(page, rand_name, 45):
                print(f"[CF] {rand_name} có token -> submit lại")
                continue
            if reloads >= 1:
                return "captcha_missing"
            reloads += 1
            print(f"[SIGNUP] {rand_name} reload lấy token mới (#{reloads})")
            try:
                await page.reload(wait_until="domcontentloaded", timeout=20000)
                await page.wait_for_timeout(1200)
            except Exception:
                pass
            await _wait_cf_clear(page, rand_name, 20)
            continue
        if post_err:
            return post_err
        print(f"[SIGNUP] {rand_name} submit #{attempt} chưa xong | "
              f"txt={txt[:120]!r}")

    return "signup_failed"


async def _chat_check(page, api_key, timeout_s: float = 45.0):
    js = """async ([key, ms]) => {
      const ctl = new AbortController();
      const t = setTimeout(() => ctl.abort(), ms);
      try {
        const r = await fetch('https://tokenharbor.ai/v1/chat/completions', {
          method: 'POST', signal: ctl.signal, credentials: 'include',
          headers: {'content-type': 'application/json',
                    'authorization': 'Bearer ' + key},
          body: JSON.stringify({model: 'deepseek-v4-flash:free',
            messages: [{role: 'user', content: 'hi'}], max_tokens: 1, stream: false})
        });
        const txt = await r.text();
        clearTimeout(t);
        return {status: r.status, body: txt.slice(0, 400)};
      } catch (e) {
        clearTimeout(t);
        return {status: 0, body: String(e)};
      }
    }"""
    last = "unknown"
    for attempt in range(1, 3):
        try:
            r = await asyncio.wait_for(
                page.evaluate(js, [api_key, int(timeout_s * 1000)]),
                timeout=timeout_s + 20)
        except Exception as e:
            last = f"eval_error {type(e).__name__}"
            await asyncio.sleep(2)
            continue

        st = r.get("status")
        body = (r.get("body") or "")
        low = body.lower()
        if st == 200 and "choices" in low:
            return True, "chat_ok"
        if st == 401:
            return False, "401 unauthorized"
        if st == 403:
            return False, f"403 forbidden {low[:80]}"
        if st == 429 or "rate limit" in low or "rate_limit" in low:
            last = f"429 {low[:80]}"
            await asyncio.sleep(4)
            continue
        if st == 0 or st == 200 or st >= 500:
            last = f"http_{st} {low[:60]}"
            await asyncio.sleep(3)
            continue
        return None, f"http_{st} {low[:80]}"
    return None, last


def _nine_call(path: str, method: str = "GET", body: dict | None = None,
               timeout: float = 15.0):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(NINE_URL + path, data=data, method=method,
                                 headers=headers)
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
        LOG.http(">>", f"router {method} {path}", r.status, raw,
                 extra=f"{(time.monotonic() - t0) * 1000:.0f}ms")
        return r.status, (json.loads(raw) if raw.strip().startswith(("{", "[")) else {})
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:
            raw = ""
        LOG.http(">>", f"router {method} {path}", e.code, raw,
                 extra=f"{(time.monotonic() - t0) * 1000:.0f}ms")
        raise


def _nine_add_key(name: str, key: str) -> tuple:
    try:
        if NINE_NODE["id"] is None:
            _, d = _nine_call("/api/provider-nodes")
            nodes = d.get("nodes") if isinstance(d, dict) else d
            want = NINE_PROVIDER.lower()
            hit = [n for n in (nodes or [])
                   if (n.get("name") or "").strip().lower() == want]
            if not hit:
                LOG.warn("[NINE] %s: no provider %r (have: %s)", name, NINE_PROVIDER,
                         ", ".join(sorted((n.get("name") or "?") for n in (nodes or []))[:20]))
                return False, f"không thấy provider {NINE_PROVIDER!r}"
            NINE_NODE["id"] = hit[0]["id"]
        _nine_call("/api/providers", "POST", {
            "provider": NINE_NODE["id"], "apiKey": key, "name": name,
            "priority": 1, "isActive": True, "testStatus": "unknown",
            "providerSpecificData": {}})
        return True, "ok"
    except urllib.error.HTTPError as e:
        if e.code == 409:
            return True, "đã có sẵn"
        try:
            body = e.read().decode("utf-8", "replace")[:120]
        except Exception:
            body = ""
        LOG.warn("[NINE] %s add key failed: HTTP %s %s", name, e.code, body)
        return False, f"HTTP {e.code} {body}"
    except Exception as e:
        LOG.exc("[NINE] %s add key raised", name)
        return False, type(e).__name__


async def _web_session(context):
    try:
        import requests
    except Exception as e:
        print(f"[WEB] thiếu requests ({type(e).__name__}) -> dùng UI")
        return None
    s = requests.Session()
    try:
        for c in await context.cookies():
            try:
                s.cookies.set(c["name"], c["value"],
                              domain=c.get("domain") or BASE_HOST,
                              path=c.get("path") or "/")
            except Exception:
                pass
    except Exception:
        return None
    s.headers.update({
        "user-agent": WEB_UA,
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-US,en;q=0.9",
        "origin": "https://" + BASE_HOST,
        "referer": "https://" + BASE_HOST + "/dashboard/api-keys",
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
    })
    return s


def _web_verify(sess, link: str) -> tuple:
    r = sess.get(link, timeout=25, allow_redirects=True)
    return r.url, (r.text or "")[:6000].lower()


def _web_create_key(sess, name: str) -> dict:
    r = sess.post("https://" + BASE_HOST + "/api/keys",
                  json={"label": name, "name": name}, timeout=25)
    body = (r.text or "")[:400]
    if r.status_code in (200, 201):
        try:
            j = r.json()
        except Exception:
            return {"error": f"key_bad_json: {body[:100]}"}
        if isinstance(j, dict):
            if j.get("plaintext"):
                return j
            if isinstance(j.get("key"), dict) and j["key"].get("plaintext"):
                return j["key"]
        return {"error": f"key_no_plaintext: {body[:120]}"}
    return {"error": f"key_http_{r.status_code}: {body[:120]}"}


def _web_chat(sess, key: str, timeout_s: float = 45.0):
    last = "unknown"
    for _ in range(2):
        try:
            r = sess.post("https://" + BASE_HOST + "/v1/chat/completions",
                          headers={"authorization": "Bearer " + key,
                                   "content-type": "application/json"},
                          json={"model": "deepseek-v4-flash:free",
                                "messages": [{"role": "user", "content": "hi"}],
                                "max_tokens": 1, "stream": False},
                          timeout=timeout_s)
        except Exception as e:
            last = f"net_{type(e).__name__}"
            time.sleep(2)
            continue
        st, body = r.status_code, (r.text or "")[:400]
        low = body.lower()
        if st == 200 and "choices" in low:
            return True, "chat_ok"
        if st == 401:
            return False, "401 unauthorized"
        if st == 403:
            return False, f"403 forbidden {low[:80]}"
        if st == 429 or "rate limit" in low or "rate_limit" in low:
            last = f"429 {low[:80]}"
            time.sleep(3)
            continue
        if st == 0 or st >= 500:
            last = f"http_{st} {low[:60]}"
            time.sleep(2)
            continue
        return None, f"http_{st} {low[:80]}"
    return None, last


async def _click_el(el):
    try:
        await el.click(timeout=5000)
    except Exception:
        try:
            await el.click(timeout=5000, force=True)
        except Exception:
            pass


async def _create_key(page, rand_name):
    captured = {}

    async def on_resp(resp):
        try:
            if resp.request.method != "POST":
                return
            if BASE_HOST not in resp.url:
                return
            txt = await resp.text()
            if "plaintext" in txt:
                captured["data"] = json.loads(txt)
        except Exception:
            pass

    page.on("response", on_resp)
    try:
        for attempt in (1, 2, 3):
            t0 = time.monotonic()
            try:
                await asyncio.wait_for(
                    page.goto(KEYS_URL, wait_until="domcontentloaded", timeout=20000),
                    timeout=25)
            except Exception as e:
                print(f"[KEY] {rand_name} mở trang keys lỗi: {type(e).__name__}")
                continue
            await page.wait_for_timeout(1200)

            new_btn = page.locator("button", has_text="New key")
            for _ in range(10):
                if await new_btn.count():
                    break
                await page.wait_for_timeout(1000)
            if not await new_btn.count():
                print(f"[KEY] {rand_name} KHÔNG thấy nút 'New key'")
                try:
                    btns = await page.evaluate(
                        "() => [...document.querySelectorAll('button')]"
                        ".map(b => b.innerText.trim()).slice(0,20)")
                    print(f"[KEY] buttons: {btns}")
                except Exception:
                    pass
                await page.wait_for_timeout(1000)
                continue
            await _click_el(new_btn.first)
            print(f"[KEY] {rand_name} bấm '+ New key'")

            inp = page.locator('input[type="text"]')
            field = None
            for _ in range(20):
                await page.wait_for_timeout(120)
                n = await inp.count()
                for i in range(n):
                    cand = inp.nth(i)
                    try:
                        if await cand.is_visible():
                            field = cand
                            break
                    except Exception:
                        continue
                if field is not None:
                    break
            if field is None:
                print(f"[KEY] {rand_name} KHÔNG thấy input nhập tên")
                try:
                    info = await page.evaluate(
                        "() => ({inputs: [...document.querySelectorAll('input')]"
                        ".map(i => i.placeholder||i.type), "
                        "btns: [...document.querySelectorAll('button')]"
                        ".map(b => b.innerText.trim()).slice(0,20)})")
                    print(f"[KEY] dump: {info}")
                except Exception:
                    pass
                await page.wait_for_timeout(600)
                continue
            try:
                await field.click(timeout=3000)
            except Exception:
                pass
            try:
                await field.press_sequentially(rand_name, delay=15)
            except Exception:
                await field.fill("")
                try:
                    await field.click(timeout=3000)
                except Exception:
                    pass
                await page.keyboard.type(rand_name, delay=15)
            try:
                if (await field.input_value()) != rand_name:
                    await field.fill(rand_name)
            except Exception:
                pass
            print(f"[KEY] {rand_name} nhập tên ok")

            scope = page
            try:
                anc = field.locator(
                    "xpath=ancestor::*[@role='dialog' or self::dialog][1]")
                if await anc.count():
                    scope = anc.first
            except Exception:
                pass
            cb = scope.locator("button", has_text="Create key")
            for _ in range(15):
                if await cb.count():
                    break
                await page.wait_for_timeout(120)
            if not await cb.count() and scope is not page:
                cb = page.locator("button", has_text="Create key")
            if not await cb.count():
                cb = scope.locator("button", has_text=re.compile(r"^\s*Create\s*$"))
            if await cb.count():
                target = cb.last
                for i in range(await cb.count()):
                    c = cb.nth(i)
                    try:
                        if await c.is_visible():
                            target = c
                            break
                    except Exception:
                        continue
                await _click_el(target)
                print(f"[KEY] {rand_name} bấm 'Create key'")
            else:
                print(f"[KEY] {rand_name} KHÔNG thấy nút 'Create key'")
                try:
                    info = await page.evaluate(
                        "() => ({val: (document.querySelector('input[type=text]')||{}).value, "
                        "btns: [...document.querySelectorAll('button')]"
                        ".filter(b => b.offsetParent).map(b => b.innerText.trim())})")
                    print(f"[KEY] dump: {info}")
                except Exception:
                    pass
                try:
                    await field.press("Enter")
                    print(f"[KEY] {rand_name} Enter (fallback)")
                except Exception:
                    pass

            pressed = False
            for n in range(30):
                await page.wait_for_timeout(150)
                if "data" in captured:
                    break
                if not pressed and n == 8:
                    pressed = True
                    try:
                        await field.press("Enter")
                    except Exception:
                        pass
            if "data" in captured:
                print(f"[KEY] {rand_name} tạo key qua UI "
                      f"{int((time.monotonic()-t0)*1000)}ms")
                return captured["data"]

            try:
                txt = await page.evaluate(
                    "() => document.body ? document.body.innerText : ''")
                m = KEY_RE.search(txt or "")
                if m:
                    print(f"[KEY] {rand_name} lấy key từ text trang")
                    return {"plaintext": m.group(0)}
            except Exception:
                pass
            print(f"[KEY] {rand_name} chưa thấy key, thử lại")
    finally:
        try:
            page.remove_listener("response", on_resp)
        except Exception:
            pass
    return {"error": "key_ui_failed"}


async def register_one(conn, semaphore, pool, ipbad=None):
    async with semaphore:
        rand_name = gen_random_string(10)
        password = PASSWORD if not ONE_PASSWORD else gen_password()

        mail_token = None
        if MAIL_PROVIDER == "custom":
            email = f"{rand_name}@{DOMAIN}"
        else:
            try:
                box = await asyncio.to_thread(
                    tempmail.new, MAIL_PROVIDER, rand_name, password)
                email, mail_token = box.email, box
                if MAIL_PROVIDER == "random":
                    print(f"[MAIL] {rand_name} random -> {box.provider}")
            except Exception as e:
                print(f"[MAIL] {rand_name} {MAIL_PROVIDER} fail "
                      f"({type(e).__name__}: {e}) -> fallback {WORKER_API} @{DOMAIN}")
                email = f"{rand_name}@{DOMAIN}"

        print(f"[REG] {rand_name} <{email}>")
        if UI is not None:
            UI.start()

        api_key = None
        verified = False
        reason = None
        browser = None
        context = None
        page = None
        st = {"browser": None, "context": None, "last": time.monotonic()}

        def _tick():
            st["last"] = time.monotonic()

        async def _watchdog():
            while True:
                await asyncio.sleep(5)
                idle = time.monotonic() - st["last"]
                if idle > ACC_TIMEOUT:
                    print(f"[WATCHDOG] {rand_name} idle {int(idle)}s > "
                          f"{ACC_TIMEOUT}s -> đóng browser")
                    for key in ("context", "browser"):
                        obj = st.get(key)
                        if obj is not None:
                            try:
                                await obj.close()
                            except Exception:
                                pass
                    st["closed"] = True
                    return

        wd = asyncio.create_task(_watchdog())

        try:
            _check_stop()
            proxy = None
            net_locale = net_tz = None
            if NET_MODE == "nest":
                NET_MODE = "v6"
            if NET_MODE == "v6":
                proxy = v6proxy.pick()
                if proxy:
                    proxy = {"server": proxy["server"]}
                _g = await _exit_locale_tz()
                if _g:
                    net_locale, net_tz = _g
                print(f"[NET] {rand_name} v6 {proxy['server'] if proxy else 'direct'} "
                      f"| {net_locale} {net_tz}")
            elif NET_MODE == "express":
                print(f"[NET] {rand_name} express {vpn_state['region']}")
            else:
                print(f"[NET] {rand_name} direct")

            if reason is None:
                browser = await asyncio.wait_for(open_browser(HEADLESS), timeout=ACC_TIMEOUT)
                st["browser"] = browser
                if proxy:
                    context = await browser.new_context(
                        proxy=proxy, locale=net_locale, timezone_id=net_tz)
                else:
                    _g = await _exit_locale_tz()
                    if _g:
                        context = await browser.new_context(
                            locale=_g[0], timezone_id=_g[1])
                        print(f"[NET] {rand_name} tz={_g[1]} locale={_g[0]}")
                    else:
                        context = await browser.new_context()
                st["context"] = context
                await _strip_cf_csp(context)
                page = await context.new_page()
                _tick()
                print(f"[REG] {rand_name} browser mở")

            if ipbad is not None and ipbad.is_set():
                reason = "ip_bad_skip"

            if reason is None:
                t0 = time.monotonic()
                try:
                    loaded = await asyncio.wait_for(
                        _load_signup(page, rand_name), timeout=90)
                except asyncio.TimeoutError:
                    print(f"[SIGNUP] {rand_name} load timeout 90s")
                    loaded = "signup_page_timeout"
                st["t_load"] = time.monotonic() - t0
                _tick()
                print(f"[TIME] {rand_name} load={st['t_load']:.0f}s")
                if loaded != "ok":
                    reason = loaded
                elif ipbad is not None and ipbad.is_set():
                    reason = "ip_bad_skip"
                else:
                    t0 = time.monotonic()
                    ws = await asyncio.wait_for(
                        _signup(page, rand_name, email, password, timeout=120),
                        timeout=140)
                    st["t_signup"] = time.monotonic() - t0
                    _tick()
                    print(f"[SIGNUP] {rand_name} -> {ws} "
                          f"({st['t_signup']:.0f}s)")
                    if ws != "ok":
                        reason = ws

            wsess = None
            if reason is None and WEB_MODE:
                wsess = await _web_session(context)
                print(f"[WEB] {rand_name} http session "
                      f"{'ok' if wsess is not None else 'fail -> UI'}")
            if reason is None:
                eok, ewhy = await _click(page, ENABLE_SEL, 4000)
                if not eok:
                    print(f"[ENABLE] {rand_name} {ewhy}, reload+retry")
                    try:
                        await page.reload(wait_until="domcontentloaded", timeout=15000)
                        await page.wait_for_timeout(400)
                    except Exception:
                        pass
                    eok, ewhy = await _click(page, ENABLE_SEL, 4000)
                print(f"[ENABLE] {rand_name} {'ok' if eok else ewhy}")
                await page.wait_for_timeout(150)

                vok, vwhy = await _click(page, VERIFY_SEL, 2500)
                if not vok:
                    print(f"[VERIFY] {rand_name} {vwhy}, reload")
                    try:
                        await page.reload(wait_until="domcontentloaded", timeout=15000)
                        await page.wait_for_timeout(200)
                    except Exception:
                        pass
                    vok, vwhy = await _click(page, VERIFY_SEL, 4000)
                if vok:
                    print(f"[VERIFY] {rand_name} ok")
                    await page.wait_for_timeout(150)
                else:
                    print(f"[VERIFY] {rand_name} {vwhy} (skip)")
                    reason = "verify_button_missing"

            if reason is None:
                _tick()
                print(f"[MAIL] {rand_name} waiting...")
                verify_link = None
                delay = 0.5
                i = 0
                t0 = time.monotonic()
                mail_end = t0 + MAIL_TIMEOUT
                while time.monotonic() < mail_end:
                    i += 1
                    try:
                        if mail_token:
                            verify_link = await asyncio.wait_for(
                                asyncio.to_thread(tempmail.find_once, mail_token), timeout=20)
                        else:
                            verify_link = await asyncio.wait_for(
                                wait_for_verify_link(WORKER_API, email, attempts=1, interval=0.1),
                                timeout=20)
                        delay = 0.5
                    except Exception:
                        delay = min(delay * 1.5, 3.0)
                    if verify_link:
                        print(f"[MAIL] {rand_name} có link sau "
                              f"{int(time.monotonic()-t0)}s")
                        break
                    if i in (2, 6, 12):
                        if await asyncio.wait_for(_click(page, VERIFY_SEL, 1200), timeout=10):
                            print(f"[VERIFY] {rand_name} resend #{i}")
                    _tick()
                    await page.wait_for_timeout(int(delay * 1000))
                    _check_stop()
                st["t_mail"] = time.monotonic() - t0
                if not verify_link:
                    reason = "mail_timeout"

                if reason is None:
                    try:
                        if wsess is not None:
                            vurl, vtext = await asyncio.wait_for(
                                asyncio.to_thread(_web_verify, wsess, verify_link),
                                timeout=35)
                        else:
                            try:
                                await asyncio.wait_for(
                                    page.goto(verify_link,
                                              wait_until="domcontentloaded",
                                              timeout=45000),
                                    timeout=55)
                            except Exception as _e:
                                print(f"[VERIFY] {rand_name} goto loi "
                                      f"({type(_e).__name__}) -> doc trang hien tai")
                            await page.wait_for_timeout(1500)
                            vurl = page.url
                            vtext = (await asyncio.wait_for(page.evaluate(
                                "() => document.body ? document.body.innerText : ''"),
                                timeout=10)).lower()
                        bad = ("verify=error" in vurl or "verify=invalid" in vurl
                               or (("invalid" in vtext or "expired" in vtext)
                                   and ("link" in vtext or "token" in vtext
                                        or "verif" in vtext)))
                        if not bad and ("verify=success" not in vurl
                                        and "verified" not in vtext
                                        and "success" not in vtext):
                            print(f"[VERIFY] {rand_name} không thấy xác nhận "
                                  f"(url={vurl[:80]})")
                            await _dump_debug(page, rand_name, "verify_nofail")
                            for k in range(1, 4):
                                await asyncio.wait_for(
                                    _click(page, VERIFY_SEL, 1500), timeout=8)
                                print(f"[VERIFY] {rand_name} xin mail mới #{k}")
                                new = None
                                end2 = time.monotonic() + 25
                                while time.monotonic() < end2:
                                    _check_stop()
                                    try:
                                        if mail_token:
                                            new = await asyncio.wait_for(
                                                asyncio.to_thread(
                                                    tempmail.find_once, mail_token),
                                                timeout=20)
                                        else:
                                            new = await asyncio.wait_for(
                                                wait_for_verify_link(
                                                    WORKER_API, email,
                                                    attempts=1, interval=0.1),
                                                timeout=20)
                                    except Exception:
                                        new = None
                                    if new and new != verify_link:
                                        break
                                    await page.wait_for_timeout(700)
                                if not new or new == verify_link:
                                    continue
                                verify_link = new
                                if wsess is not None:
                                    vurl, vtext = await asyncio.wait_for(
                                        asyncio.to_thread(
                                            _web_verify, wsess, verify_link),
                                        timeout=35)
                                else:
                                    try:
                                        await asyncio.wait_for(
                                            page.goto(verify_link,
                                                      wait_until="domcontentloaded",
                                                      timeout=45000),
                                            timeout=55)
                                    except Exception as _e:
                                        print(f"[VERIFY] {rand_name} goto loi "
                                              f"({type(_e).__name__}) -> doc trang")
                                    await page.wait_for_timeout(1500)
                                    vurl = page.url
                                    vtext = (await asyncio.wait_for(page.evaluate(
                                        "() => document.body ? document.body.innerText : ''"),
                                        timeout=10)).lower()
                                if ("verify=success" in vurl
                                        or "verified" in vtext
                                        or "success" in vtext):
                                    print(f"[VERIFY] {rand_name} xác nhận ok "
                                          f"sau mail #{k}")
                                    break
                            else:
                                print(f"[VERIFY] {rand_name} vẫn không xác "
                                      f"nhận được -> thử key (có thể lỗi 403)")
                        if bad:
                            reason = "verify_link_invalid"
                        else:
                            _tick()
                            print(f"[KEY] {rand_name} fetching")
                            t0 = time.monotonic()
                            data = None
                            if wsess is not None:
                                data = await asyncio.to_thread(
                                    _web_create_key, wsess, rand_name)
                                if "error" in data:
                                    print(f"[KEY] {rand_name} web fail "
                                          f"({str(data.get('error'))[:90]}) -> UI")
                                    data = None
                            if data is None:
                                data = await _create_key(page, rand_name)
                            st["t_key"] = time.monotonic() - t0
                            if "error" not in data and "plaintext" in data:
                                api_key = data["plaintext"]
                                print(f"[KEY] {rand_name} = {api_key}")
                                print(f"[CHECK] {rand_name} chat test...")
                                _tick()
                                t0 = time.monotonic()

                                async def _check(_k=api_key, _t=45.0):
                                    if wsess is not None:
                                        return await asyncio.to_thread(
                                            _web_chat, wsess, _k, _t)
                                    return await _chat_check(page, _k, _t)

                                ok, info = await _check()
                                if ok is False and "verify your email" in info.lower():
                                    for _ in range(4):
                                        _check_stop()
                                        await asyncio.sleep(8)
                                        _tick()
                                        print(f"[CHECK] {rand_name} chờ verify, thử lại")
                                        ok, info = await _check()
                                        if ok is not False or "verify your email" not in info.lower():
                                            break
                                st["t_chat"] = time.monotonic() - t0
                                if ok is None:
                                    print(f"[CHECK] {rand_name} retry ({info})")
                                    _tick()
                                    ok, info = await _check(api_key, 70)
                                if ok is True:
                                    verified = True
                                    print(f"[CHECK] {rand_name} LIVE ({info})")
                                elif ok is False:
                                    print(f"[CHECK] {rand_name} DEAD ({info})")
                                    reason = f"key_dead: {info}"
                                    api_key = None
                                else:
                                    print(f"[CHECK] {rand_name} unverified ({info})")
                                    reason = f"key_unverified: {info}"
                            else:
                                reason = f"key_error: {data.get('error', 'no plaintext')}"
                    except asyncio.TimeoutError:
                        print(f"[VERIFY] {rand_name} timeout bước verify/key")
                        reason = "verify_timeout"
                    except Exception as e:
                        reason = f"verify_failed: {e}"

        except _Stopped:
            print(f"[STOP] {rand_name} bỏ giữa chừng")
            reason = "stopped"
        except Exception as e:
            print(f"[REG] {rand_name} error: {type(e).__name__}: {e}")
            LOG.exc(f"[REG] {rand_name} crashed")
            print(f"[LOG] {rand_name} traceback -> {LOG.path}")
            reason = reason or f"error: {type(e).__name__}"
        finally:
            code0 = (reason or "").split(":")[0].strip()
            if ipbad is not None and (code0 in IP_ROTATE
                                      or code0 in REGION_DEAD):
                ipbad.set()
            status = "live" if verified else (reason or "unknown")
            print(f"[STAT] {rand_name} load={st.get('t_load', 0):.0f}s "
                  f"signup={st.get('t_signup', 0):.0f}s "
                  f"mail={st.get('t_mail', 0):.0f}s "
                  f"key={st.get('t_key', 0):.1f}s "
                  f"chat={st.get('t_chat', 0):.1f}s -> {status.split(':')[0]}")
            if not verified and DEBUG_DUMP and page is not None:
                await _dump_debug(page, rand_name, status)
            try:
                async with file_lock:
                    conn.execute(
                        "INSERT OR REPLACE INTO accounts "
                        "(email, password, api_key, status, created_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (email, password, api_key, status,
                         datetime.now(timezone.utc).isoformat()))
                    conn.commit()
            except Exception as e:
                print(f"[DB] {rand_name} write fail: {type(e).__name__}: {e}")
            if verified and api_key:
                try:
                    await export_results(email, password, api_key)
                except Exception as e:
                    print(f"[IO] {rand_name} export fail: {type(e).__name__}: {e}")
                print(f"[DONE] {rand_name} | {api_key}")
                if NINE_ADD:
                    nok, nwhy = await asyncio.to_thread(
                        _nine_add_key, rand_name, api_key)
                    print(f"[NINE] {rand_name} "
                          f"{'thêm vào 9router ok' if nok else 'lỗi: ' + nwhy}")
                if UI is not None:
                    UI.finish(True)
            else:
                print(f"[FAIL] {rand_name} | {status}")
                if UI is not None:
                    UI.finish(False, status)

            wd.cancel()
            try:
                await wd
            except BaseException:
                pass
            if not st.get("closed"):
                if context is not None:
                    try:
                        await asyncio.wait_for(context.close(), timeout=10)
                    except Exception:
                        pass
                if browser is not None:
                    try:
                        await asyncio.wait_for(browser.close(), timeout=15)
                    except Exception:
                        pass
            print(f"[REG] {rand_name} xong")

        return status


_STOP_FLAG = False


class _Stopped(Exception):
    pass


def _stop_requested() -> bool:
    global _STOP_FLAG
    if _STOP_FLAG:
        return True
    if os.path.exists(STOP_FILE):
        try:
            os.remove(STOP_FILE)
        except OSError:
            pass
        _STOP_FLAG = True
        print("[STOP] nhận lệnh dừng từ web -> dừng batch hiện tại")
    return _STOP_FLAG


def _check_stop() -> None:
    if _stop_requested():
        raise _Stopped()


async def _switch_region(region: str, force: bool = False) -> bool:
    prev = VPN_IP["ip"]
    async with VPN_GATE:
        if force:
            print(f"[VPN] reconnect {region}")
            ok = await asyncio.to_thread(vpn.reconnect, region)
        else:
            print(f"[VPN] đổi region -> {region}")
            ok = await asyncio.to_thread(vpn.connect, region, 75.0, 2,
                                         avoid_ip=HOME_IP["ip"])
        if ok:
            ip = await asyncio.to_thread(vpn.current_ip)
            if ip:
                VPN_IP["ip"] = ip
            if HOME_IP["ip"] and ip == HOME_IP["ip"]:
                print(f"[VPN] {region} RÒ: IP ra = IP nhà ({ip}) -> bỏ region")
                await asyncio.to_thread(vpn.disconnect)
                return False
            print(f"[VPN] {region} sẵn sàng ip={ip or '?'}")
            return True
    print(f"[VPN] {region} FAIL (state={vpn.state()}) -> cứu mạng")
    async with VPN_GATE:
        await asyncio.to_thread(vpn.ensure, vpn_state["region"])
    return False


async def _net_guard():
    while True:
        await asyncio.sleep(20)
        if VPN_GATE.locked():
            continue
        st = await asyncio.to_thread(vpn.state)
        if st == "Connected" and await asyncio.to_thread(vpn.online, 3):
            continue
        print(f"[VPN] mất kết nối (state={st}) -> cứu")
        async with VPN_GATE:
            ok = await asyncio.to_thread(vpn.ensure, vpn_state["region"])
        print(f"[VPN] cứu {'ok' if ok else 'FAIL'}")


async def main():
    global UI
    if not reset_outputs():
        print("[START] không xoá được tokens.txt/accounts.txt (đang bị giữ?) -> dừng")
        return

    conn = init_db()
    semaphore = asyncio.Semaphore(CONCURRENT_TASKS)
    pool = None

    UI = _Progress(TOTAL_ACCOUNTS)
    UI.live = sys.stdout.isatty() and _enable_vt()
    ticker = None
    if UI.live:
        async def _tick_ui():
            while True:
                await asyncio.sleep(1)
                UI.draw()
        ticker = asyncio.create_task(_tick_ui())

    if not (os.environ.get("CAMOUFOX_WS") or "").strip():
        await asyncio.to_thread(ensure_foxserve, HEADLESS)

    print(f"[START] Total: {TOTAL_ACCOUNTS} | Concurrent: {CONCURRENT_TASKS} | "
          f"Net: {NET_MODE} | Headless: {HEADLESS} | Browser: fox")

    vprep = None
    guard = None
    if NET_MODE in ("express", "direct"):
        HOME_IP["ip"] = await asyncio.to_thread(vpn.public_ip4)
        vprep = await asyncio.to_thread(vpn.prepare)
        guard = asyncio.create_task(_net_guard())
        print(f"[VPN] state={vpn.state()} netlock={vpn.networklock() or '?'} "
              f"ip={vpn.current_ip() or 'không có mạng!'} "
              f"ip_nha={HOME_IP['ip'] or '?'}")

    try:
        per_ip = max(1, ACCS_PER_IP)
        if NET_MODE == "express":
            regions = await asyncio.to_thread(vpn.pool)
            if not regions:
                print("[VPN] không đọc được danh sách region -> dùng fallback")
            regions = regions or VPN_REGIONS[:]
            drop = await asyncio.to_thread(vpn.regions)
            blocked = [r for r in drop if vpn.is_blocked(r)]
            if blocked:
                print(f"[VPN] bỏ {len(blocked)} host bị chặn: "
                      + " ".join(blocked))
            random.shuffle(regions)
            print(f"[VPN] pool {len(regions)} host")
            slots = [0] * TOTAL_ACCOUNTS
            ri = 0
            iterations = 0
            attempts = 0
            bad_regions = set()
            dead_fail = {}
            max_attempts = TOTAL_ACCOUNTS * (MAX_TRIES + 2)
            max_iter = max_attempts + len(regions) * 8
            while slots and attempts < max_attempts:
                if _stop_requested():
                    break
                iterations += 1
                if iterations > max_iter:
                    print(f"[VPN] quá {max_iter} vòng -> dừng")
                    break
                region = None
                for _ in range(len(regions)):
                    cand = regions[ri % len(regions)]
                    ri += 1
                    if cand not in bad_regions:
                        region = cand
                        break
                if region is None:
                    if attempts < max_attempts:
                        print("[VPN] hết host dùng được -> thử lại vòng mới")
                        bad_regions.clear()
                        continue
                    print("[VPN] hết host dùng được -> dừng")
                    break
                if not await _switch_region(region):
                    dead_fail[region] = dead_fail.get(region, 0) + 1
                    if dead_fail[region] >= 2:
                        bad_regions.add(region)
                        print(f"[VPN] {region} không connect được -> bỏ")
                    continue
                dead_fail.pop(region, None)
                vpn_state["region"] = region
                if UI is not None:
                    UI.region = region
                batch = min(per_ip, len(slots))
                jobs = slots[:batch]
                slots = slots[batch:]
                attempts += batch
                ipbad = asyncio.Event()
                tasks = [register_one(conn, semaphore, pool, ipbad)
                         for _ in range(batch)]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                ip_bad = 0
                region_dead = False
                sf_bad = 0
                for r, tries in zip(results, jobs):
                    if isinstance(r, BaseException):
                        print(f"[REG] task lỗi: {type(r).__name__}: {r}")
                        LOG.exc_obj(r, "[REG] batch task crashed")
                        code = "error"
                    else:
                        code = r.split(":")[0].strip() if isinstance(r, str) else ""
                    if code == "live":
                        continue
                    if code == "stopped":
                        print("[STOP] bỏ acc còn lại của batch")
                        break
                    if code in REGION_DEAD:
                        region_dead = True
                    elif code in IP_ROTATE:
                        ip_bad += 1
                    elif code == "signup_failed":
                        sf_bad += 1
                    if tries + 1 < MAX_TRIES and attempts < max_attempts:
                        slots.append(tries + 1)
                        if UI is not None:
                            UI.requeue(code)
                if region_dead:
                    bad_regions.add(region)
                    print(f"[VPN] {region} bị chặn IP -> bỏ region")
                elif ip_bad or sf_bad >= 2:
                    bad_regions.add(region)
                    why = (f"{ip_bad} acc bị IP chặn" if ip_bad
                           else f"{sf_bad} signup fail")
                    print(f"[VPN] {region}: {why} -> bỏ host, sang host khác")
                print(f"[BATCH] còn {len(slots)}/{TOTAL_ACCOUNTS} "
                      f"(region {region}, lần thử {attempts})")
        elif NET_MODE == "nest":
            NET_MODE = "v6"
            slots = [0] * TOTAL_ACCOUNTS
            attempts = 0
            max_attempts = TOTAL_ACCOUNTS * MAX_TRIES
            while slots and attempts < max_attempts:
                if _stop_requested():
                    break
                if attempts:
                    if not await pool.force_rotate():
                        print("[NET] không lấy được IP mới -> thử lại sau 5s")
                        await asyncio.sleep(5)
                        continue
                batch = min(per_ip, len(slots))
                jobs = slots[:batch]
                slots = slots[batch:]
                attempts += batch
                tasks = [register_one(conn, semaphore, pool) for _ in range(batch)]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                ip_bad = 0
                for r, tries in zip(results, jobs):
                    if isinstance(r, BaseException):
                        print(f"[REG] task lỗi: {type(r).__name__}: {r}")
                        LOG.exc_obj(r, "[REG] batch task crashed")
                        code = "error"
                    else:
                        code = r.split(":")[0].strip() if isinstance(r, str) else ""
                    if code == "live":
                        continue
                    if code == "stopped":
                        print("[STOP] bỏ acc còn lại của batch")
                        break
                    if code in IP_ROTATE:
                        ip_bad += 1
                    if tries + 1 < MAX_TRIES and attempts < max_attempts:
                        if code in IP_ROTATE:
                            slots.insert(0, tries + 1)
                        else:
                            slots.append(tries + 1)
                        if UI is not None:
                            UI.requeue(code)
                if ip_bad:
                    print(f"[NET] {ip_bad}/{len(jobs)} acc bị chặn IP "
                          f"(CF) -> xoay IP, chạy lại trước")
                print(f"[BATCH] còn {len(slots)}/{TOTAL_ACCOUNTS} "
                      f"(nest, lần thử {attempts})")
        else:
            slots = [0] * TOTAL_ACCOUNTS
            attempts = 0
            max_attempts = TOTAL_ACCOUNTS * MAX_TRIES
            quota_streak = 0
            while slots and attempts < max_attempts:
                if _stop_requested():
                    break
                batch = min(per_ip, len(slots))
                jobs = slots[:batch]
                slots = slots[batch:]
                attempts += batch
                ipbad = None if NET_MODE == "v6" else asyncio.Event()
                tasks = [register_one(conn, semaphore, pool, ipbad)
                         for _ in range(batch)]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                quota_hits = 0
                for r, tries in zip(results, jobs):
                    code = (r.split(":")[0].strip()
                            if isinstance(r, str) else "error")
                    if code == "live":
                        continue
                    if code == "too_many_signups":
                        quota_hits += 1
                    if code == "stopped":
                        print("[STOP] bỏ acc còn lại của batch")
                        break
                    if tries + 1 < MAX_TRIES and attempts < max_attempts:
                        slots.append(tries + 1)
                        if UI is not None:
                            UI.requeue(code)
                print(f"[BATCH] còn {len(slots)}/{TOTAL_ACCOUNTS} "
                      f"(direct, lần thử {attempts})")
                if quota_hits:
                    quota_streak += 1
                    wait_s = min(900, 180 * quota_streak)
                    if quota_streak >= 4:
                        print(f"[QUOTA] het quota signup cua network "
                              f"({quota_streak} lan) -> dung batch, nghi {wait_s}s")
                        await asyncio.sleep(wait_s)
                        break
                    print(f"[QUOTA] het quota signup cua network "
                          f"-> nghi {wait_s}s roi thu lai")
                    await asyncio.sleep(wait_s)
                else:
                    quota_streak = 0
    finally:
        if guard is not None:
            guard.cancel()
        if vprep is not None:
            await asyncio.to_thread(vpn.restore, vprep)
        if ticker is not None:
            ticker.cancel()
        try:
            conn.close()
        except Exception:
            pass
        try:
            await release_shared()
        except Exception:
            pass
        if (os.environ.get("REG_FOX_KEEP") or "1").strip() == "0":
            meta = _fox_meta()
            if meta:
                _fox_kill(meta.get("pid"))
                try:
                    os.remove(FOX_WS_FILE)
                except OSError:
                    pass

    if UI is not None:
        UI.summary()


def _ask_int(prompt, default):
    try:
        v = input(f"{prompt} [{default}]: ").strip()
        return int(v) if v else default
    except Exception:
        return default


def _ask_choice(prompt, options, default):
    print(f"\n{prompt}")
    for i, (label, _val) in enumerate(options, 1):
        mark = " (default)" if _val == default else ""
        print(f"  {i}. {label}{mark}")
    try:
        v = input("chọn: ").strip()
        if not v:
            return default
        idx = int(v)
        if 1 <= idx <= len(options):
            return options[idx - 1][1]
    except Exception:
        pass
    return default


def _interactive_setup():
    print("=" * 46)
    print("  TOKENHARBOR REG")
    print("=" * 46)
    count = _ask_int("Số account", TOTAL_ACCOUNTS)
    concurrent = _ask_int("Số task song song", CONCURRENT_TASKS)
    net = _ask_choice("Mạng:", [
        ("direct  — đi thẳng VPN hiện tại", "direct"),
        ("express — tự đổi region ExpressVPN", "express"),
        ("v6      — xoay IPv6 local", "v6"),
    ], NET_MODE)
    per_ip = _ask_int("Số acc dùng chung 1 IP", ACCS_PER_IP)
    if per_ip < 1:
        per_ip = 1
    hl = _ask_choice("Trình duyệt:", [
        ("hiện cửa sổ (headed)", "false"),
        ("ẩn (headless)", "true"),
    ], "true" if HEADLESS else "false")
    web = _ask_choice("Tạo key / verify / chat:", [
        ("UI thật (chậm, chắc)", "false"),
        ("web HTTP (nhanh hơn)", "true"),
    ], "false")
    nine = _ask_choice("Thêm key vào 9router khi acc live:", [
        ("có (provider 'a')", "true"),
        ("không", "false"),
    ], "true")
    NINE_ADD = nine == "true"
    print("=" * 46)
    global WEB_MODE
    WEB_MODE = web == "true"
    return count, concurrent, net, per_ip, hl == "true"


def _truthy(v: str) -> bool:
    return v.strip().lower() in ("1", "true", "yes", "on")


def _apply_env():
    global TOTAL_ACCOUNTS, CONCURRENT_TASKS, NET_MODE, HEADLESS, ACCS_PER_IP
    global PROXY_PER_IP, WEB_MODE, NINE_ADD, NINE_PROVIDER
    e = os.environ
    if e.get("REG_COUNT"):
        TOTAL_ACCOUNTS = int(e["REG_COUNT"])
    if e.get("REG_CONCURRENT"):
        CONCURRENT_TASKS = int(e["REG_CONCURRENT"])
    if e.get("REG_NET"):
        net = e["REG_NET"].strip().lower()
        if net not in VALID_NET:
            raise SystemExit(f"REG_NET='{net}' không hợp lệ (chọn: {', '.join(VALID_NET)})")
        NET_MODE = net
    if e.get("REG_HEADLESS"):
        HEADLESS = _truthy(e["REG_HEADLESS"])
    if e.get("REG_WEB"):
        WEB_MODE = _truthy(e["REG_WEB"])
    if e.get("REG_NINE"):
        NINE_ADD = _truthy(e["REG_NINE"])
    if e.get("REG_NINE_PROVIDER"):
        NINE_PROVIDER = e["REG_NINE_PROVIDER"].strip()
    if e.get("REG_PER_IP"):
        per_ip = int(e["REG_PER_IP"])
        if per_ip < 1:
            raise SystemExit(f"REG_PER_IP={per_ip} không hợp lệ (phải >= 1)")
        ACCS_PER_IP = per_ip
        PROXY_PER_IP = ACCS_PER_IP


if __name__ == "__main__":
    if os.environ.get("REG_COUNT") or os.environ.get("REG_NET"):
        _apply_env()
    elif sys.stdin.isatty():
        TOTAL_ACCOUNTS, CONCURRENT_TASKS, NET_MODE, ACCS_PER_IP, HEADLESS = _interactive_setup()
        PROXY_PER_IP = ACCS_PER_IP
    reglog.note_startup(
        LOG, net=NET_MODE, count=TOTAL_ACCOUNTS, concurrent=CONCURRENT_TASKS,
        per_ip=ACCS_PER_IP, headless=HEADLESS, web=WEB_MODE, router_add=NINE_ADD,
        provider=NINE_PROVIDER, router=NINE_URL, target=TARGET_URL,
        verbose=VERBOSE, self_log=SELF_LOG)
    if SELF_LOG:
        _ORIG_PRINT(f"[LOG] engine log -> {LOG.path}")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        LOG.info("interrupted (Ctrl+C)")
    except SystemExit:
        raise
    except BaseException:
        LOG.exc("engine run failed")
        _ORIG_PRINT(f"[LOG] crashed, traceback -> {LOG.path}")
        raise
