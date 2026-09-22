"""The orchestration: who gets asked, what gets run, what gets refused."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from browseruse.agent.jev import JevUnavailable, Observation, RiskVerdict
from browseruse.agent.loop import BrowserAgent
from browseruse.config import Config
from tests.conftest import make_element, make_snapshot


# -- doubles -------------------------------------------------------------

@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class ToolUseBlock:
    name: str
    input: dict[str, Any]
    id: str = "tu_1"
    type: str = "tool_use"


@dataclass
class FakeMessage:
    content: list[Any]
    stop_reason: str = "tool_use"


class FakeStream:
    def __init__(self, message: FakeMessage) -> None:
        self._message = message

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    @property
    async def text_stream(self):  # pragma: no cover - replaced below
        raise NotImplementedError

    async def get_final_message(self):
        return self._message


class _TextStream:
    def __init__(self, chunks): self._chunks = chunks
    def __aiter__(self): self._it = iter(self._chunks); return self
    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class FakeClaude:
    """Returns a scripted sequence of assistant turns."""

    def __init__(self, turns: list[FakeMessage]) -> None:
        self._turns = list(turns)
        self.calls: list[dict[str, Any]] = []
        self.messages = self

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        message = self._turns.pop(0)
        stream = FakeStream(message)
        chunks = [b.text for b in message.content if b.type == "text"]
        type(stream).text_stream = property(lambda self, c=chunks: _TextStream(c))
        return stream


class FakeSession:
    def __init__(self, snapshots: list[Any]) -> None:
        self._snapshots = snapshots
        self.executed: list[tuple[str, dict]] = []

    async def snapshot(self):
        return self._snapshots[min(len(self.executed), len(self._snapshots) - 1)]


class FakeJev:
    def __init__(self, *, risk=0.2, page_state="normal", done=0.1, pick=(None, 0.0), fail=False):
        self.risk, self.page_state, self.done, self.pick, self.fail = risk, page_state, done, pick, fail
        self.risk_calls: list[str] = []

    async def observe(self, goal, snapshot):
        if self.fail:
            raise JevUnavailable("down")
        return Observation(self.page_state, 0.95, self.done)

    async def assess_risk(self, goal, action, args, snapshot):
        if self.fail:
            raise JevUnavailable("down")
        self.risk_calls.append(action)
        return RiskVerdict(self.risk, 0.9, {})

    async def pick_element(self, goal, description, snapshot):
        return self.pick


def build(monkeypatch, turns, jev, *, approve=True, snapshots=None, config=None):
    """An agent wired to doubles, plus the log of what it asked and did."""
    asked: list[tuple[str, dict]] = []
    executed: list[tuple[str, dict]] = []

    async def approver(verdict, action, args, target):
        asked.append((action, args))
        return approve

    snapshots = snapshots or [make_snapshot()]
    session = FakeSession(snapshots)
    agent = BrowserAgent(
        config or Config(anthropic_api_key="k"), session, jev,
        approve=approver, report=lambda kind, text: None,
    )
    agent._client = FakeClaude(turns)

    async def fake_execute(sess, snap, action, args):
        from browseruse.browser.actions import ActionResult
        executed.append((action, args))
        sess.executed.append((action, args))
        if action == "done":
            return ActionResult(True, args["summary"], finished=True)
        return ActionResult(True, f"did {action}")

    import browseruse.agent.loop as loop_module
    monkeypatch.setattr(loop_module.browser_actions, "execute", fake_execute)
    return agent, asked, executed


# -- tests ---------------------------------------------------------------

async def test_a_low_risk_action_runs_without_asking(monkeypatch):
    turns = [
        FakeMessage([ToolUseBlock("click", {"index": 0, "target_description": "Continue"})]),
        FakeMessage([ToolUseBlock("done", {"summary": "Clicked it."})]),
    ]
    agent, asked, executed = build(monkeypatch, turns, FakeJev(risk=0.2))
    report = await agent.run("click continue")

    assert asked == [], "a harmless click should not interrupt the user"
    assert [a for a, _ in executed] == ["click", "done"]
    assert report.summary == "Clicked it."
    assert report.stopped_because == "done"


async def test_a_high_risk_action_is_put_to_the_user(monkeypatch):
    turns = [
        FakeMessage([ToolUseBlock("click", {"index": 0, "target_description": "Pay"})]),
        FakeMessage([ToolUseBlock("done", {"summary": "Paid."})]),
    ]
    agent, asked, executed = build(monkeypatch, turns, FakeJev(risk=2.4))
    await agent.run("pay for it")

    assert [a for a, _ in asked] == ["click"]
    assert ("click", {"index": 0, "target_description": "Pay"}) in executed


async def test_a_declined_action_is_not_carried_out_and_claude_is_told_so(monkeypatch):
    turns = [
        FakeMessage([ToolUseBlock("click", {"index": 0, "target_description": "Pay"})]),
        FakeMessage([ToolUseBlock("done", {"summary": "Stopped as asked."})]),
    ]
    agent, asked, executed = build(monkeypatch, turns, FakeJev(risk=2.4), approve=False)
    await agent.run("pay for it")

    assert [a for a, _ in executed] == ["done"], "the declined click must not run"
    results = [
        block for message in agent._messages
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    assert results[0]["is_error"] is True
    assert "declined" in results[0]["content"].lower()


async def test_a_jev_outage_makes_every_write_ask(monkeypatch):
    turns = [
        FakeMessage([ToolUseBlock("click", {"index": 0, "target_description": "Continue"})]),
        FakeMessage([ToolUseBlock("done", {"summary": "ok"})]),
    ]
    agent, asked, executed = build(monkeypatch, turns, FakeJev(fail=True))
    await agent.run("click continue")

    assert [a for a, _ in asked] == ["click"], "no score must mean ask, not assume safe"


async def test_a_login_wall_hands_the_window_back_before_spending_a_claude_turn(monkeypatch):
    agent, asked, executed = build(monkeypatch, [], FakeJev(page_state="login_required"))
    report = await agent.run("read my inbox")

    assert report.stopped_because == "blocked"
    assert "sign-in" in report.summary
    assert executed == [] and agent._client.calls == []


async def test_a_stale_index_is_re_matched_by_jev(monkeypatch):
    live = make_snapshot([make_element(7, "Search")])  # Claude's index 3 is gone
    turns = [
        FakeMessage([ToolUseBlock("click", {"index": 3, "target_description": "the Search button"})]),
        FakeMessage([ToolUseBlock("done", {"summary": "ok"})]),
    ]
    agent, asked, executed = build(monkeypatch, turns, FakeJev(pick=(7, 0.93)), snapshots=[live])
    await agent.run("search")

    assert executed[0][1]["index"] == 7, "the action should have been re-pointed at [7]"


async def test_a_low_confidence_re_match_is_not_trusted(monkeypatch):
    live = make_snapshot([make_element(7, "Search")])
    turns = [
        FakeMessage([ToolUseBlock("click", {"index": 3, "target_description": "the Search button"})]),
        FakeMessage([ToolUseBlock("done", {"summary": "ok"})]),
    ]
    agent, asked, executed = build(monkeypatch, turns, FakeJev(pick=(7, 0.30)), snapshots=[live])
    await agent.run("search")

    assert executed[0][1]["index"] == 3, "a 30% guess must not override Claude"


async def test_read_only_actions_skip_the_risk_call_entirely(monkeypatch):
    jev = FakeJev()
    turns = [
        FakeMessage([ToolUseBlock("scroll", {"direction": "down"})]),
        FakeMessage([ToolUseBlock("done", {"summary": "ok"})]),
    ]
    agent, asked, executed = build(monkeypatch, turns, jev)
    await agent.run("scroll down")

    assert jev.risk_calls == [], "scrolling is not worth a risk call"
    assert asked == []


async def test_the_step_budget_is_enforced(monkeypatch):
    turns = [
        FakeMessage([ToolUseBlock("scroll", {"direction": "down"}, id=f"t{i}")])
        for i in range(10)
    ]
    agent, asked, executed = build(
        monkeypatch, turns, FakeJev(), config=Config(anthropic_api_key="k", max_steps=3)
    )
    report = await agent.run("scroll forever")

    assert report.stopped_because == "step_limit"
    assert len(report.steps) == 3


async def test_claude_is_nudged_when_jev_thinks_the_goal_is_already_met(monkeypatch):
    turns = [FakeMessage([ToolUseBlock("done", {"summary": "Already there."})])]
    agent, _, _ = build(monkeypatch, turns, FakeJev(done=0.97))
    await agent.run("open the basket")

    first_turn = agent._messages[0]["content"][0]["text"]
    assert "goal is already met" in first_turn
    assert "97%" in first_turn


async def test_a_credential_is_refused_without_jev_ever_seeing_it(monkeypatch):
    """The refusal must happen before the risk call, or scoring leaks the secret."""
    from browseruse.browser.dom import Element

    field = Element(index=0, tag="input", label="Password", frame_url="https://x/",
                    type="password", sensitive=True, filled=False)
    jev = FakeJev()
    turns = [
        FakeMessage([ToolUseBlock("type_text", {"index": 0, "target_description": "password box",
                                                "text": "hunter2"})]),
        FakeMessage([ToolUseBlock("done", {"summary": "Asked the user to sign in."})]),
    ]
    agent, asked, executed = build(monkeypatch, turns, jev, snapshots=[make_snapshot([field])])
    await agent.run("log me in")

    assert jev.risk_calls == [], "the secret was described to Jev before being refused"
    assert [a for a, _ in executed] == ["done"], "the typing must not have run"
    assert asked == [], "a refusal is not a confirmation prompt"


async def test_text_bound_for_a_sensitive_field_is_redacted_before_scoring(monkeypatch):
    """Belt and braces: if a sensitive field slips past prescreen, Jev still
    never receives the contents."""
    from browseruse.browser.dom import Element

    seen: list[dict] = []
    field = Element(index=0, tag="input", label="Card number", frame_url="https://x/",
                    sensitive=True)

    class RecordingJev(FakeJev):
        async def assess_risk(self, goal, action, args, snapshot):
            seen.append(args)
            return await super().assess_risk(goal, action, args, snapshot)

    from browseruse.safety import redact_for_transmission
    snap = make_snapshot([field])
    assert redact_for_transmission({"index": 0, "text": "4111111111111111"}, snap)["text"] == (
        "[redacted: sensitive field]"
    )
