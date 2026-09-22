"""The gate that decides what runs without asking."""

from __future__ import annotations

import pytest

from browseruse.safety import Decision, judge
from tests.conftest import make_element, make_snapshot


def gate(action="click", args=None, snapshot=None, risk=0.2, threshold=1.5, read_only=False):
    return judge(
        action=action,
        args=args if args is not None else {"index": 0},
        snapshot=snapshot if snapshot is not None else make_snapshot(),
        risk_score=risk,
        risk_explanation=None,
        threshold=threshold,
        read_only_mode=read_only,
    )


@pytest.mark.parametrize("action", ["scroll", "wait", "extract_text", "list_tabs", "done"])
def test_read_only_actions_never_ask(action):
    assert gate(action=action, args={}, risk=None).decision is Decision.ALLOW


def test_low_risk_click_runs_unattended():
    assert gate(risk=0.2).decision is Decision.ALLOW


def test_score_at_threshold_asks():
    assert gate(risk=1.5, threshold=1.5).decision is Decision.CONFIRM
    assert gate(risk=1.49, threshold=1.5).decision is Decision.ALLOW


def test_missing_score_fails_closed():
    """A Jev outage must not read as 'harmless'."""
    assert gate(risk=None).decision is Decision.CONFIRM


@pytest.mark.parametrize(
    "label",
    ["Place your order", "Pay now", "Confirm booking", "Transfer funds", "Cancel subscription"],
)
def test_blocklist_overrides_a_low_score(label):
    snapshot = make_snapshot([make_element(0, label)])
    assert gate(snapshot=snapshot, risk=0.0).decision is Decision.CONFIRM


@pytest.mark.parametrize(
    "text",
    ["my 2FA code is 123456", "the one-time code", "seed phrase for the wallet"],
)
def test_credentials_are_refused_outright(text):
    verdict = gate(action="type_text", args={"index": 0, "text": text}, risk=0.0)
    assert verdict.decision is Decision.BLOCK


def test_read_only_mode_asks_about_every_write():
    assert gate(risk=0.0, read_only=True).decision is Decision.CONFIRM
    # ...but still lets reads through.
    assert gate(action="scroll", args={}, risk=0.0, read_only=True).decision is Decision.ALLOW


def test_blocklist_also_reads_the_page_title():
    snapshot = make_snapshot([make_element(0, "Continue")], title="Delete account")
    assert gate(snapshot=snapshot, risk=0.0).decision is Decision.CONFIRM


def test_a_checkout_page_title_is_enough_to_ask():
    """The page you are on counts, not just the button you are clicking."""
    snapshot = make_snapshot([make_element(0, "Continue")], title="Checkout")
    assert gate(snapshot=snapshot, risk=0.0).decision is Decision.CONFIRM
