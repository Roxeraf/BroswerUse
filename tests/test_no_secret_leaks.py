"""Nothing from a password, card or one-time-code field may reach a model.

These are regression tests for a real leak: the element index captured
`el.value` for every input, so an autofilled password went into both the Claude
prompt and the Jev state.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from browseruse.browser.dom import snapshot
from browseruse.safety import Decision, prescreen, redact_for_transmission
from tests.conftest import chromium_path, make_element, make_snapshot, needs_browser

LOGIN_HTML = Path(__file__).parent / "fixtures" / "login.html"

SECRETS = {
    "hunter2-my-real-password": "password",
    "4111111111111111": "card number",
    "737": "CVV",
}


@pytest.fixture
async def login_snapshot():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True, executable_path=chromium_path(), args=["--no-sandbox"]
        )
        page = await browser.new_page()
        await page.goto(LOGIN_HTML.as_uri())
        yield await snapshot(page)
        await browser.close()


@needs_browser
@pytest.mark.parametrize(("secret", "what"), list(SECRETS.items()))
async def test_secrets_never_appear_in_what_is_sent_to_claude(login_snapshot, secret, what):
    rendered = login_snapshot.render()
    assert secret not in rendered, f"{what} leaked into the Claude prompt"


@needs_browser
@pytest.mark.parametrize(("secret", "what"), list(SECRETS.items()))
async def test_secrets_never_appear_in_what_is_sent_to_jev(login_snapshot, secret, what):
    """Jev sees element descriptions and page text; neither may carry a secret."""
    blob = " ".join(
        [*(e.describe() for e in login_snapshot.elements),
         *login_snapshot.choice_labels().values(),
         login_snapshot.text]
    )
    assert secret not in blob, f"{what} leaked into the Jev state"


@needs_browser
async def test_sensitive_fields_are_still_visible_to_the_agent(login_snapshot):
    """Redaction must not blind the agent -- it needs to know the field is there."""
    sensitive = [e for e in login_snapshot.elements if e.sensitive]

    assert len(sensitive) == 3, "password, card and cvv should all be flagged"
    assert all(e.filled for e in sensitive)
    assert all("sensitive field, already filled" in e.describe() for e in sensitive)
    assert all(e.value == "" for e in sensitive)


@needs_browser
async def test_a_password_field_is_detected_by_its_type_not_just_its_name(login_snapshot):
    password = next(e for e in login_snapshot.elements if e.type == "password")
    assert password.sensitive


@needs_browser
async def test_ordinary_fields_are_not_redacted(login_snapshot):
    """Over-redacting would break the agent for normal forms."""
    email = next(e for e in login_snapshot.elements if e.type == "email")
    assert not email.sensitive
    assert "dominik@example.com" in email.describe()


# -- the pre-transmission refusal ---------------------------------------

def test_typing_into_a_sensitive_field_is_refused_before_anything_is_sent():
    field = make_element(0, "Password", tag="input", type="password", sensitive=True)
    refusal = prescreen("type_text", {"index": 0, "text": "hunter2"}, make_snapshot([field]))

    assert refusal is not None and refusal.decision is Decision.BLOCK


def test_a_one_time_code_in_the_text_is_refused_before_anything_is_sent():
    refusal = prescreen(
        "type_text", {"index": 0, "text": "my one-time code is 445566"}, make_snapshot()
    )
    assert refusal is not None and refusal.decision is Decision.BLOCK


def test_clicking_near_a_sensitive_field_is_not_refused():
    """Only typing into it is refused; the agent may still click Sign in."""
    field = make_element(0, "Sign in")
    assert prescreen("click", {"index": 0}, make_snapshot([field])) is None


def test_reads_are_never_prescreened():
    assert prescreen("scroll", {"direction": "down"}, make_snapshot()) is None
    assert prescreen("done", {"summary": "x"}, make_snapshot()) is None


def test_redaction_replaces_text_bound_for_a_sensitive_field():
    field = make_element(0, "Password", tag="input", type="password", sensitive=True)
    args = redact_for_transmission({"index": 0, "text": "hunter2"}, make_snapshot([field]))

    assert args["text"] == "[redacted: sensitive field]"


def test_redaction_leaves_ordinary_text_alone():
    field = make_element(0, "Search")
    args = redact_for_transmission({"index": 0, "text": "blue shoes"}, make_snapshot([field]))

    assert args["text"] == "blue shoes"
