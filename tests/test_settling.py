"""Waiting for the page to actually finish reacting.

Regression tests for two silent failures: a snapshot taken while a page was
still re-rendering described the page as it was *before* the click, and a
target="_blank" link left the agent staring at the old tab.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from browseruse.browser import actions
from tests.conftest import needs_browser

pytestmark = needs_browser

SPA = (
    "data:text/html,<body><div id=o></div>"
    "<button onclick=\"setTimeout(()=>{document.getElementById('o').innerHTML="
    "'<button>Result one</button>'},800)\">Show results</button></body>"
)
INERT = "data:text/html,<body><button onclick='void 0'>Inert</button></body>"
RESTLESS = (
    "data:text/html,<body><div id=x>0</div><button>Click</button>"
    "<script>setInterval(()=>{document.getElementById('x').textContent=Date.now()},50)</script>"
    "</body>"
)


async def test_a_render_that_lands_800ms_later_is_waited_for(live_session):
    await live_session.page.goto(SPA)
    snap = await live_session.snapshot()
    button = next(e for e in snap.elements if "Show results" in e.label)

    await actions.execute(live_session, snap, "click", {"index": button.index})
    after = await live_session.snapshot()

    labels = [e.label for e in after.elements]
    assert "Result one" in labels, "the snapshot was taken before the re-render landed"


async def test_a_page_that_never_settles_gives_up_instead_of_hanging(live_session):
    await live_session.page.goto(RESTLESS)

    started = time.monotonic()
    waited = await live_session.wait_until_settled(max_wait=1.5)

    assert 1.4 <= waited < 2.5, "a ticking page must hit the cap, not block forever"
    assert time.monotonic() - started < 3.0


async def test_an_action_that_changes_nothing_returns_within_the_grace(live_session):
    """The reliability wait must not become a fixed tax on every step."""
    await live_session.page.goto(INERT)
    snap = await live_session.snapshot()

    started = time.monotonic()
    await actions.execute(live_session, snap, "click", {"index": 0})
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, f"waited {elapsed:.2f}s for a click that did nothing"


async def test_mark_pending_distinguishes_new_changes_from_old(live_session):
    await live_session.page.goto(INERT)
    await live_session.page.evaluate("() => document.body.appendChild(document.createElement('i'))")
    await live_session.mark_pending()

    counted = await live_session.page.evaluate("() => window.__buMutations")
    assert counted == 0, "the counter must reset, or old churn reads as this action's effect"


# -- tabs the page opens for itself --------------------------------------

NEW_TAB = (Path(__file__).parent / "fixtures" / "newtab.html").as_uri()


async def test_a_target_blank_link_is_followed(live_session):
    await live_session.page.goto(NEW_TAB)
    snap = await live_session.snapshot()
    link = next(e for e in snap.elements if "Open in new tab" in e.label)

    result = await actions.execute(live_session, snap, "click", {"index": link.index})
    after = await live_session.snapshot()

    assert "Confirm booking" in [e.label for e in after.elements], "stayed on the old tab"
    assert "new tab opened" in result.message, "Claude must be told the tab changed"


async def test_no_spurious_tab_switch_on_an_ordinary_click(live_session):
    await live_session.page.goto(INERT)
    snap = await live_session.snapshot()

    result = await actions.execute(live_session, snap, "click", {"index": 0})

    assert "new tab" not in result.message
    assert await live_session.take_new_tab() is None
