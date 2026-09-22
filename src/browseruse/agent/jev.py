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


#: Actions Jev may take on its own. Everything here is a pure selection: no
#: text has to be generated, which is precisely what a System One model cannot
#: do. Typing a search query is therefore always Claude's job.
AUTOPILOT_ACTIONS: dict[str, str] = {
    "click": "Click one element on the page -- a link, a button, a filter, a result.",
    "scroll": "Scroll further down the page; what is needed is below the fold.",
    "go_back": "Return to the previous page; this one was a dead end.",
    "ask_claude": "Anything else: typing text, choosing between real alternatives, "
    "reading and summarising, deciding the plan, or any step where the right move "
    "is not obvious from the page alone.",
}


@dataclass(frozen=True)
class Observation:
    """What Jev thinks about the page the agent is looking at."""

    page_state: str
    page_state_confidence: float
    goal_complete: float
    #: Probability the goal needs the page's prose, not just its controls.
    needs_page_text: float = 1.0
    #: Jev's own read of the next move, when it was asked for one.
    next_action: str = "ask_claude"
    next_action_confidence: float = 0.0
    next_element: int | None = None
    next_element_confidence: float = 0.0

    @property
    def blocked_on_human(self) -> bool:
        """Login walls and captchas are yours to solve, not the agent's."""
        return self.page_state in {"login_required", "captcha"}

    @property
    def wants_page_text(self) -> bool:
        """Withhold the prose only on a confident no -- a wrong drop costs quality."""
        return self.needs_page_text >= 0.25

    def autopilot_move(self, min_confidence: float) -> tuple[str, dict] | None:
        """The action Jev is confident enough to take without asking Claude.

        Everything has to line up: an ordinary page, a goal that is not already
        met, an action Jev can actually express, and confidence on both the
        action and -- where one is needed -- the element.
        """
        if self.page_state != "normal" or self.goal_complete >= 0.5:
            return None
        if self.next_action == "ask_claude" or self.next_action_confidence < min_confidence:
            return None
        if self.next_action == "scroll":
            return "scroll", {"direction": "down", "amount": 700}
        if self.next_action == "go_back":
            return "go_back", {}
        if self.next_action == "click":
            if self.next_element is None or self.next_element_confidence < min_confidence:
                return None
            return "click", {"index": self.next_element}
        return None


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

    def __init__(self, config: Config, meter: Any = None) -> None:
        self._config = config
        #: Optional CostMeter; every call reports its usage to it.
        self.meter = meter
        self._client = AsyncTypeSafeClient(
            api_key=config.typesafe_api_key,
            model=config.jev_model,
            timeout=8.0,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def observe(
        self, goal: str, snapshot: PageSnapshot, *, propose_next: bool = False
    ) -> Observation:
        """Everything Jev can tell us about this page, in one request.

        ``system_one`` answers a whole mapping of named questions at once, so
        asking what to do next and whether the prose is needed costs the same
        one call as classifying the page did.
        """
        state = {
            "goal": goal,
            "url": snapshot.url,
            "title": snapshot.title,
            "visible_text": snapshot.text[:2000],
            "elements": [el.describe() for el in snapshot.elements[:60]],
        }
        questions: dict[str, Any] = {
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
            "needs_page_text": Noul(
                instructions="Achieving this goal requires reading the page's written "
                "content, not just operating its buttons and links.",
                criteria={
                    "true": "The answer is in the page's text: prices, descriptions, "
                    "results, an article, a confirmation number.",
                    "false": "This is a navigation step -- the labels on the controls "
                    "are enough to decide what to press next.",
                },
            ),
        }
        labels = snapshot.choice_labels()
        if propose_next and labels:
            questions["next_action"] = Choice(
                instructions="What is the single obvious next move towards the goal? "
                "Choose ask_claude unless the move is unambiguous from this page alone.",
                criteria=dict(AUTOPILOT_ACTIONS),
            )
            questions["next_element"] = Choice(
                instructions="If the next move is a click, which element should be clicked?",
                criteria=labels,
            )

        result = await self._ask(state, questions)
        page_state = result.choices["page_state"]

        next_action, action_confidence = "ask_claude", 0.0
        next_element: int | None = None
        element_confidence = 0.0
        if "next_action" in result.choices:
            action_answer = result.choices["next_action"]
            next_action, action_confidence = action_answer.choice, action_answer.confidence
            element_answer = result.choices["next_element"]
            element_confidence = element_answer.confidence
            try:
                next_element = int(element_answer.choice)
            except ValueError:
                next_element = None

        return Observation(
            page_state=page_state.choice,
            page_state_confidence=page_state.confidence,
            goal_complete=result.nouls["goal_complete"].noul,
            needs_page_text=result.nouls["needs_page_text"].noul,
            next_action=next_action,
            next_action_confidence=action_confidence,
            next_element=next_element,
            next_element_confidence=element_confidence,
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
            result = await self._client.system_one(state=state, questions=questions)
        except TypeSafeError as exc:
            # TypeSafeError covers API errors, connection failures and timeouts.
            raise JevUnavailable(str(exc)) from exc
        if self.meter is not None:
            self.meter.record_jev(result.usage)
        return result
