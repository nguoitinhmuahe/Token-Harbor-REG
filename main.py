import os
import re
import sys
import time
import json
import socket
import random
import string
import sqlite3
import asyncio
from datetime import datetime, timezone
from urllib.parse import urlparse

import mailtm
import vpn
from chromedrv import open_chrome


async def open_browser(headless: bool):
    return await open_chrome(headless=headless)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

if sys.stdout.isatty():
    os.system('cls' if os.name == 'nt' else 'clear')

TARGET_URL = "https://tokenharbor.ai/login?mode=signup"
KEYS_URL = "https://tokenharbor.ai/dashboard/api-keys"
BASE_HOST = "tokenharbor.ai"

TOTAL_ACCOUNTS = 30
CONCURRENT_TASKS = 5
HEADLESS = True

ACCS_PER_IP = 10
ACC_TIMEOUT = 180
MAIL_TIMEOUT = 90
VPN_REGIONS = [
    "vietnam",
    "singapore-cbd", "singapore-jurong", "singapore-marina-bay",
    "thailand", "malaysia", "indonesia", "philippines",
    "japan-tokyo", "japan-shibuya", "japan-osaka", "japan-yokohama",
    "hong-kong-1", "hong-kong-2", "taiwan-3",
]
vpn_state = {"region": None}

HARD_BLOCK = ("too_many_signups", "ip_not_supported")
REGION_DEAD = HARD_BLOCK
IP_ROTATE = ("captcha_missing", "network_blocked", "signup_page_timeout",
             "cf_timeout", "cf_blocked")
MAX_IP_ROTATE = 3
MAX_TRIES = 6

DB_PATH = "accounts.db"
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
        n = int(frac * width)
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
        _ORIG_PRINT("")
        _ORIG_PRINT("=" * 58)
        _ORIG_PRINT(f"  XONG  ok {self.ok} | fail {self.fail} | "
                    f"tổng {self.done}/{self.total}")
        _ORIG_PRINT(f"  thời gian {self.mmss(el)} | {rate:.1f} acc/h")
        if self.reasons:
            _ORIG_PRINT("  lý do fail:")
            for k, v in sorted(self.reasons.items(), key=lambda x: -x[1]):
                _ORIG_PRINT(f"    {v:>3}  {k}")
        _ORIG_PRINT("=" * 58)


_NOISE_SUB = (
    "[CF]", "[TIME]", "[FORM]", "[SIGNUP] POST", "resend #",
    "[MAIL] ", "fetching", "bấm '+ New key'", "nhập tên ok",
    "bấm 'Create key'", "chưa thấy key", "Enter (fallback)",
    "mở trang keys lỗi", "chat test...",
    "[CHECK] ", "[VERIFY] ", "browser mở", " = thk_",
)


def print(*args, **kwargs):
    msg = " ".join(str(a) for a in args)
    if not VERBOSE:
        if any(s in msg for s in _NOISE_SUB):
            return
        if msg.startswith("[REG]") and msg.endswith("xong"):
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
                    os.remove(p)
                break
            except OSError:
                time.sleep(0.5)
        else:
            return False
    return True


def gen_random_string(length=8):
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))


def gen_password(length=16) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_"
    return ''.join(random.choices(alphabet, k=length))


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
        st = await _cf_state(page)
        if st in ("absent", "solved"):
            return st
        now = time.monotonic()
        if now - last_click > 3:
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


async def _fill_form(page, email, password) -> bool:
    try:
        await page.wait_for_selector(SEL_EMAIL, timeout=8000)
        await page.fill(SEL_EMAIL, email)
        await page.fill(SEL_PASSWORD, password)
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
    except Exception:
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


