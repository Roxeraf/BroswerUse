"""Runtime settings, resolved once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

load_dotenv()

BrowserName = Literal["brave", "chrome"]

#: Jev's Choice primitive accepts at most 255 labels, so the element index we
#: hand both models is capped here rather than at some arbitrary round number.
MAX_INDEXED_ELEMENTS = 255


def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _num(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    anthropic_api_key: str | None = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY"))
    typesafe_api_key: str | None = field(default_factory=lambda: os.getenv("TYPESAFE_API_KEY"))

    browser: BrowserName = "brave"
    cdp_port: int = 9222
    real_profile: bool = False
    headless: bool = False

    model: str = "claude-opus-5"
    jev_model: str = "jev-latest"

    #: Jev scores each action 0-3; anything at or above this pauses for a yes/no.
    risk_threshold: float = 1.5
    max_steps: int = 40

    state_dir: Path = field(default_factory=lambda: Path.home() / ".browseruse")

    @classmethod
    def from_env(cls) -> "Config":
        browser = os.getenv("BROWSERUSE_BROWSER", "brave").strip().lower()
        if browser not in {"brave", "chrome"}:
            raise ValueError(f"BROWSERUSE_BROWSER must be 'brave' or 'chrome', got {browser!r}")
        return cls(
            browser=browser,  # type: ignore[arg-type]
            cdp_port=int(_num("BROWSERUSE_CDP_PORT", 9222)),
            real_profile=_flag("BROWSERUSE_REAL_PROFILE"),
            headless=_flag("BROWSERUSE_HEADLESS"),
            model=os.getenv("BROWSERUSE_MODEL", "claude-opus-5"),
            jev_model=os.getenv("BROWSERUSE_JEV_MODEL", "jev-latest"),
            risk_threshold=_num("BROWSERUSE_RISK_THRESHOLD", 1.5),
            max_steps=int(_num("BROWSERUSE_MAX_STEPS", 40)),
        )

    @property
    def profile_dir(self) -> Path:
        """Where the dedicated automation profile lives (ignored with --real-profile)."""
        return self.state_dir / "profiles" / self.browser
