"""Jev request shapes and answer handling, against a mocked API."""

from __future__ import annotations

import json

import httpx2
import pytest

from browseruse.agent.jev import JevAdvisor, JevUnavailable, PAGE_STATES, RISK_RUBRIC
from browseruse.config import Config, MAX_INDEXED_ELEMENTS
from tests.conftest import make_element, make_snapshot


def _answer_for(question: dict) -> dict:
    """A plausible answer in the wire shape the SDK expects."""
    if question["type"] == "choice":
        criteria = question["criteria"]
        first = next(iter(criteria))
        return {
            "type": "choice",
            "choice": first,
            "confidence": 0.91,
            "probabilities": {k: 1.0 / len(criteria) for k in criteria},
        }
    if question["type"] == "noul":
        return {"type": "noul", "noul": 0.93}
    legend = {str(i): c for i, c in enumerate(question["criteria"])}
    return {
        "type": "score",
        "score": 2.1,
        "confidence": 0.88,
        "legend": legend,
        "probabilities": {k: 1.0 / len(legend) for k in legend},
    }


@pytest.fixture
def advisor_and_log():
    """A JevAdvisor whose HTTP calls are captured instead of sent."""
    sent: list[dict] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        sent.append(body)
        answers = {n: _answer_for(q) for n, q in body["questions"].items()}
        return httpx2.Response(
            200,
            json={
                "model": "jev-latest",
                "answers": answers,
                "usage": {"input_tokens": 120, "output_tokens": 0},
            },
        )

    advisor = JevAdvisor(Config(typesafe_api_key="test-key"))
    advisor._client._http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return advisor, sent


async def test_observe_asks_every_question_in_one_request(advisor_and_log):
    advisor, sent = advisor_and_log
    observation = await advisor.observe("buy the shoes", make_snapshot())

    assert len(sent) == 1, "one call must answer the whole mapping"
    assert set(sent[0]["questions"]) == {"page_state", "goal_complete", "needs_page_text"}
    assert observation.page_state in PAGE_STATES
    assert 0.0 <= observation.goal_complete <= 1.0
    await advisor.aclose()


async def test_proposing_the_next_move_costs_no_extra_request(advisor_and_log):
    """The whole saving rests on this: asking what to do next is free."""
    advisor, sent = advisor_and_log
    elements = [make_element(i, f"Button {i}") for i in range(10)]
    await advisor.observe("buy the shoes", make_snapshot(elements), propose_next=True)

    assert len(sent) == 1, "the proposal must ride along, not cost a second call"
    assert set(sent[0]["questions"]) == {
        "page_state", "goal_complete", "needs_page_text", "next_action", "next_element",
    }
    await advisor.aclose()


async def test_no_proposal_is_asked_for_on_an_empty_page(advisor_and_log):
    advisor, sent = advisor_and_log
    await advisor.observe("x", make_snapshot([]), propose_next=True)

    assert "next_element" not in sent[0]["questions"]
    await advisor.aclose()


async def test_login_and_captcha_are_flagged_as_the_users_problem(advisor_and_log):
    advisor, _ = advisor_and_log
    from browseruse.agent.jev import Observation

    assert Observation("login_required", 0.9, 0.0).blocked_on_human
    assert Observation("captcha", 0.9, 0.0).blocked_on_human
    assert not Observation("cookie_banner", 0.9, 0.0).blocked_on_human
    assert not Observation("normal", 0.9, 0.0).blocked_on_human
    await advisor.aclose()


async def test_risk_uses_the_rubric_as_an_ordered_list(advisor_and_log):
    advisor, sent = advisor_and_log
    verdict = await advisor.assess_risk("buy the shoes", "click", {"index": 0}, make_snapshot())

    question = sent[0]["questions"]["risk"]
    assert question["type"] == "score"
    assert question["criteria"] == RISK_RUBRIC, "score criteria are ordered, index == score"
    assert verdict.band == RISK_RUBRIC[2]  # the mock answers 2.1
    assert "risk 2.1/3" in verdict.explain()
    await advisor.aclose()


async def test_element_choice_never_exceeds_jevs_255_label_limit(advisor_and_log):
    advisor, sent = advisor_and_log
    elements = [make_element(i, f"Button {i}") for i in range(MAX_INDEXED_ELEMENTS)]
    index, confidence = await advisor.pick_element(
        "buy the shoes", "the Pay button", make_snapshot(elements)
    )

    criteria = sent[0]["questions"]["element"]["criteria"]
    assert len(criteria) == MAX_INDEXED_ELEMENTS <= 255
    assert all(key.isdigit() for key in criteria), "labels must round-trip to element indices"
    assert index == 0 and confidence == pytest.approx(0.91)
    await advisor.aclose()


async def test_pick_element_on_an_empty_page_is_not_an_error(advisor_and_log):
    advisor, sent = advisor_and_log
    index, confidence = await advisor.pick_element("x", "anything", make_snapshot([]))
    assert (index, confidence) == (None, 0.0)
    assert sent == [], "no point asking Jev to choose from nothing"
    await advisor.aclose()


async def test_an_api_failure_surfaces_as_jev_unavailable():
    advisor = JevAdvisor(Config(typesafe_api_key="test-key"))
    advisor._client._http_client = httpx2.AsyncClient(
        transport=httpx2.MockTransport(lambda r: httpx2.Response(503, json={"error": "down"}))
    )
    with pytest.raises(JevUnavailable):
        await advisor.observe("x", make_snapshot())
    await advisor.aclose()


# -- what Jev is allowed to do on its own --------------------------------

def observation(**kwargs):
    from browseruse.agent.jev import Observation

    base = dict(page_state="normal", page_state_confidence=0.95, goal_complete=0.1,
                needs_page_text=1.0, next_action="click", next_action_confidence=0.95,
                next_element=3, next_element_confidence=0.95)
    return Observation(**{**base, **kwargs})


def test_a_confident_click_is_taken_alone():
    assert observation().autopilot_move(0.80) == ("click", {"index": 3})


def test_scroll_and_go_back_need_no_element():
    scroll = observation(next_action="scroll", next_element=None).autopilot_move(0.80)
    assert scroll is not None and scroll[0] == "scroll"
    assert observation(next_action="go_back").autopilot_move(0.80) == ("go_back", {})


@pytest.mark.parametrize(
    "override",
    [
        {"next_action": "ask_claude"},
        {"next_action_confidence": 0.60},
        {"next_element_confidence": 0.60},
        {"next_element": None},
        {"page_state": "cookie_banner"},
        {"page_state": "login_required"},
        {"goal_complete": 0.9},
    ],
)
def test_anything_less_than_certain_goes_to_claude(override):
    assert observation(**override).autopilot_move(0.80) is None


def test_typing_is_never_taken_alone():
    """Jev cannot generate text, so a typed string is always Claude's."""
    from browseruse.agent.jev import AUTOPILOT_ACTIONS

    assert "type_text" not in AUTOPILOT_ACTIONS
    assert observation(next_action="type_text").autopilot_move(0.80) is None


def test_prose_is_withheld_only_on_a_confident_no():
    assert observation(needs_page_text=0.9).wants_page_text
    assert observation(needs_page_text=0.4).wants_page_text, "a maybe keeps the text"
    assert not observation(needs_page_text=0.1).wants_page_text