async def _load_signup(page, rand_name) -> bool:
    for attempt in range(4):
        try:
            await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=40000)
        except Exception as e:
            print(f"[SIGNUP] {rand_name} goto try{attempt+1}: {str(e)[:90]}")
            await page.wait_for_timeout(500)
            continue

        end = time.monotonic() + 35
        clicked = 0
        last_click = 0.0
        while time.monotonic() < end:
            if await page.query_selector(SEL_EMAIL):
                print(f"[CF] {rand_name} form hiện sau {int(35-(end-time.monotonic()))}s")
                return True

            now = time.monotonic()
            if await _is_cf_interstitial(page):
                if clicked == 0:
                    print(f"[CF] {rand_name} dính CF, click Turnstile...")
                if now - last_click > 4:
                    last_click = now
                    if await _click_turnstile(page):
                        clicked += 1
                        if clicked <= 3:
                            print(f"[CF] {rand_name} click #{clicked}")
            else:
                try:
                    blen = await page.evaluate(
                        "() => { const b = document.body || document.documentElement;"
                        " return b ? b.innerText.trim().length : 0; }")
                except Exception:
                    blen = -1
                if 0 <= blen < 40 and now - (end - 35) > 3:
                    print(f"[SIGNUP] {rand_name} trang trắng/không phải CF (len={blen}) -> thử lại")
                    break
            await page.wait_for_timeout(400)

        try:
            info = await page.evaluate("""() => ({
                url: location.href,
                title: document.title,
                bodyLen: document.body ? document.body.innerText.length : -1,
                head: (document.body ? document.body.innerText : '').slice(0,150),
                cf: !!document.querySelector('iframe[src*="challenges.cloudflare.com"]')
            })""")
            print(f"[CF] {rand_name} try{attempt+1} thất bại: {info}")
        except Exception as e:
            print(f"[CF] {rand_name} try{attempt+1}: eval lỗi {type(e).__name__}")

        await _clear_browsing(page)
        await page.wait_for_timeout(500)
    return False


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
            const w = !!document.querySelector('.cf-turnstile') || !!f;
            const body = (document.body && document.body.innerText || '').toLowerCase();
            const err = body.includes('too many attempts')
                     || body.includes('site is busy')
                     || body.includes('human check failed');
            return {tok, widget: w, err};
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
    clicks = 0
    while time.monotonic() < end:
        info = await _ts_probe(page)
        if info.get("tok", 0) > 20:
            return True
        if info.get("err"):
            return False
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
        await page.wait_for_timeout(2000)
    return (await _ts_probe(page)).get("tok", 0) > 20


CF_CSP_DROP = ("content-security-policy", "content-security-policy-report-only")


async def _strip_cf_csp(context) -> None:
    async def _route(route):
        req = route.request
        if "challenges.cloudflare.com" in req.url and req.resource_type == "document":
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

    await context.route("**/*", _route)


async def _signup(page, rand_name, email, password, timeout: float = 60.0) -> str:
    MAX_ATTEMPTS = 4
    end = time.monotonic() + timeout
    last_post = {"body": "", "status": 0}
    reloads = 0
    cf_retry = 0

    async def _on_resp(resp):
        try:
            if resp.request.method == "POST" and BASE_HOST in resp.url:
                last_post["body"] = (await resp.text())[:300]
                last_post["status"] = resp.status
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
        if not await _click_create(page):
            print(f"[SIGNUP] {rand_name} không click được Create, thử lại")
            continue
        print(f"[SIGNUP] {rand_name} submit #{attempt}")

        post_err = ""
        for _ in range(8):
            await page.wait_for_timeout(1000)
            if not _on_auth_page(page.url):
                return "ok"
            low = last_post["body"].lower()
            if low:
                if (last_post.get("status") == 429 or "just a moment" in low
                        or "cf-chl" in low or "checking your browser" in low):
                    cf_retry += 1
                    if cf_retry > 1:
                        return "cf_blocked"
                    print(f"[CF] {rand_name} 429 -> thu qua challenge, giu IP")
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
            if await _solve_turnstile(page, rand_name, 30):
                print(f"[CF] {rand_name} có token -> submit lại")
                continue
            if reloads >= 2:
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


async def _chat_check(page, api_key, timeout_s: float = 60.0):
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
    try:
        r = await asyncio.wait_for(
            page.evaluate(js, [api_key, int(timeout_s * 1000)]),
            timeout=timeout_s + 20)
    except Exception as e:
        return None, f"eval_error {type(e).__name__}"

    st = r.get("status")
    body = (r.get("body") or "")
    low = body.lower()
    if st == 200 and "choices" in low:
        return True, "chat_ok"
    if st == 401:
        return False, f"401 unauthorized"
    if st == 403:
        return False, f"403 forbidden {low[:80]}"
    if st == 429 or "rate limit" in low or "rate_limit" in low:
        return False, f"429 {low[:100]}"
    if st == 0:
        return None, f"timeout {low[:60]}"
    return None, f"http_{st} {low[:80]}"


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
        for attempt in (1, 2):
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


