"""An attached browser: tabs, navigation, and snapshots."""

from __future__ import annotations

from dataclasses import dataclass

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from browseruse.browser import dom
from browseruse.browser.launcher import LaunchedBrowser, launch
from browseruse.config import Config


@dataclass(frozen=True)
class TabInfo:
    index: int
    title: str
    url: str
    active: bool


class BrowserSession:
    """Owns the CDP connection to your Brave or Chrome."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._launched: LaunchedBrowser | None = None
        self._page: Page | None = None

    async def __aenter__(self) -> "BrowserSession":
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def start(self) -> None:
        self._launched = launch(self._config)
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.connect_over_cdp(
            self._launched.ws_endpoint
        )
        self._context = (
            self._browser.contexts[0]
            if self._browser.contexts
            else await self._browser.new_context()
        )
        pages = [p for p in self._context.pages if not p.is_closed()]
        self._page = pages[-1] if pages else await self._context.new_page()

    async def close(self) -> None:
        """Detach. The browser itself is left open unless we started it."""
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._playwright is not None:
            await self._playwright.stop()
        # We deliberately do not terminate a browser the user already had open;
        # `self._launched.process` is None in that case, so this is a no-op there.
        self._browser = self._playwright = self._context = self._page = None

    def shutdown_launched_browser(self) -> None:
        """Close the browser window, but only if this process opened it."""
        if self._launched is not None:
            self._launched.terminate()

    # -- pages -----------------------------------------------------------

    @property
    def page(self) -> Page:
        if self._page is None or self._page.is_closed():
            pages = [p for p in self._pages() if not p.is_closed()]
            if not pages:
                raise RuntimeError("The browser has no open tabs.")
            self._page = pages[-1]
        return self._page

    def _pages(self) -> list[Page]:
        return list(self._context.pages) if self._context else []

    async def tab_infos(self) -> list[TabInfo]:
        """Open tabs with their real titles."""
        infos: list[TabInfo] = []
        for i, p in enumerate(self._pages()):
            if p.is_closed():
                continue
            try:
                title = await p.title()
            except Exception:
                title = p.url
            infos.append(TabInfo(index=i, title=title, url=p.url, active=p is self._page))
        return infos

    async def switch_tab(self, index: int) -> Page:
        pages = [p for p in self._pages() if not p.is_closed()]
        if not 0 <= index < len(pages):
            raise IndexError(f"No tab {index}; there are {len(pages)}.")
        self._page = pages[index]
        await self._page.bring_to_front()
        return self._page

    async def new_tab(self, url: str | None = None) -> Page:
        if self._context is None:
            raise RuntimeError("Session is not started.")
        page = await self._context.new_page()
        self._page = page
        if url:
            await page.goto(url, wait_until="domcontentloaded")
        return page

    async def goto(self, url: str) -> None:
        if not url.startswith(("http://", "https://", "file://", "about:")):
            url = f"https://{url}"
        await self.page.goto(url, wait_until="domcontentloaded")

    async def snapshot(self) -> dom.PageSnapshot:
        return await dom.snapshot(self.page)
