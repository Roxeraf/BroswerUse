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


async def test_observe_asks_both_questions_in_one_request(advisor_and_log):
    advisor, sent = advisor_and_log
    observation = await advisor.observe("buy the shoes", make_snapshot())

    assert len(sent) == 1, "page state and done-check must share one call"
    assert set(sent[0]["questions"]) == {"page_state", "goal_complete"}
    assert observation.page_state in PAGE_STATES
    assert 0.0 <= observation.goal_complete <= 1.0
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
