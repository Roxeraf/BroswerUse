"""The agent loop: Claude decides, Jev gates, the browser acts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import anthropic

from browseruse.agent.jev import JevAdvisor, JevUnavailable, Observation
from browseruse.agent.prompts import SYSTEM_PROMPT
from browseruse.agent.tools import BROWSER_TOOLS
from browseruse.browser import actions as browser_actions
from browseruse.browser.dom import PageSnapshot
from browseruse.browser.session import BrowserSession
from browseruse.config import Config
from browseruse.safety import Decision, Judgement, judge, prescreen, redact_for_transmission

#: Below this, Jev's element pick is not trusted over Claude's own index.
REPICK_CONFIDENCE = 0.55
#: Above this, Jev's "already done" reading is surfaced to Claude as a nudge.
DONE_CONFIDENCE = 0.85

#: What the user is asked when an action needs approval.
ApprovalFn = Callable[[Judgement, str, dict[str, Any], str], Awaitable[bool]]
#: Progress messages for whatever UI is attached.
ReporterFn = Callable[[str, str], None]


@dataclass
class StepRecord:
    action: str
    args: dict[str, Any]
    result: str
    risk: float | None = None
    approved: bool | None = None


@dataclass
class RunReport:
    summary: str
    steps: list[StepRecord] = field(default_factory=list)
    stopped_because: str = "done"


class BrowserAgent:
    """One conversation with Claude, driving one browser session."""

    def __init__(
        self,
        config: Config,
        session: BrowserSession,
        jev: JevAdvisor | None,
        *,
        approve: ApprovalFn,
        report: ReporterFn,
    ) -> None:
        self._config = config
        self._session = session
        self._jev = jev
        self._approve = approve
        self._report = report
        self._client = anthropic.AsyncAnthropic(api_key=config.anthropic_api_key)
        self._messages: list[dict[str, Any]] = []
        self.read_only_mode = False
        #: Mutable at runtime via the CLI's /risk command.
        self.risk_threshold = config.risk_threshold

    async def run(self, goal: str) -> RunReport:
        """Pursue ``goal`` until Claude calls done, or the step budget runs out."""
        snapshot = await self._session.snapshot()
        observation = await self._observe(goal, snapshot)

        if observation is not None and observation.blocked_on_human:
            return RunReport(
                summary=self._blocked_message(observation, snapshot),
                stopped_because="blocked",
            )

        self._messages.append(
            {
                "role": "user",
                "content": self._compose_turn(goal, snapshot, observation, first=True),
            }
        )

        steps: list[StepRecord] = []
        for _ in range(self._config.max_steps):
            response = await self._think()
            self._messages.append({"role": "assistant", "content": response.content})

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                text = self._text_of(response)
                return RunReport(text or "(no reply)", steps, stopped_because="end_turn")

            results: list[dict[str, Any]] = []
            finished: str | None = None

            for block in tool_uses:
                record, result_content, done_summary = await self._run_tool(
                    goal, snapshot, block.name, dict(block.input)
                )
                steps.append(record)
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_content,
                        **({"is_error": True} if record.approved is False else {}),
                    }
                )
                if done_summary is not None:
                    finished = done_summary
                    break

            if finished is not None:
                return RunReport(finished, steps, stopped_because="done")

            snapshot = await self._session.snapshot()
            observation = await self._observe(goal, snapshot)

            if observation is not None and observation.blocked_on_human:
                return RunReport(
                    self._blocked_message(observation, snapshot), steps, stopped_because="blocked"
                )

            # The tool results and the fresh page state go back in one user turn.
            self._messages.append(
                {"role": "user", "content": results + self._compose_turn(goal, snapshot, observation)}
            )

        return RunReport(
            f"Stopped after {self._config.max_steps} steps without finishing. "
            f"Currently at {snapshot.url}. Tell me how to continue.",
            steps,
            stopped_because="step_limit",
        )

    # -- Claude ----------------------------------------------------------

    async def _think(self):
        async with self._client.messages.stream(
            model=self._config.model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            tools=BROWSER_TOOLS,
            thinking={"type": "adaptive"},
            cache_control={"type": "ephemeral"},
            messages=self._messages,
        ) as stream:
            async for text in stream.text_stream:
                self._report("say", text)
            return await stream.get_final_message()

    @staticmethod
    def _text_of(response: Any) -> str:
        return "".join(b.text for b in response.content if b.type == "text").strip()

    # -- Jev -------------------------------------------------------------

    async def _observe(self, goal: str, snapshot: PageSnapshot) -> Observation | None:
        """Ask Jev about the page. A Jev outage degrades the loop, not ends it."""
        if self._jev is None:
            return None
        try:
            return await self._jev.observe(goal, snapshot)
        except JevUnavailable as exc:
            self._report("warn", f"Jev is unreachable ({exc}); confirming every action instead.")
            return None

    async def _resolve_index(
        self, goal: str, snapshot: PageSnapshot, args: dict[str, Any]
    ) -> tuple[dict[str, Any], str | None]:
        """Re-point a stale element index using Jev, if we can and should."""
        index = int(args["index"])
        description = str(args.get("target_description") or "").strip()
        if snapshot.element(index) is not None or not description or self._jev is None:
            return args, None
        try:
            picked, confidence = await self._jev.pick_element(goal, description, snapshot)
        except JevUnavailable:
            return args, None
        if picked is None or confidence < REPICK_CONFIDENCE:
            return args, None
        return {**args, "index": picked}, (
            f"index {index} no longer exists; Jev re-matched {description!r} "
            f"to [{picked}] at {confidence:.0%} confidence"
        )

    # -- one tool call ---------------------------------------------------

    async def _run_tool(
        self, goal: str, snapshot: PageSnapshot, action: str, args: dict[str, Any]
    ) -> tuple[StepRecord, str, str | None]:
        note: str | None = None
        if "index" in args:
            args, note = await self._resolve_index(goal, snapshot, args)
            if note:
                self._report("info", note)

        # Refuse outright before the action is described to any remote model:
        # scoring a request to type a password would transmit the password.
        refusal = prescreen(action, args, snapshot)
        if refusal is not None:
            self._report("block", refusal.reason)
            return (
                StepRecord(action, args, f"Blocked: {refusal.reason}", None, False),
                f"Blocked and not carried out. {refusal.reason}",
                None,
            )

        risk_score: float | None = None
        risk_explanation: str | None = None
        if action not in browser_actions.READ_ONLY_ACTIONS and action != "done":
            if self._jev is not None:
                try:
                    verdict = await self._jev.assess_risk(
                        goal, action, redact_for_transmission(args, snapshot), snapshot
                    )
                    risk_score = verdict.score
                    risk_explanation = verdict.explain()
                except JevUnavailable as exc:
                    self._report("warn", f"Jev could not score this action ({exc}).")

        verdict = judge(
            action=action,
            args=args,
            snapshot=snapshot,
            risk_score=risk_score,
            risk_explanation=risk_explanation,
            threshold=self.risk_threshold,
            read_only_mode=self.read_only_mode,
        )

        element = snapshot.element(int(args["index"])) if "index" in args else None
        target = element.describe() if element else ""

        if verdict.decision is Decision.BLOCK:
            record = StepRecord(action, args, f"Blocked: {verdict.reason}", risk_score, False)
            self._report("block", verdict.reason)
            return record, f"Blocked and not carried out. {verdict.reason}", None

        if verdict.decision is Decision.CONFIRM:
            approved = await self._approve(verdict, action, args, target)
            if not approved:
                record = StepRecord(action, args, "Declined by the user", risk_score, False)
                return (
                    record,
                    "The user declined this action. Do not retry it or work around it.",
                    None,
                )

        result = await browser_actions.execute(self._session, snapshot, action, args)
        message = result.message if not result.payload else f"{result.message}\n\n{result.payload}"
        if note:
            message = f"({note})\n{message}"

        record = StepRecord(
            action, args, result.message, risk_score, approved=True if verdict.needs_user else None
        )
        self._report("act" if result.ok else "warn", result.message)
        return record, message, result.message if result.finished else None

    # -- turn composition ------------------------------------------------

    def _compose_turn(
        self,
        goal: str,
        snapshot: PageSnapshot,
        observation: Observation | None,
        *,
        first: bool = False,
    ) -> list[dict[str, Any]]:
        parts: list[str] = []
        if first:
            parts.append(f"The user asked: {goal}")
        parts.append(snapshot.render())

        if observation is not None:
            hints: list[str] = []
            if observation.page_state != "normal" and observation.page_state_confidence >= 0.6:
                hints.append(
                    f"Page state: {observation.page_state} "
                    f"({observation.page_state_confidence:.0%} confidence)."
                )
            if observation.goal_complete >= DONE_CONFIDENCE:
                hints.append(
                    f"This page looks like the goal is already met "
                    f"({observation.goal_complete:.0%}). If it is, call done now."
                )
            if hints:
                parts.append("Jev's read of this page: " + " ".join(hints))

        return [{"type": "text", "text": "\n\n".join(parts)}]

    @staticmethod
    def _blocked_message(observation: Observation, snapshot: PageSnapshot) -> str:
        what = "a sign-in" if observation.page_state == "login_required" else "a bot check"
        return (
            f"{snapshot.url} is showing {what}, which is yours to complete, not mine. "
            f"Finish it in the browser window and tell me to carry on."
        )
