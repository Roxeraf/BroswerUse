"""Replaying a procedure: matching, safety, and knowing when to give up."""

from __future__ import annotations

from typing import Any

import pytest

from browseruse.agent.jev import JevUnavailable, RiskVerdict
from browseruse.agent.replay import RecipeRunner
from browseruse.config import Config
from browseruse.recipes import Recipe, RecipeStep
from tests.conftest import make_element, make_snapshot


class FakeJev:
    def __init__(self, *, pick=(5, 0.95), risk=0.2, fail=False):
        self.pick, self.risk, self.fail = pick, risk, fail
        self.meter = None
        self.pick_calls = 0

    async def pick_element(self, goal, description, snapshot):
        if self.fail:
            raise JevUnavailable("down")
        self.pick_calls += 1
        return self.pick

    async def assess_risk(self, goal, action, args, snapshot):
        return RiskVerdict(self.risk, 0.9, {})


class FakeSession:
    def __init__(self, snapshot): self._snapshot = snapshot
    async def snapshot(self): return self._snapshot


def build(monkeypatch, recipe_steps, jev, *, approve=True, snapshot=None, config=None):
    asked: list[tuple[str, dict]] = []
    executed: list[tuple[str, dict]] = []

    async def approver(verdict, action, args, target):
        asked.append((action, args))
        return approve

    snapshot = snapshot or make_snapshot([make_element(5, "Notifications")])
    runner = RecipeRunner(
        config or Config(anthropic_api_key="k"), FakeSession(snapshot), jev,
        approve=approver, report=lambda kind, text: None,
    )

    async def fake_execute(session, snap, action, args):
        from browseruse.browser.actions import ActionResult
        executed.append((action, args))
        return ActionResult(True, f"did {action}")

    import browseruse.agent.replay as replay_module
    monkeypatch.setattr(replay_module.browser_actions, "execute", fake_execute)
    return runner, Recipe("r", "open my notifications", recipe_steps), asked, executed


STEP_CLICK = RecipeStep("click", {"target_description": "Notifications"},
                        target_description="Notifications")
STEP_NAV = RecipeStep("navigate", {"url": "https://example.test"})


async def test_a_matching_recipe_replays_with_no_claude_turns(monkeypatch):
    jev = FakeJev()
    runner, recipe, asked, executed = build(monkeypatch, [STEP_NAV, STEP_CLICK], jev)
    report = await runner.run(recipe)

    assert report.ok and report.completed == 2
    assert [a for a, _ in executed] == ["navigate", "click"]
    assert report.cost.claude.calls == 0, "replay must not call Claude at all"


async def test_the_remembered_intent_is_resolved_to_a_live_index(monkeypatch):
    jev = FakeJev(pick=(5, 0.95))
    runner, recipe, _, executed = build(monkeypatch, [STEP_CLICK], jev)
    await runner.run(recipe)

    assert executed[0][1]["index"] == 5, "the stored intent must become today's index"
    assert jev.pick_calls == 1


async def test_a_changed_page_stops_replay_instead_of_clicking_something_else(monkeypatch):
    """The whole danger of replay: confidently doing the wrong thing."""
    jev = FakeJev(pick=(2, 0.40))
    runner, recipe, _, executed = build(monkeypatch, [STEP_CLICK], jev)
    report = await runner.run(recipe)

    assert not report.ok and report.needs_claude
    assert "changed" in report.reason
    assert executed == [], "nothing may run once the match is doubtful"


async def test_a_step_that_needed_approval_asks_again_every_time(monkeypatch):
    """A recipe must never launder a one-off yes into standing permission."""
    step = RecipeStep("click", {"target_description": "Submit order"},
                      target_description="Submit order", needed_approval=True)
    jev = FakeJev(risk=0.0)  # scored harmless today
    runner, recipe, asked, executed = build(monkeypatch, [step], jev)
    await runner.run(recipe)

    assert [a for a, _ in asked] == ["click"], "the stored approval flag must force a prompt"


async def test_declining_a_replayed_step_stops_the_run(monkeypatch):
    step = RecipeStep("click", {"target_description": "Submit order"},
                      target_description="Submit order", needed_approval=True)
    runner, recipe, _, executed = build(monkeypatch, [step, STEP_NAV], FakeJev(), approve=False)
    report = await runner.run(recipe)

    assert not report.ok and "declined" in report.reason
    assert executed == []


async def test_a_step_that_became_risky_is_gated_even_if_it_was_not_before(monkeypatch):
    runner, recipe, asked, _ = build(monkeypatch, [STEP_CLICK], FakeJev(risk=2.6))
    await runner.run(recipe)

    assert [a for a, _ in asked] == ["click"], "replay runs the live risk score, not the old one"


async def test_credentials_are_refused_on_replay_too(monkeypatch):
    field = make_element(5, "Password", tag="input", type="password", sensitive=True)
    step = RecipeStep("type_text", {"text": "hunter2", "target_description": "password box"},
                      target_description="password box")
    runner, recipe, _, executed = build(monkeypatch, 
        [step], FakeJev(pick=(5, 0.99)), snapshot=make_snapshot([field])
    )
    report = await runner.run(recipe)

    assert not report.ok and executed == []


async def test_missing_parameters_are_reported_before_anything_runs(monkeypatch):
    step = RecipeStep("type_text", {"text": "flights to {{city}}"})
    runner, recipe, _, executed = build(monkeypatch, [step], FakeJev())
    report = await runner.run(recipe)

    assert not report.ok and "city" in report.reason
    assert executed == []


async def test_parameters_are_substituted_on_replay(monkeypatch):
    step = RecipeStep("type_text", {"text": "flights to {{city}}"})
    runner, recipe, _, executed = build(monkeypatch, [step], FakeJev())
    report = await runner.run(recipe, {"city": "Lisbon"})

    assert report.ok
    assert executed[0][1]["text"] == "flights to Lisbon"


async def test_replay_without_jev_says_so_rather_than_guessing(monkeypatch):
    runner, recipe, _, executed = build(monkeypatch, [STEP_CLICK], None)
    report = await runner.run(recipe)

    assert not report.ok and "Jev" in report.reason
    assert executed == []


async def test_a_jev_outage_mid_replay_stops_cleanly(monkeypatch):
    runner, recipe, _, executed = build(monkeypatch, [STEP_CLICK], FakeJev(fail=True))
    report = await runner.run(recipe)

    assert not report.ok and "unreachable" in report.reason


async def test_the_report_says_how_far_it_got(monkeypatch):
    jev = FakeJev(pick=(5, 0.95))
    runner, recipe, _, _ = build(monkeypatch, [STEP_NAV, STEP_NAV, STEP_CLICK], jev)
    jev.pick = (5, 0.10)  # the third step no longer matches
    report = await runner.run(recipe)

    assert report.completed == 2 and report.total == 3
