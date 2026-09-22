"""An attached browser: tabs, navigation, and snapshots."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from browseruse.browser import dom
from browseruse.browser.launcher import LaunchedBrowser, launch
from browseruse.config import Config


#: Installed into every document. A snapshot taken while the page is still
#: re-rendering shows the *old* page, which is how an agent ends up deciding
#: its click did nothing. Recording the last mutation lets us wait for the
#: page to go quiet instead of guessing with a fixed sleep.
_MUTATION_WATCHER = """
(() => {
  if (window.__buWatching) return;
  window.__buWatching = true;
  window.__buLastMutation = Date.now();
  window.__buMutations = 0;
  const bump = () => { window.__buLastMutation = Date.now(); window.__buMutations += 1; };
  new MutationObserver(bump).observe(document, {
    subtree: true, childList: true, attributes: true, characterData: true,
  });
})()
"""


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
        #: Tabs the page opened itself, e.g. through a target="_blank" link.
        self._new_pages: list[Page] = []

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
        await self._context.add_init_script(_MUTATION_WATCHER)
        self._context.on("page", self._record_new_page)

        pages = [p for p in self._context.pages if not p.is_closed()]
        self._page = pages[-1] if pages else await self._context.new_page()
        # Pages that already existed missed the init script.
        for page in pages:
            try:
                await page.evaluate(_MUTATION_WATCHER)
            except Exception:
                pass

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
        self._new_pages.clear()

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

    def _record_new_page(self, page: Page) -> None:
        """Playwright wraps handlers, so this must be a real method, not a bound builtin."""
        self._new_pages.append(page)

    async def mark_pending(self) -> None:
        """Zero the mutation counter, so the next wait can tell old from new."""
        try:
            await self.page.evaluate(
                "() => { window.__buLastMutation = Date.now(); window.__buMutations = 0; }"
            )
        except Exception:
            pass

    async def wait_until_settled(
        self,
        *,
        quiet_seconds: float = 0.35,
        grace_seconds: float = 1.0,
        max_wait: float = 4.0,
    ) -> float:
        """Block until the page has finished reacting. Returns how long it took.

        Quiescence alone is not enough: a page that will re-render after an XHR
        is perfectly quiet in the meantime, and waiting for "no mutations" would
        return instantly with the pre-click DOM. So until the first mutation
        arrives we keep waiting up to ``grace_seconds``; after it, we wait for
        ``quiet_seconds`` of calm. A page that never settles -- a clock, a
        carousel, a long-poll -- hits ``max_wait`` and carries on rather than
        hanging the agent.
        """
        started = time.monotonic()
        while True:
            elapsed = time.monotonic() - started
            if elapsed >= max_wait:
                break
            try:
                state = await self.page.evaluate(
                    "() => ({"
                    " idle: window.__buLastMutation ? Date.now() - window.__buLastMutation : 1e9,"
                    " count: window.__buMutations || 0 })"
                )
            except Exception:
                break  # mid-navigation; the next snapshot will catch up
            if state["count"] == 0:
                if elapsed >= grace_seconds:
                    break  # the action genuinely changed nothing
            elif state["idle"] / 1000 >= quiet_seconds:
                break
            await asyncio.sleep(0.08)
        return time.monotonic() - started

    async def take_new_tab(self) -> Page | None:
        """Switch to a tab the page just opened for itself, if there was one.

        Clicking a ``target="_blank"`` link leaves the agent staring at the old
        page while the thing it asked for loads somewhere else -- a silent and
        very common way for a run to go nowhere.
        """
        fresh = [page for page in self._new_pages if not page.is_closed()]
        self._new_pages.clear()
        if not fresh:
            return None
        page = fresh[-1]
        self._page = page
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10_000)
            await page.bring_to_front()
        except Exception:
            pass
        return page

    async def snapshot(self) -> dom.PageSnapshot:
        return await dom.snapshot(self.page)
