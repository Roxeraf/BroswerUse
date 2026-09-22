"""DOM indexing, driven against a real browser."""

from __future__ import annotations

import pytest
from playwright.async_api import async_playwright

from browseruse.browser.dom import snapshot
from browseruse.config import MAX_INDEXED_ELEMENTS
from tests.conftest import FIXTURE_HTML, chromium_path, needs_browser

pytestmark = needs_browser


@pytest.fixture
async def page():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True, executable_path=chromium_path(), args=["--no-sandbox"]
        )
        page = await browser.new_page()
        await page.goto(FIXTURE_HTML.as_uri())
        yield page
        await browser.close()


async def test_indexes_the_things_you_can_actually_click(page):
    snap = await snapshot(page)
    labels = [element.label for element in snap.elements]

    assert any("Accept all" in label for label in labels)
    assert any("Your basket" in label for label in labels)
    assert any("Custom role button" in label for label in labels), "role=button counts"


async def test_skips_what_you_cannot_click(page):
    snap = await snapshot(page)
    labels = [element.label for element in snap.elements]

    assert not any("Hidden button" in label for label in labels), "display:none leaked in"
    assert not any("Disabled button" in label for label in labels), "disabled element leaked in"


async def test_labels_fall_back_through_aria_then_placeholder(page):
    snap = await snapshot(page)
    labels = [element.label for element in snap.elements]

    assert any("Run search" in label for label in labels), "aria-label unused"
    assert any("Search products" in label for label in labels), "placeholder unused"


async def test_offscreen_elements_are_listed_but_marked(page):
    snap = await snapshot(page)
    offscreen = [element for element in snap.elements if not element.in_viewport]

    assert any("Place your order" in element.label for element in offscreen)
    assert "(below the fold)" in next(
        element.describe() for element in offscreen if "Place your order" in element.label
    )


async def test_visible_text_excludes_hidden_text(page):
    snap = await snapshot(page)
    assert "Hidden button" not in snap.text
    assert "Some visible body text" in snap.text


async def test_indices_are_dense_and_within_jevs_choice_limit(page):
    snap = await snapshot(page)
    indices = [element.index for element in snap.elements]

    assert indices == list(range(len(indices))), "indices must be contiguous from 0"
    assert len(indices) <= MAX_INDEXED_ELEMENTS <= 255
    assert set(snap.choice_labels()) == {str(i) for i in indices}


async def test_every_index_resolves_back_to_a_live_element(page):
    """The index is only useful if an action can find the element again."""
    from browseruse.browser.dom import INDEX_ATTRIBUTE

    snap = await snapshot(page)
    for element in snap.elements:
        frame = snap.frame_for(element.index)
        assert frame is not None
        locator = frame.locator(f'[{INDEX_ATTRIBUTE}="{element.index}"]')
        assert await locator.count() == 1, f"index {element.index} does not resolve"


async def test_a_second_snapshot_does_not_leave_stale_markers(page):
    from browseruse.browser.dom import INDEX_ATTRIBUTE

    first = await snapshot(page)
    await page.evaluate("document.getElementById('cookie').remove()")
    second = await snapshot(page)

    assert len(second.elements) == len(first.elements) - 1
    marked = await page.locator(f"[{INDEX_ATTRIBUTE}]").count()
    assert marked == len(second.elements), "stale index attributes were left behind"
