"""What a task actually cost, from the usage both APIs report.

Every number here comes from a response, not an estimate: Claude reports
`usage` on each turn, Jev reports it on each `system_one` call. The prices are
the only assumption, and they are in one table you can correct.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True)
class Pricing:
    """US dollars per million tokens."""

    input: float
    output: float
    cache_read: float = 0.0
    cache_write: float = 0.0

    @classmethod
    def for_claude(cls, input_price: float, output_price: float) -> "Pricing":
        """Cache reads bill at 0.1x input, 5-minute writes at 1.25x."""
        return cls(input_price, output_price, input_price * 0.1, input_price * 1.25)


#: Correct these if prices move; nothing else in the file needs to change.
CLAUDE_PRICES: dict[str, Pricing] = {
    "claude-opus-5": Pricing.for_claude(5.00, 25.00),
    "claude-opus-4-8": Pricing.for_claude(5.00, 25.00),
    "claude-sonnet-5": Pricing.for_claude(2.00, 10.00),
    "claude-haiku-4-5": Pricing.for_claude(1.00, 5.00),
}
#: Jev returns typed decisions, so there is no output stream to bill.
JEV_PRICE = Pricing(input=0.042, output=0.0)

FALLBACK_CLAUDE = Pricing.for_claude(5.00, 25.00)


@dataclass
class Tally:
    """Token counts for one provider, as reported by it."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total_input(self) -> int:
        """`input_tokens` is the uncached remainder only -- the sum is the prompt."""
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    def cost(self, pricing: Pricing) -> float:
        return (
            self.input_tokens * pricing.input
            + self.output_tokens * pricing.output
            + self.cache_read_tokens * pricing.cache_read
            + self.cache_write_tokens * pricing.cache_write
        ) / 1_000_000

    def __add__(self, other: "Tally") -> "Tally":
        return Tally(
            self.calls + other.calls,
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
        )

    def __sub__(self, other: "Tally") -> "Tally":
        return Tally(
            self.calls - other.calls,
            self.input_tokens - other.input_tokens,
            self.output_tokens - other.output_tokens,
            self.cache_read_tokens - other.cache_read_tokens,
            self.cache_write_tokens - other.cache_write_tokens,
        )


def _int(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) else 0


class CostMeter:
    """Accumulates what has been spent, per provider."""

    def __init__(self, model: str) -> None:
        self.model = model
        self.claude = Tally()
        self.jev = Tally()
        self._pricing = CLAUDE_PRICES.get(model, FALLBACK_CLAUDE)
        self.model_priced = model in CLAUDE_PRICES

    def record_claude(self, usage: Any) -> None:
        self.claude = self.claude + Tally(
            calls=1,
            input_tokens=_int(getattr(usage, "input_tokens", 0)),
            output_tokens=_int(getattr(usage, "output_tokens", 0)),
            cache_read_tokens=_int(getattr(usage, "cache_read_input_tokens", 0)),
            cache_write_tokens=_int(getattr(usage, "cache_creation_input_tokens", 0)),
        )

    def record_jev(self, usage: Any) -> None:
        self.jev = self.jev + Tally(
            calls=1,
            input_tokens=_int(getattr(usage, "input_tokens", 0)),
            output_tokens=_int(getattr(usage, "output_tokens", 0)),
        )

    def snapshot(self) -> "CostMeter":
        """A frozen copy, for measuring one task out of a longer session."""
        copy = CostMeter(self.model)
        copy.claude, copy.jev = replace(self.claude), replace(self.jev)
        return copy

    def since(self, earlier: "CostMeter") -> "CostMeter":
        delta = CostMeter(self.model)
        delta.claude = self.claude - earlier.claude
        delta.jev = self.jev - earlier.jev
        return delta

    @property
    def claude_cost(self) -> float:
        return self.claude.cost(self._pricing)

    @property
    def jev_cost(self) -> float:
        return self.jev.cost(JEV_PRICE)

    @property
    def total(self) -> float:
        return self.claude_cost + self.jev_cost

    def cache_hit_rate(self) -> float:
        """Share of prompt tokens served from cache. Near zero means it broke."""
        total = self.claude.total_input
        return self.claude.cache_read_tokens / total if total else 0.0

    def one_line(self) -> str:
        return (
            f"${self.total:.4f}  "
            f"(Claude ${self.claude_cost:.4f} over {self.claude.calls} turns, "
            f"Jev ${self.jev_cost:.4f} over {self.jev.calls} calls, "
            f"{self.cache_hit_rate():.0%} of the prompt cached)"
        )

    def detail(self) -> str:
        lines = [
            f"Model: {self.model}"
            + ("" if self.model_priced else "  [no price on file -- billed as Opus 5]"),
            "",
            f"  Claude   {self.claude.calls:>4} turns   "
            f"{self.claude.total_input:>9,} in / {self.claude.output_tokens:>7,} out   "
            f"${self.claude_cost:.4f}",
            f"           of the input: {self.claude.cache_read_tokens:,} cached "
            f"({self.cache_hit_rate():.0%}), {self.claude.cache_write_tokens:,} written, "
            f"{self.claude.input_tokens:,} fresh",
            f"  Jev      {self.jev.calls:>4} calls   "
            f"{self.jev.total_input:>9,} in / {self.jev.output_tokens:>7,} out   "
            f"${self.jev_cost:.4f}",
            "",
            f"  TOTAL                                                     ${self.total:.4f}",
        ]
        if self.total and self.jev_cost:
            lines.append(f"\n  Jev is {self.claude_cost / self.jev_cost:,.0f}x cheaper here.")
        return "\n".join(lines)
