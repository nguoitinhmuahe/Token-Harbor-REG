
from __future__ import annotations

import asyncio

from camoufox.async_api import AsyncNewContext

from fastfox import fast_browser, read_server_meta


class FoxBrowser:

    def __init__(self, cm=None, browser=None, pw=None):
        self._cm = cm
        self._browser = browser
        self._pw = pw

    async def new_context(self, **kwargs):
        if self._pw is not None:
            proxy = kwargs.pop("proxy", None)
            kwargs.setdefault("no_viewport", True)
            return await AsyncNewContext(self._browser, proxy=proxy, **kwargs)
        return await self._browser.new_context(**kwargs)

    @property
    def contexts(self):
        return self._browser.contexts

    async def close(self):
        if self._pw is not None:
            try:
                await self._pw.stop()
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
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        try:
            browser = await asyncio.wait_for(
                pw.firefox.connect(meta["ws"]), timeout=5)
        except Exception as e:
            print(f"[FOX] connect fail ({type(e).__name__}); launch separately")
            try:
                await pw.stop()
            except Exception:
                pass
        else:
            return FoxBrowser(browser=browser, pw=pw)

    cm = fast_browser(headless=headless)
    browser = await cm.__aenter__()
    return FoxBrowser(cm=cm, browser=browser)