async def register_one(conn, semaphore):
    async with semaphore:
        rand_name = gen_random_string(10)
        password = gen_password()

        mail_token = None
        try:
            dom = random.choice(await asyncio.to_thread(mailtm.domains))
            email = f"{rand_name}@{dom}"
            await asyncio.to_thread(mailtm.create, email, password)
            mail_token = await asyncio.to_thread(mailtm.token, email, password)
        except Exception as e:
            _ORIG_PRINT(f"[FAIL] {rand_name} | mail_failed: {type(e).__name__}")
            if UI is not None:
                UI.finish(False, "mail_failed")
            return

        print(f"[REG] {rand_name} <{email}>")
        if UI is not None:
            UI.start()

        api_key = None
        verified = False
        reason = None
        browser = None
        context = None
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
            print(f"[NET] {rand_name} express {vpn_state['region']}")

            if reason is None:
                browser = await asyncio.wait_for(open_browser(HEADLESS), timeout=ACC_TIMEOUT)
                st["browser"] = browser
                context = await browser.new_context()
                st["context"] = context
                await _strip_cf_csp(context)
                page = await context.new_page()
                _tick()
                print(f"[REG] {rand_name} browser mở")

            t0 = time.monotonic()
            try:
                loaded = await asyncio.wait_for(_load_signup(page, rand_name), timeout=150)
            except asyncio.TimeoutError:
                print(f"[SIGNUP] {rand_name} load timeout 150s")
                loaded = False
            _tick()
            print(f"[TIME] {rand_name} load={int(time.monotonic()-t0)}s")
            if not loaded:
                reason = "signup_page_timeout"
            else:
                t0 = time.monotonic()
                ws = await asyncio.wait_for(
                    _signup(page, rand_name, email, password, timeout=150), timeout=170)
                _tick()
                print(f"[SIGNUP] {rand_name} -> {ws} ({int(time.monotonic()-t0)}s)")
                if ws != "ok":
                    reason = ws

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
                        verify_link = await asyncio.wait_for(
                            asyncio.to_thread(mailtm.find_once, mail_token), timeout=20)
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
                if not verify_link:
                    reason = "mail_timeout"

                if reason is None:
                    try:
                        await asyncio.wait_for(
                            page.goto(verify_link, timeout=20000), timeout=25)
                        await page.wait_for_timeout(300)
                        vurl = page.url
                        vtext = (await asyncio.wait_for(page.evaluate(
                            "() => document.body ? document.body.innerText : ''"),
                            timeout=10)).lower()
                        bad = ("verify=error" in vurl or "verify=invalid" in vurl
                               or (("invalid" in vtext or "expired" in vtext)
                                   and ("link" in vtext or "token" in vtext
                                        or "verif" in vtext)))
                        if bad:
                            reason = "verify_link_invalid"
                        else:
                            if ("verify=success" not in vurl
                                    and "verified" not in vtext
                                    and "success" not in vtext):
                                print(f"[VERIFY] {rand_name} no success flag "
                                      f"(url={vurl})")
                            _tick()
                            print(f"[KEY] {rand_name} fetching")
                            data = await _create_key(page, rand_name)
                            if "error" not in data and "plaintext" in data:
                                api_key = data["plaintext"]
                                print(f"[KEY] {rand_name} = {api_key}")
                                print(f"[CHECK] {rand_name} chat test...")
                                _tick()
                                ok, info = await _chat_check(page, api_key)
                                if ok is None:
                                    print(f"[CHECK] {rand_name} retry ({info})")
                                    _tick()
                                    ok, info = await _chat_check(page, api_key, 90)
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

        except Exception as e:
            print(f"[REG] {rand_name} error: {type(e).__name__}: {e}")
            reason = reason or f"error: {type(e).__name__}"
        finally:
            status = "live" if verified else (reason or "unknown")
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
                if UI is not None:
                    UI.finish(True)
            else:
                print(f"[FAIL] {rand_name} | {status}")
                if UI is not None:
                    UI.finish(False, status)

            wd.cancel()
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


async def _switch_region(region: str, force: bool = False) -> bool:
    if force:
        print(f"[VPN] reconnect {region} để lấy IP mới")
        try:
            await asyncio.to_thread(vpn.disconnect)
            await asyncio.sleep(1.0)
        except Exception:
            pass
    else:
        print(f"[VPN] đổi region -> {region}")
    ok = await asyncio.to_thread(vpn.connect, region)
    if not ok:
        print(f"[VPN] connect {region} FAIL")
        return False
    for _ in range(20):
        await asyncio.sleep(1.5)
        try:
            await asyncio.to_thread(socket.gethostbyname, BASE_HOST)
            ip = await asyncio.to_thread(vpn.current_ip)
            if ip:
                print(f"[VPN] {region} sẵn sàng ip={ip}")
                return True
        except Exception:
            pass
    print(f"[VPN] {region} KHÔNG ổn định sau 30s")
    return False


