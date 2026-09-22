"""Actions, driven against a real page."""

from __future__ import annotations

import pytest
from playwright.async_api import async_playwright

from browseruse.browser import actions
from browseruse.browser.dom import snapshot
from tests.conftest import FIXTURE_HTML, chromium_path, needs_browser

pytestmark = needs_browser


class PageSession:
    """The slice of BrowserSession that actions actually use."""

    def __init__(self, page): self.page = page
    async def snapshot(self): return await snapshot(self.page)
    async def goto(self, url):
        if not url.startswith(("http://", "https://", "file://", "about:")):
            url = f"https://{url}"
        await self.page.goto(url, wait_until="domcontentloaded")


@pytest.fixture
async def session():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True, executable_path=chromium_path(), args=["--no-sandbox"]
        )
        page = await browser.new_page()
        await page.goto(FIXTURE_HTML.as_uri())
        yield PageSession(page)
        await browser.close()


def find(snap, needle):
    return next(e for e in snap.elements if needle in e.label)


async def test_click_resolves_an_index_to_the_right_element(session):
    snap = await session.snapshot()
    await session.page.evaluate(
        "document.getElementById('accept').addEventListener('click',"
        " () => { window.__clicked = true; })"
    )
    result = await actions.execute(session, snap, "click", {"index": find(snap, "Accept all").index})

    assert result.ok, result.message
    assert await session.page.evaluate("window.__clicked === true")


async def test_typing_lands_in_the_field(session):
    snap = await session.snapshot()
    result = await actions.execute(
        session, snap, "type_text",
        {"index": find(snap, "Search products").index, "text": "blue shoes", "press_enter": False},
    )

    assert result.ok, result.message
    assert await session.page.input_value("input[name=q]") == "blue shoes"


async def test_typing_clears_the_field_first_by_default(session):
    await session.page.fill("input[name=q]", "old text")
    snap = await session.snapshot()
    await actions.execute(
        session, snap, "type_text",
        {"index": find(snap, "Search products").index, "text": "new", "press_enter": False},
    )
    assert await session.page.input_value("input[name=q]") == "new"


async def test_select_option_picks_by_visible_label(session):
    snap = await session.snapshot()
    select = next(e for e in snap.elements if e.tag == "select")
    result = await actions.execute(session, snap, "select_option", {"index": select.index, "value": "Large"})

    assert result.ok, result.message
    assert await session.page.input_value("select") == "Large"


async def test_extract_text_returns_the_page_content(session):
    snap = await session.snapshot()
    result = await actions.execute(session, snap, "extract_text", {})

    assert result.ok and result.payload
    assert "Some visible body text" in result.payload


async def test_a_stale_index_is_reported_not_raised(session):
    snap = await session.snapshot()
    result = await actions.execute(session, snap, "click", {"index": 999})

    assert result.ok is False
    assert "No element [999]" in result.message


async def test_an_unknown_action_is_reported_not_raised(session):
    snap = await session.snapshot()
    result = await actions.execute(session, snap, "teleport", {})

    assert result.ok is False and "Unknown action" in result.message


async def test_a_click_on_a_vanished_element_fails_gracefully(session):
    snap = await session.snapshot()
    target = find(snap, "Accept all")
    await session.page.evaluate("document.getElementById('cookie').remove()")

    result = await actions.execute(session, snap, "click", {"index": target.index})
    assert result.ok is False, "clicking a removed element must not raise"
    assert "click failed" in result.message


async def test_done_finishes_the_run(session):
    snap = await session.snapshot()
    result = await actions.execute(session, snap, "done", {"summary": "All set."})

    assert result.ok and result.finished and result.message == "All set."
