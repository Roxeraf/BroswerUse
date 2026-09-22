"""Deciding what the agent may do on its own.

Two independent checks, in this order:

1. A small hard-coded blocklist. These never run unattended no matter what any
   model scores them, because a mis-scored bank transfer is not recoverable.
2. Jev's risk score against the threshold in the config.

If Jev is unreachable the gate fails *closed* for anything that writes: a
missing risk score is not the same as a low one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from browseruse.browser.actions import READ_ONLY_ACTIONS
from browseruse.browser.dom import PageSnapshot


class Decision(Enum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    BLOCK = "block"


#: Matched against the target element's label and the page title. Deliberately
#: short -- this is a backstop, not the primary mechanism.
ALWAYS_CONFIRM = re.compile(
    r"\b(pay|payment|buy now|place (your )?order|checkout|confirm (purchase|booking|order)|"
    r"transfer|send money|withdraw|delete (account|everything)|close account|"
    r"cancel (booking|subscription|reservation)|unsubscribe|deactivate)\b",
    re.IGNORECASE,
)

#: Never automated, and never offered as a "remember my answer" either.
NEVER_AUTOMATE = re.compile(
    r"\b(seed phrase|private key|2fa|two[- ]factor|one[- ]time (code|password)|"
    r"verification code|social security)\b",
    re.IGNORECASE,
)


def prescreen(action: str, args: dict[str, Any], snapshot: PageSnapshot) -> Judgement | None:
    """The refusal that must happen before the action is described to anyone.

    Risk scoring sends the action and its arguments to Jev. For a credential or
    a one-time code that is already too late, so this check runs first and, when
    it fires, nothing about the action leaves the machine.
    """
    if action in READ_ONLY_ACTIONS or action == "done":
        return None

    element = snapshot.element(int(args["index"])) if "index" in args else None
    if element is not None and element.sensitive and action in {"type_text", "select_option"}:
        return Judgement(
            Decision.BLOCK,
            "That is a password, card or one-time-code field. Fill it in yourself -- "
            "I will wait and carry on afterwards.",
        )

    haystack = " ".join(
        filter(None, [element.describe() if element else "", snapshot.title, str(args.get("text", ""))])
    )
    if NEVER_AUTOMATE.search(haystack):
        return Judgement(
            Decision.BLOCK,
            "This involves a credential or one-time code. Do it yourself -- "
            "I will wait and carry on afterwards.",
        )
    return None


def redact_for_transmission(args: dict[str, Any], snapshot: PageSnapshot) -> dict[str, Any]:
    """The form of an action that is safe to describe to a remote model."""
    element = snapshot.element(int(args["index"])) if "index" in args else None
    if "text" not in args:
        return args
    if element is not None and element.sensitive:
        return {**args, "text": "[redacted: sensitive field]"}
    return args


@dataclass(frozen=True)
class Judgement:
    decision: Decision
    reason: str
    risk_score: float | None = None

    @property
    def needs_user(self) -> bool:
        return self.decision is not Decision.ALLOW


def judge(
    *,
    action: str,
    args: dict[str, Any],
    snapshot: PageSnapshot,
    risk_score: float | None,
    risk_explanation: str | None,
    threshold: float,
    read_only_mode: bool = False,
) -> Judgement:
    """Decide whether ``action`` runs on its own, needs a yes, or is refused."""
    if action in READ_ONLY_ACTIONS or action == "done":
        return Judgement(Decision.ALLOW, "read-only action", risk_score)

    element = snapshot.element(int(args["index"])) if "index" in args else None
    haystack = " ".join(
        filter(None, [element.describe() if element else "", snapshot.title, str(args.get("text", ""))])
    )

    if NEVER_AUTOMATE.search(haystack):
        return Judgement(
            Decision.BLOCK,
            "This involves a credential or one-time code. Do it yourself -- "
            "I will wait and carry on afterwards.",
            risk_score,
        )

    if read_only_mode:
        return Judgement(Decision.CONFIRM, "read-only mode is on", risk_score)

    if ALWAYS_CONFIRM.search(haystack):
        return Judgement(
            Decision.CONFIRM, "matches the always-confirm list", risk_score
        )

    if risk_score is None:
        return Judgement(
            Decision.CONFIRM,
            "Jev could not score this action, so it is not running unattended",
            None,
        )

    if risk_score >= threshold:
        return Judgement(
            Decision.CONFIRM, risk_explanation or f"risk {risk_score:.1f} >= {threshold}", risk_score
        )

    return Judgement(Decision.ALLOW, risk_explanation or "below the risk threshold", risk_score)
