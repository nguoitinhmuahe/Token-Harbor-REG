
from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.request
from urllib.parse import urlparse
from contextlib import asynccontextmanager

from camoufox.async_api import AsyncCamoufox
from playwright.async_api import async_playwright

STARTUP_PREFS = {
    "browser.safebrowsing.enabled": False,
    "browser.safebrowsing.malware.enabled": False,
    "browser.safebrowsing.phishing.enabled": False,
    "browser.safebrowsing.downloads.enabled": False,
    "browser.safebrowsing.downloads.remote.enabled": False,
    "browser.search.update": False,
    "browser.region.update.enabled": False,
    "toolkit.telemetry.enabled": False,
    "toolkit.telemetry.unified": False,
    "datareporting.healthreport.uploadEnabled": False,
    "datareporting.policy.dataSubmissionEnabled": False,
    "browser.ping-centre.telemetry": False,
    "extensions.pocket.enabled": False,
    "network.dns.disablePrefetch": True,
    "network.prefetch-next": False,
    "network.predictor.enabled": False,
    "network.http.speculative-parallel-limit": 0,
    "browser.urlbar.speculativeConnect.enabled": False,
    "geo.enabled": False,
    "browser.shell.checkDefaultBrowser": False,
    "browser.startup.homepage_override.mstone": "ignore",
    "browser.aboutwelcome.enabled": False,
    "browser.newtabpage.enabled": False,
}

FAST_OPTS = {
    "humanize": False,
    "i_know_what_im_doing": True,
    "canvas_noise": False,
    "pin_cpu_cores": False,
    "disable_coop": True,
    "geoip": True,
}


@asynccontextmanager
async def fast_browser(headless: bool = True, prefs: dict | None = None, **overrides):
    opts = dict(FAST_OPTS)
    opts["headless"] = headless
    opts["firefox_user_prefs"] = {**STARTUP_PREFS, **(prefs or {})}
    opts.update(overrides)

    async with AsyncCamoufox(**opts) as browser:
        yield browser


WS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".camoufox_ws")


def read_server_meta() -> dict | None:
    env = os.environ.get("CAMOUFOX_WS")
    if env:
        return {"ws": env.strip(), "headless": True}
    try:
        with open(WS_FILE, "r", encoding="utf-8") as f:
            raw = f.read().strip()
    except OSError:
        return None
    if not raw:
        return None
    if raw.startswith("{"):
        try:
            meta = json.loads(raw)
        except json.JSONDecodeError:
            meta = None
        if meta and meta.get("ws"):
            return {"ws": meta["ws"], "headless": bool(meta.get("headless", True))}
    return {"ws": raw, "headless": True}


def read_ws_endpoint() -> str | None:
    meta = read_server_meta()
    return meta["ws"] if meta else None


def write_ws_endpoint(url: str, headless: bool = True) -> None:
    with open(WS_FILE, "w", encoding="utf-8") as f:
        f.write(json.dumps({"ws": url, "headless": headless}))


@asynccontextmanager
async def connect_browser(endpoint: str, timeout: float = 15.0):
    pw = await async_playwright().start()
    try:
        browser = await asyncio.wait_for(pw.firefox.connect(endpoint), timeout=timeout)
    except BaseException:
        await pw.stop()
        raise
    try:
        yield browser
    finally:
        await pw.stop()


@asynccontextmanager
async def browser_session(headless: bool = True, prefs: dict | None = None,
                          endpoint: str | None = None, connect_timeout: float = 15.0,
                          **overrides):
    if endpoint:
        meta = {"ws": endpoint, "headless": headless}
    else:
        meta = read_server_meta()

    if meta and meta["headless"] == headless:
        pw = await async_playwright().start()
        try:
            browser = await asyncio.wait_for(
                pw.firefox.connect(meta["ws"]), timeout=connect_timeout)
        except Exception as e:
            print(f"[fastfox] connect fail ({e}); fallback launch")
            try:
                await pw.stop()
            except Exception:
                pass
        else:
            try:
                print(f"[fastfox] connected {meta['ws']} (headless={headless})")
                yield browser
            finally:
                await pw.stop()
            return
    elif meta:
        print(f"[fastfox] server headless={meta['headless']} != requested {headless}; "
              f"launch directly")

    async with fast_browser(headless=headless, prefs=prefs, **overrides) as browser:
        yield browser


ASSET_EXTS = (
    "png", "jpg", "jpeg", "webp", "gif", "svg", "ico", "bmp", "avif",
    "woff", "woff2", "ttf", "otf", "eot",
    "mp4", "webm", "ogg", "mp3",
    "map",
)


def _is_asset(url) -> bool:
    if not isinstance(url, str):
        url = str(url)
    path = urlparse(url).path.lower()
    dot = path.rfind(".")
    return dot != -1 and path[dot + 1:] in ASSET_EXTS


async def _abort(route) -> None:
    try:
        await route.abort()
    except Exception:
        pass


async def block_assets(page) -> None:
    await page.route(_is_asset, _abort)


def _http_get(url: str, timeout: float = 10.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


async def fetch_inbox(api: str, email: str, timeout: float = 10.0) -> str:
    return await asyncio.to_thread(_http_get, f"{api}?to={email}", timeout)


DEFAULT_VERIFY_BASE = "https://tokenharbor.ai/verify-email?token="


def _decode_mail(raw: str) -> str:
    s = raw.lstrip()
    if s[:1] == "{":
        try:
            data = json.loads(s)
        except json.JSONDecodeError:
            return raw
        if isinstance(data, dict) and isinstance(data.get("body"), str):
            return data["body"]
    elif s[:1] == "[":
        try:
            data = json.loads(s)
        except json.JSONDecodeError:
            return raw
        if isinstance(data, list) and data and isinstance(data[-1], dict) \
                and isinstance(data[-1].get("body"), str):
            return data[-1]["body"]
    return raw


def extract_verify_link(body: str, base: str = DEFAULT_VERIFY_BASE) -> str | None:
    if not body:
        return None
    raw = _decode_mail(body).replace("=\r\n", "").replace("=\n", "")
    if base not in raw:
        return None
    rest = raw.split(base, 1)[1]
    token = re.split(r"[\s\"'<>)\]]", rest, 1)[0]
    if "If" in token:
        token = token.split("If", 1)[0]
    token = token.rstrip("=")
    if token.startswith("3D"):
        token = token[2:]
    if not token:
        return None
    return base + token


async def wait_for_verify_link(
    api: str,
    email: str,
    *,
    attempts: int = 60,
    interval: float = 0.5,
    base: str = DEFAULT_VERIFY_BASE,
) -> str | None:
    for _ in range(attempts):
        try:
            link = extract_verify_link(await fetch_inbox(api, email), base)
            if link:
                return link
        except Exception:
            pass
        await asyncio.sleep(interval)
    return None
