"""What Lexi does when the Anthropic key stops working.

This is not hypothetical: on 2026-08-18 the production key hit "credit balance
too low", so every model call raised. The key was still PRESENT, which matters —
code that guards on `settings.llm_api_key` being set takes the model path and
then fails inside it, rather than taking the no-model path deliberately.

The design intent is to degrade rather than break: triage falls back to rules,
the plan builder falls back to defaults, and composition falls back to the
slot-derived template. These pin that intent, because the failure is otherwise
invisible — nothing tells Kory he is getting the degraded version.

What is LOST when the model is down (and is expected to be):
  - draft prose reads as the template rather than a written reply;
  - the plan layer stops resolving unusual phrasings, leaving the rule window.
What must NOT be lost:
  - a scheduling ask still produces real, calendar-checked times;
  - nothing raises into Teams as an unexplainable ToolError.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

MT = ZoneInfo("America/Denver")
FREE = {"status": "available", "horizon_days": 45, "busy_events": []}


class _NoCredit(Exception):
    """Stands in for anthropic.BadRequestError: credit balance too low."""


@pytest.fixture
def model_is_down():
    """Every model call raises, but the key is still configured — exactly the
    production condition, and NOT the same as having no key at all."""
    def _raise(*_a, **_k):
        raise _NoCredit("Your credit balance is too low to access the Anthropic API.")

    # Patch the call funnel, not get_hermes_client: five modules bind that name
    # at import time, so a definition-site patch never reaches them.
    with patch("app.llm.hermes_client._Completions.create", side_effect=_raise):
        yield


def _next_weekday(offset: int = 10) -> date:
    day = date.today() + timedelta(days=offset)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def test_a_scheduling_ask_still_produces_real_times(model_is_down):
    """The engine is deterministic. Losing the model must not lose the offer."""
    from app.scheduling.schedule_from_context import schedule_from_context

    target = _next_weekday()
    with patch(
        "app.scheduling.calendar_context.load_scheduling_calendar_context", return_value=FREE
    ):
        result = schedule_from_context(
            subject="[TEST] Intro call",
            body=f"Could we meet the week of {target:%B} {target.day}?",
            intent="referral_or_intro",
            sender_email="dana@example.com",
            calendar_context=FREE,
        )
    assert result.ok, result.failure_message
    assert result.slots, "no times were offered with the model unavailable"
    for slot in result.slots:
        assert datetime.fromisoformat(slot["start"]).weekday() < 5, (
            "a weekend slot was offered — the engine's rules must hold "
            "regardless of the model"
        )


def test_the_draft_falls_back_to_the_template_rather_than_failing(model_is_down):
    """Prose quality is what is lost. An email must still be produced."""
    from app.scheduling.hermes_compose import compose_offer_email_with_hermes

    target = _next_weekday()
    slots = [{
        "start": datetime(target.year, target.month, target.day, 9, 0, tzinfo=MT).isoformat(),
        "end": datetime(target.year, target.month, target.day, 9, 30, tzinfo=MT).isoformat(),
    }]
    with (
        patch("app.scheduling.hermes_compose._fetch_message_headers", return_value=None),
        patch("app.scheduling.hermes_compose._load_thread_context", return_value=""),
        patch("app.scheduling.hermes_compose._thread_sender_name", return_value="Dana"),
        patch("app.scheduling.hermes_compose._conversation_id_for_thread", return_value=""),
    ):
        draft, source = compose_offer_email_with_hermes(
            proposal_sender="Dana <dana@example.com>",
            proposal_subject="Intro call",
            proposal_body="Can we find 30 minutes?",
            thread_id="t-degraded",
            slots=slots,
            voice_mode="lexi",
            stored_recipient_timezone="America/Denver",
            intent="referral_or_intro",
        )
    assert source == "template_fallback"
    assert "Dana" in draft
    assert target.strftime("%A") in draft, "the offered time is missing from the draft"

    # And the degraded draft must still satisfy the send gate.
    from app.scheduling.draft_slot_sync import draft_matches_slots

    ok, mismatch = draft_matches_slots(draft_body=draft, proposed_slots=slots)
    assert ok, mismatch


def test_triage_falls_back_to_rules_instead_of_dropping_the_email(model_is_down):
    """The front door. If triage raised, inbound mail would stop being processed."""
    from app.agents.triage_agent import _fallback_triage

    triage = _fallback_triage(
        "NoCredit: credit balance too low",
        subject="Quick call next week?",
        body="Would love to find 30 minutes with Kory.",
    )
    assert triage.intent, "triage produced no intent, so nothing would be staged"
    assert 0.0 <= triage.confidence_score <= 1.0


def test_the_plan_layer_still_returns_a_usable_window(model_is_down):
    """It should fall back to the rule window, not raise."""
    from app.scheduling.scheduling_plan import build_scheduling_plan

    plan = build_scheduling_plan(
        subject="Intro call",
        body="Sometime in the next couple of weeks would be great.",
        intent="referral_or_intro",
    )
    assert plan is not None


def test_no_scheduling_tool_raises_into_teams_with_the_model_down(model_is_down):
    """A raise reaches Kory as an unexplainable ToolError. A degraded answer he
    can read is always better."""
    from tests.teams_parity import call_tool

    for name, kwargs in [
        ("lexi_find_slots", {"subject": "Intro", "body": "Can we meet next week?",
                             "intent": "referral_or_intro", "meeting_format": "",
                             "sender_email": "dana@example.com"}),
        ("lexi_preview_schedule", {"subject": "Intro", "body": "Can we meet next week?",
                                   "sender_email": "dana@example.com",
                                   "intent": "referral_or_intro"}),
        ("lexi_handle_teams_command", {"text": "pending"}),
    ]:
        assert call_tool(name, **kwargs) is not None, f"{name} returned nothing"
