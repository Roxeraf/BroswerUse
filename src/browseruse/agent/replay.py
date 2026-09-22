"""Running a learned procedure, without paying for a model to re-think it.

Replay resolves each remembered intent to a live element with Jev, runs it
through the same safety gate as a fresh run, and checks the result. The moment
anything does not line up -- the page changed, Jev is unsure, a step fails --
it stops and says why, so the caller can hand the goal back to Claude. A recipe
that silently does the wrong thing is worse than no recipe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from browseruse.agent.cost import CostMeter
from browseruse.agent.jev import JevAdvisor, JevUnavailable
from browseruse.browser import actions as browser_actions
from browseruse.browser.session import BrowserSession
from browseruse.config import Config
from browseruse.recipes import Recipe, RecipeStep
from browseruse.safety import Decision, judge, prescreen

#: Jev must be at least this sure an element matches the remembered intent.
MATCH_CONFIDENCE = 0.70


@dataclass
class ReplayReport:
    ok: bool
    reason: str
    completed: int = 0
    total: int = 0
    notes: list[str] = field(default_factory=list)
    cost: CostMeter | None = None

    @property
    def needs_claude(self) -> bool:
        """Replay stopped early, so the rest of the goal is still outstanding."""
        return not self.ok


class RecipeRunner:
    """Replays a recipe. Falls back rather than guessing."""

    def __init__(
        self,
        config: Config,
        session: BrowserSession,
        jev: JevAdvisor | None,
        *,
        approve: Any,
        report: Any,
        meter: CostMeter | None = None,
    ) -> None:
        self._config = config
        self._session = session
        self._jev = jev
        self._approve = approve
        self._report = report
        self.meter = meter or CostMeter(config.model)
        if jev is not None and jev.meter is None:
            jev.meter = self.meter
        self.read_only_mode = False
        self.risk_threshold = config.risk_threshold

    async def run(self, recipe: Recipe, parameters: dict[str, str] | None = None) -> ReplayReport:
        parameters = parameters or {}
        before = self.meter.snapshot()
        missing = recipe.placeholders() - set(parameters)
        if missing:
            return ReplayReport(
                False,
                f"This recipe needs {', '.join(sorted(missing))}. "
                f"Run it as: /run {recipe.slug} {' '.join(f'{m}=...' for m in sorted(missing))}",
                total=len(recipe.steps),
                cost=self.meter.since(before),
            )
        if self._jev is None:
            return ReplayReport(
                False,
                "Replay needs Jev to match remembered steps to the live page. "
                "Set TYPESAFE_API_KEY, or let Claude do the task directly.",
                total=len(recipe.steps),
                cost=self.meter.since(before),
            )

        notes: list[str] = []
        for position, step in enumerate(recipe.steps, start=1):
            outcome = await self._replay_step(recipe, step, parameters, position)
            if outcome is not None:
                return ReplayReport(
                    False, outcome, position - 1, len(recipe.steps), notes,
                    self.meter.since(before),
                )
            notes.append(f"{step.action}: {step.target_description or step.args}")

        return ReplayReport(
            True, "Replayed from memory.", len(recipe.steps), len(recipe.steps), notes,
            self.meter.since(before),
        )

    async def _replay_step(
        self, recipe: Recipe, step: RecipeStep, parameters: dict[str, str], position: int
    ) -> str | None:
        """Run one step. Returns None on success, or why it stopped."""
        snapshot = await self._session.snapshot()
        args = step.replay_args(parameters)

        if step.needs_element:
            try:
                index, confidence = await self._jev.pick_element(
                    recipe.goal, step.target_description or "", snapshot
                )
            except JevUnavailable as exc:
                return f"step {position}: Jev is unreachable ({exc})."
            if index is None or confidence < MATCH_CONFIDENCE:
                return (
                    f"step {position} ({step.target_description!r}): nothing on this page "
                    f"matches well enough ({confidence:.0%}). The site has probably changed."
                )
            args["index"] = index
            element = snapshot.element(index)
            self._report(
                "auto",
                f"{step.action} -> [{index}] {element.label if element else ''} "
                f"({confidence:.0%} match)",
            )

        refusal = prescreen(step.action, args, snapshot)
        if refusal is not None:
            return f"step {position}: {refusal.reason}"

        risk: float | None = None
        if step.action not in browser_actions.READ_ONLY_ACTIONS:
            try:
                risk = (await self._jev.assess_risk(recipe.goal, step.action, args, snapshot)).score
            except JevUnavailable as exc:
                return f"step {position}: Jev could not score this action ({exc})."

        verdict = judge(
            action=step.action, args=args, snapshot=snapshot, risk_score=risk,
            risk_explanation=None, threshold=self.risk_threshold,
            read_only_mode=self.read_only_mode,
        )
        # A recipe never launders an approval: a step that needed a yes when it
        # was learned needs one every single time it runs.
        if verdict.decision is Decision.BLOCK:
            return f"step {position}: {verdict.reason}"
        if verdict.needs_user or step.needed_approval:
            element = snapshot.element(args["index"]) if "index" in args else None
            approved = await self._approve(
                verdict, step.action, args, element.describe() if element else ""
            )
            if not approved:
                return f"step {position}: you declined it."

        result = await browser_actions.execute(self._session, snapshot, step.action, args)
        if not result.ok:
            return f"step {position}: {result.message}"
        return None
