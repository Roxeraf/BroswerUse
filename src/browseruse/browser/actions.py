"""The verbs the agent can use, and the code that carries them out."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from playwright.async_api import Error as PlaywrightError

from browseruse.browser.dom import INDEX_ATTRIBUTE, PageSnapshot
from browseruse.browser.session import BrowserSession


@dataclass(frozen=True)
class ActionResult:
    ok: bool
    message: str
    #: Set when the action produced content the model should read (e.g. extract).
    payload: str | None = None
    #: True when the loop should stop and report back to the user.
    finished: bool = False


class UnknownAction(ValueError):
    pass


#: Actions that only look at the page. They skip the risk gate entirely --
#: there is nothing to confirm about scrolling.
READ_ONLY_ACTIONS = frozenset(
    {"scroll", "wait", "extract_text", "list_tabs", "switch_tab", "screenshot"}
)


def _locator(snapshot: PageSnapshot, session: BrowserSession, index: int):
    """Resolve an element index back to a live Playwright locator."""
    frame = snapshot.frame_for(index)
    if frame is None:
        raise UnknownAction(f"No element [{index}] in the current snapshot.")
    return frame.locator(f'[{INDEX_ATTRIBUTE}="{index}"]').first


async def _settle(session: BrowserSession, timeout: float = 5.0) -> None:
    """Give the page a moment to react without hanging on chatty sockets."""
    try:
        await session.page.wait_for_load_state("domcontentloaded", timeout=timeout * 1000)
    except PlaywrightError:
        pass
    await asyncio.sleep(0.4)


async def execute(
    session: BrowserSession,
    snapshot: PageSnapshot,
    action: str,
    args: dict[str, Any],
) -> ActionResult:
    """Run one action. Never raises for ordinary page failures -- it reports them."""
    try:
        return await _dispatch(session, snapshot, action, args)
    except UnknownAction as exc:
        return ActionResult(False, str(exc))
    except PlaywrightError as exc:
        first_line = str(exc).split("\n", 1)[0]
        return ActionResult(False, f"{action} failed: {first_line}")
    except Exception as exc:  # noqa: BLE001 - surfaced to the model, not swallowed
        return ActionResult(False, f"{action} failed: {type(exc).__name__}: {exc}")


async def _dispatch(
    session: BrowserSession,
    snapshot: PageSnapshot,
    action: str,
    args: dict[str, Any],
) -> ActionResult:
    match action:
        case "navigate":
            url = str(args["url"])
            await session.goto(url)
            await _settle(session)
            return ActionResult(True, f"Opened {session.page.url}")

        case "go_back":
            await session.page.go_back(wait_until="domcontentloaded")
            await _settle(session)
            return ActionResult(True, f"Went back to {session.page.url}")

        case "click":
            index = int(args["index"])
            element = snapshot.element(index)
            locator = _locator(snapshot, session, index)
            await locator.scroll_into_view_if_needed(timeout=5000)
            await locator.click(timeout=10000)
            await _settle(session)
            label = f' "{element.label}"' if element and element.label else ""
            return ActionResult(True, f"Clicked [{index}]{label}. Now at {session.page.url}")

        case "type_text":
            index = int(args["index"])
            text = str(args["text"])
            locator = _locator(snapshot, session, index)
            await locator.scroll_into_view_if_needed(timeout=5000)
            await locator.click(timeout=10000)
            if args.get("clear_first", True):
                await locator.fill("", timeout=5000)
            await locator.type(text, delay=25)
            if args.get("press_enter", False):
                await locator.press("Enter")
                await _settle(session)
                return ActionResult(True, f"Typed into [{index}] and pressed Enter.")
            return ActionResult(True, f"Typed into [{index}].")

        case "select_option":
            index = int(args["index"])
            value = str(args["value"])
            locator = _locator(snapshot, session, index)
            await locator.select_option(label=value, timeout=5000)
            await _settle(session)
            return ActionResult(True, f"Selected {value!r} in [{index}].")

        case "press_key":
            key = str(args["key"])
            await session.page.keyboard.press(key)
            await _settle(session)
            return ActionResult(True, f"Pressed {key}.")

        case "scroll":
            direction = str(args.get("direction", "down"))
            amount = int(args.get("amount", 600))
            delta = amount if direction == "down" else -amount
            await session.page.mouse.wheel(0, delta)
            await asyncio.sleep(0.4)
            return ActionResult(True, f"Scrolled {direction} {abs(delta)}px.")

        case "scroll_to_text":
            text = str(args["text"])
            locator = session.page.get_by_text(text, exact=False).first
            await locator.scroll_into_view_if_needed(timeout=5000)
            return ActionResult(True, f"Scrolled to {text!r}.")

        case "wait":
            seconds = min(float(args.get("seconds", 2)), 15.0)
            await asyncio.sleep(seconds)
            return ActionResult(True, f"Waited {seconds:g}s.")

        case "extract_text":
            fresh = await session.snapshot()
            return ActionResult(
                True,
                f"Read {len(fresh.text)} characters from {fresh.url}.",
                payload=fresh.text,
            )

        case "new_tab":
            url = args.get("url")
            await session.new_tab(str(url) if url else None)
            await _settle(session)
            return ActionResult(True, f"Opened a new tab at {session.page.url}")

        case "switch_tab":
            page = await session.switch_tab(int(args["index"]))
            return ActionResult(True, f"Switched to {page.url}")

        case "list_tabs":
            infos = await session.tab_infos()
            listing = "\n".join(
                f"  [{t.index}]{' *' if t.active else '  '} {t.title} -- {t.url}" for t in infos
            )
            return ActionResult(True, f"{len(infos)} open tab(s).", payload=listing)

        case "done":
            return ActionResult(True, str(args.get("summary", "Task finished.")), finished=True)

        case _:
            raise UnknownAction(f"Unknown action {action!r}.")