async def main():
    global UI
    if not reset_outputs():
        print("[START] không xoá được tokens.txt/accounts.txt (đang bị giữ?) -> dừng")
        return

    conn = init_db()
    semaphore = asyncio.Semaphore(CONCURRENT_TASKS)

    UI = _Progress(TOTAL_ACCOUNTS)
    UI.live = sys.stdout.isatty() and _enable_vt()
    ticker = None
    if UI.live:
        async def _tick_ui():
            while True:
                await asyncio.sleep(1)
                UI.draw()
        ticker = asyncio.create_task(_tick_ui())

    print(f"[START] Total: {TOTAL_ACCOUNTS} | Concurrent: {CONCURRENT_TASKS} | "
          f"Net: express | Headless: {HEADLESS} | Browser: fox")

    try:
        per_ip = max(1, ACCS_PER_IP)
        regions = VPN_REGIONS[:]
        random.shuffle(regions)
        slots = [0] * TOTAL_ACCOUNTS
        ri = 0
        iterations = 0
        attempts = 0
        bad_regions = set()
        dead_fail = {}
        rotate = {}
        pending_region = None
        max_attempts = TOTAL_ACCOUNTS * (MAX_TRIES + 2)
        max_iter = max_attempts + len(regions) * 8
        while slots and attempts < max_attempts:
            iterations += 1
            if iterations > max_iter:
                print(f"[VPN] quá {max_iter} vòng -> dừng")
                break
            force = False
            if pending_region:
                region, force = pending_region, True
                pending_region = None
            else:
                region = None
                for _ in range(len(regions)):
                    cand = regions[ri % len(regions)]
                    ri += 1
                    if cand not in bad_regions:
                        region = cand
                        break
                if region is None:
                    if attempts < max_attempts:
                        print("[VPN] hết region dùng được -> thử lại vòng mới")
                        bad_regions.clear()
                        continue
                    print("[VPN] hết region dùng được -> dừng")
                    break
            if not await _switch_region(region, force=force):
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
            tasks = [register_one(conn, semaphore) for _ in range(batch)]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            ip_bad = 0
            region_dead = False
            sf_bad = 0
            for r, tries in zip(results, jobs):
                if isinstance(r, BaseException):
                    print(f"[REG] task lỗi: {type(r).__name__}: {r}")
                    code = "error"
                else:
                    code = r.split(":")[0].strip() if isinstance(r, str) else ""
                if code == "live":
                    continue
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
                rotate[region] = rotate.get(region, 0) + 1
                if rotate[region] <= MAX_IP_ROTATE:
                    print(f"[VPN] {region}: {ip_bad} chặn IP + {sf_bad} signup fail"
                          f" -> reconnect lấy IP mới "
                          f"({rotate[region]}/{MAX_IP_ROTATE})")
                    pending_region = region
                else:
                    bad_regions.add(region)
                    print(f"[VPN] {region} đổi {MAX_IP_ROTATE} IP vẫn lỗi -> bỏ")
            print(f"[BATCH] còn {len(slots)}/{TOTAL_ACCOUNTS} "
                  f"(region {region}, lần thử {attempts})")
    finally:
        if ticker is not None:
            ticker.cancel()
        try:
            conn.close()
        except Exception:
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
    per_ip = _ask_int("Số acc dùng chung 1 region", ACCS_PER_IP)
    if per_ip < 1:
        per_ip = 1
    hl = _ask_choice("Trình duyệt:", [
        ("hiện cửa sổ (headed)", "false"),
        ("ẩn (headless)", "true"),
    ], "true" if HEADLESS else "false")
    print("=" * 46)
    return count, concurrent, per_ip, hl == "true"


def _truthy(v: str) -> bool:
    return v.strip().lower() in ("1", "true", "yes", "on")


def _apply_env():
    global TOTAL_ACCOUNTS, CONCURRENT_TASKS, HEADLESS, ACCS_PER_IP
    e = os.environ
    if e.get("REG_COUNT"):
        TOTAL_ACCOUNTS = int(e["REG_COUNT"])
    if e.get("REG_CONCURRENT"):
        CONCURRENT_TASKS = int(e["REG_CONCURRENT"])
    if e.get("REG_HEADLESS"):
        HEADLESS = _truthy(e["REG_HEADLESS"])
    if e.get("REG_PER_IP"):
        per_ip = int(e["REG_PER_IP"])
        if per_ip < 1:
            raise SystemExit(f"REG_PER_IP={per_ip} không hợp lệ (phải >= 1)")
        ACCS_PER_IP = per_ip


if __name__ == "__main__":
    if os.environ.get("REG_COUNT"):
        _apply_env()
    elif sys.stdin.isatty():
        TOTAL_ACCOUNTS, CONCURRENT_TASKS, ACCS_PER_IP, HEADLESS = _interactive_setup()
    asyncio.run(main())
