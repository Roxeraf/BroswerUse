"""The cost meter, against the usage shapes both APIs actually return."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from browseruse.agent.cost import CLAUDE_PRICES, JEV_PRICE, CostMeter, Pricing


@dataclass
class ClaudeUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class JevUsage:
    input_tokens: int | None = 0
    output_tokens: int | None = 0


def test_opus_5_prices_match_the_published_rates():
    pricing = CLAUDE_PRICES["claude-opus-5"]
    assert (pricing.input, pricing.output) == (5.00, 25.00)
    assert pricing.cache_read == pytest.approx(0.50), "cache reads are 0.1x input"
    assert pricing.cache_write == pytest.approx(6.25), "5-minute writes are 1.25x input"


def test_a_plain_uncached_turn_costs_input_plus_output():
    meter = CostMeter("claude-opus-5")
    meter.record_claude(ClaudeUsage(input_tokens=1_000_000, output_tokens=1_000_000))

    assert meter.claude_cost == pytest.approx(30.00)


def test_cached_tokens_are_billed_at_their_own_rates():
    meter = CostMeter("claude-opus-5")
    meter.record_claude(
        ClaudeUsage(cache_read_input_tokens=1_000_000, cache_creation_input_tokens=1_000_000)
    )
    assert meter.claude_cost == pytest.approx(0.50 + 6.25)


def test_jev_output_is_free():
    """Jev returns typed decisions, so there is no output stream to bill."""
    assert JEV_PRICE.output == 0.0

    meter = CostMeter("claude-opus-5")
    meter.record_jev(JevUsage(input_tokens=1_000_000, output_tokens=1_000_000))
    assert meter.jev_cost == pytest.approx(0.042)


def test_total_input_sums_the_three_buckets():
    """`input_tokens` is the uncached remainder, not the whole prompt."""
    meter = CostMeter("claude-opus-5")
    meter.record_claude(
        ClaudeUsage(input_tokens=40, cache_read_input_tokens=1800, cache_creation_input_tokens=200)
    )
    assert meter.claude.total_input == 2040
    assert meter.cache_hit_rate() == pytest.approx(1800 / 2040)


def test_a_broken_cache_shows_up_as_a_zero_hit_rate():
    meter = CostMeter("claude-opus-5")
    meter.record_claude(ClaudeUsage(input_tokens=5000))
    assert meter.cache_hit_rate() == 0.0


def test_missing_usage_fields_are_treated_as_zero_not_a_crash():
    """Jev's public Usage type allows None."""
    meter = CostMeter("claude-opus-5")
    meter.record_jev(JevUsage(input_tokens=None, output_tokens=None))
    assert meter.jev_cost == 0.0
    assert meter.jev.calls == 1


def test_an_unknown_model_falls_back_and_says_so():
    meter = CostMeter("claude-something-unreleased")
    meter.record_claude(ClaudeUsage(input_tokens=1_000_000))

    assert meter.model_priced is False
    assert "no price on file" in meter.detail()
    assert meter.claude_cost == pytest.approx(5.00)


def test_a_cheaper_model_costs_less_for_the_same_work():
    usage = ClaudeUsage(input_tokens=1_000_000, output_tokens=1_000_000)
    opus, haiku = CostMeter("claude-opus-5"), CostMeter("claude-haiku-4-5")
    opus.record_claude(usage)
    haiku.record_claude(usage)

    assert haiku.claude_cost == pytest.approx(6.00)
    assert opus.claude_cost > haiku.claude_cost * 4


def test_since_measures_one_task_out_of_a_longer_session():
    meter = CostMeter("claude-opus-5")
    meter.record_claude(ClaudeUsage(output_tokens=1_000_000))
    before = meter.snapshot()
    meter.record_claude(ClaudeUsage(output_tokens=2_000_000))

    delta = meter.since(before)
    assert delta.claude.output_tokens == 2_000_000
    assert meter.claude.output_tokens == 3_000_000, "the snapshot must not alias the live tally"


def test_the_summary_lines_render():
    meter = CostMeter("claude-opus-5")
    meter.record_claude(ClaudeUsage(input_tokens=40, output_tokens=400, cache_read_input_tokens=1800))
    meter.record_jev(JevUsage(input_tokens=2000))

    assert "$" in meter.one_line() and "cached" in meter.one_line()
    assert "TOTAL" in meter.detail() and "cheaper" in meter.detail()
