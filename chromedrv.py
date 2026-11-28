
from __future__ import annotations

import asyncio

from camoufox.async_api import AsyncNewContext

from fastfox import fast_browser, read_server_meta

_SH = {"lock": None, "pw": None, "browser": None, "ws": None}


def _lock() -> asyncio.Lock:
    if _SH["lock"] is None:
        _SH["lock"] = asyncio.Lock()
    return _SH["lock"]


async def _drop_shared() -> None:
    br, pw = _SH.get("browser"), _SH.get("pw")
    _SH.update({"pw": None, "browser": None, "ws": None})
    if br is not None:
        try:
            await asyncio.wait_for(br.close(), timeout=8)
        except Exception:
            pass
    if pw is not None:
        try:
            await pw.stop()
        except Exception:
            pass


async def _shared_browser(ws: str):
    async with _lock():
        br = _SH.get("browser")
        if br is not None:
            try:
                ok = br.is_connected()
            except Exception:
                ok = False
            if ok and _SH.get("ws") == ws:
                return br
            await _drop_shared()

        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        try:
            br = await asyncio.wait_for(pw.firefox.connect(ws), timeout=10)
        except Exception as e:
            print(f"[FOX] connect fail ({type(e).__name__}); launch rieng")
            try:
                await pw.stop()
            except Exception:
                pass
            return None
        _SH.update({"pw": pw, "browser": br, "ws": ws})
        return br


async def release_shared() -> None:
    pw = _SH.get("pw")
    _SH.update({"pw": None, "browser": None, "ws": None})
    if pw is not None:
        try:
            await asyncio.wait_for(pw.stop(), timeout=8)
        except Exception:
            pass


class FoxBrowser:

    def __init__(self, cm=None, browser=None, pw=None, shared: bool = False):
        self._cm = cm
        self._browser = browser
        self._pw = pw
        self._shared = shared
        self._ctxs = []

    async def new_context(self, **kwargs):
        if self._pw is not None:
            proxy = kwargs.pop("proxy", None)
            kwargs.setdefault("no_viewport", True)
            ctx = await AsyncNewContext(self._browser, proxy=proxy, **kwargs)
        else:
            ctx = await self._browser.new_context(**kwargs)
        self._ctxs.append(ctx)
        return ctx

    @property
    def contexts(self):
        try:
            return self._browser.contexts
        except Exception:
            return []

    async def close(self):
        if self._shared:
            ctxs, self._ctxs = self._ctxs, []
            for c in ctxs:
                try:
                    await asyncio.wait_for(c.close(), timeout=10)
                except Exception:
                    pass
            return
        try:
            await self._cm.__aexit__(None, None, None)
        except Exception:
            pass


async def open_chrome(headless: bool = False, proxy: dict | None = None,
                      locale: str | None = None,
                      timezone_id: str | None = None) -> FoxBrowser:
    meta = read_server_meta()
    if meta and meta["headless"] == headless:
        browser = await _shared_browser(meta["ws"])
        if browser is not None:
            return FoxBrowser(browser=browser, shared=True)

    cm = fast_browser(headless=headless)
    browser = await cm.__aenter__()
    return FoxBrowser(cm=cm, browser=browser)
