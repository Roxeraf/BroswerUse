from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from browseruse.browser.dom import Element, PageSnapshot

FIXTURE_HTML = Path(__file__).parent / "fixtures" / "shop.html"


def chromium_path() -> str | None:
    """A Chromium we can drive, if one is installed."""
    bundled = os.getenv("BROWSERUSE_TEST_CHROMIUM")
    if bundled and Path(bundled).exists():
        return bundled
    for candidate in Path("/opt/pw-browsers").glob("chromium-*/chrome-linux/chrome"):
        return str(candidate)
    for name in ("google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return None


needs_browser = pytest.mark.skipif(
    chromium_path() is None, reason="no Chromium available to drive"
)


def make_element(index: int, label: str, **kwargs) -> Element:
    return Element(
        index=index, tag=kwargs.pop("tag", "button"), label=label,
        frame_url="https://example.test/", **kwargs
    )


def make_snapshot(elements: list[Element] | None = None, **kwargs) -> PageSnapshot:
    return PageSnapshot(
        url=kwargs.pop("url", "https://example.test/products"),
        title=kwargs.pop("title", "Product page"),
        elements=elements if elements is not None else [make_element(0, "Continue")],
        text=kwargs.pop("text", "Total EUR 42.00"),
        **kwargs,
    )
