"""Jev: the fast typed decisions inside the loop.

Jev is a System One model -- it does not generate text, it answers typed
questions about a state with calibrated probabilities. That makes it the wrong
tool for "what should I do about this request" and exactly the right tool for
the four questions this loop asks on every single step:

* **How risky is this action?** (Score 0-3) -- drives the confirmation gate.
* **What kind of page am I on?** (Choice) -- cookie wall, login wall, captcha,
  error, or a normal page, so the loop handles the boring ones itself.
* **Is the goal already met?** (Noul) -- stop without another LLM round trip.
* **Which element did Claude mean?** (Choice over up to 255 labels) -- recovers
  from index drift when the page re-renders between snapshot and click.

The first three ride in one request; ``system_one`` answers a whole mapping of
named questions at once, so a full observation costs one call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul, Score, TypeSafeError

from browseruse.browser.dom import PageSnapshot
from browseruse.config import Config

#: Risk rubric. The index *is* the score, so index 0 is "harmless".
RISK_RUBRIC: list[str] = [
    "Reads or navigates only: opening a page, scrolling, going back, reading text. "
    "Nothing changes for anyone.",
    "Writes something reversible: typing into a field, applying a filter, adding an "
    "item to a cart, toggling a setting that can be toggled back.",
    "Commits something outward-facing or costly: submitting a payment or order, "
    "sending a message or email, posting publicly, agreeing to terms, signing up.",
    "Destroys or irreversibly changes something: deleting data, cancelling an "
    "account or booking, transferring money, revoking access.",
]

PAGE_STATES: dict[str, str] = {
    "normal": "An ordinary page showing the content the agent came for.",
    "cookie_banner": "The content is there, but a cookie or privacy consent dialog is "
    "covering it and must be dismissed first.",
    "login_required": "A sign-in, password, or two-factor prompt is blocking the content.",
    "captcha": "A captcha, bot check, or 'verify you are human' challenge is present.",
    "error_page": "An error: 404, 403, 500, rate limited, or the site says something went wrong.",
    "loading": "The page is still loading or shows only skeletons and spinners.",
}


@dataclass(frozen=True)
class Observation:
    """What Jev thinks about the page the agent is looking at."""

    page_state: str
    page_state_confidence: float
    goal_complete: float

    @property
    def blocked_on_human(self) -> bool:
        """Login walls and captchas are yours to solve, not the agent's."""
        return self.page_state in {"login_required", "captcha"}


@dataclass(frozen=True)
class RiskVerdict:
    score: float
    confidence: float
    rubric: dict[int, str]

    @property
    def band(self) -> str:
        return RISK_RUBRIC[min(int(round(self.score)), len(RISK_RUBRIC) - 1)]

    def explain(self) -> str:
        return f"risk {self.score:.1f}/3 (confidence {self.confidence:.0%}) -- {self.band}"


class JevUnavailable(RuntimeError):
    """Jev could not be reached. Callers decide whether that is fatal."""


class JevAdvisor:
    """Thin, typed wrapper over ``system_one`` for this agent's four questions."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._client = AsyncTypeSafeClient(
            api_key=config.typesafe_api_key,
            model=config.jev_model,
            timeout=8.0,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def observe(self, goal: str, snapshot: PageSnapshot) -> Observation:
        """Classify the page and check for completion in a single request."""
        state = {
            "goal": goal,
            "url": snapshot.url,
            "title": snapshot.title,
            "visible_text": snapshot.text[:2000],
            "elements": [el.describe() for el in snapshot.elements[:60]],
        }
        result = await self._ask(
            state,
            {
                "page_state": Choice(
                    instructions="What is the state of this page right now?",
                    criteria=dict(PAGE_STATES),
                ),
                "goal_complete": Noul(
                    instructions="The user's goal has already been achieved and is "
                    "visible on this page.",
                    criteria={
                        "true": "The page shows the finished result the goal asked for.",
                        "false": "More steps are still needed.",
                    },
                ),
            },
        )
        choice = result.choices["page_state"]
        return Observation(
            page_state=choice.choice,
            page_state_confidence=choice.confidence,
            goal_complete=result.nouls["goal_complete"].noul,
        )

    async def assess_risk(
        self,
        goal: str,
        action: str,
        args: dict[str, Any],
        snapshot: PageSnapshot,
    ) -> RiskVerdict:
        """Score one pending action against the risk rubric."""
        element = snapshot.element(int(args["index"])) if "index" in args else None
        state = {
            "goal": goal,
            "url": snapshot.url,
            "page_title": snapshot.title,
            "proposed_action": action,
            "action_arguments": {k: str(v)[:200] for k, v in args.items()},
            "target_element": element.describe() if element else None,
            "nearby_text": snapshot.text[:1200],
        }
        result = await self._ask(
            state,
            {
                "risk": Score(
                    instructions="How consequential is the proposed action for the user, "
                    "if it is carried out on this page right now?",
                    criteria=list(RISK_RUBRIC),
                )
            },
        )
        answer = result.scores["risk"]
        return RiskVerdict(
            score=answer.score, confidence=answer.confidence, rubric=dict(answer.legend)
        )

    async def pick_element(
        self, goal: str, description: str, snapshot: PageSnapshot
    ) -> tuple[int | None, float]:
        """Choose the element matching ``description``, as an index and confidence.

        The element list maps straight onto Choice's 255-label limit, which is
        why ``dom`` caps the index at 255 in the first place.
        """
        labels = snapshot.choice_labels()
        if not labels:
            return None, 0.0
        result = await self._ask(
            {
                "goal": goal,
                "looking_for": description,
                "url": snapshot.url,
                "page_title": snapshot.title,
            },
            {
                "element": Choice(
                    instructions=f"Which element on this page is {description!r}?",
                    criteria=labels,
                )
            },
        )
        answer = result.choices["element"]
        try:
            return int(answer.choice), answer.confidence
        except ValueError:
            return None, 0.0

    async def _ask(self, state: Any, questions: dict[str, Any]):
        try:
            return await self._client.system_one(state=state, questions=questions)
        except TypeSafeError as exc:
            # TypeSafeError covers API errors, connection failures and timeouts.
            raise JevUnavailable(str(exc)) from exc
