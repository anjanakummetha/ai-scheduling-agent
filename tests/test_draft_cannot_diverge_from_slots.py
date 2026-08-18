"""A composed draft may never offer a time the engine did not stage.

The engine is pinned never to propose a weekend, even with two weeks fully
booked, and the send gate refuses a draft whose times diverge from the staged
slots. So a divergent draft always failed SAFE — but it failed LATE, as a
refusal on a draft that had already been written, staged and shown to Kory. In
the live end-to-end runs that was the most common single failure.

The fix is that composition simply cannot emit one. The check is the send
gate's own draft_matches_slots — the same function, not a second implementation
of the same idea, because two implementations of "do these agree" drifting apart
is how the whole class of defect started.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from app.scheduling.hermes_compose import compose_offer_email_with_hermes

MT = ZoneInfo("America/Denver")


def _weekday_slots(count: int = 2) -> list[dict[str, str]]:
    """Two staged weekday slots, well clear of today."""
    day = date.today() + timedelta(days=14)
    while day.weekday() >= 5:            # never stage a weekend
        day += timedelta(days=1)
    out = []
    for index in range(count):
        d = day + timedelta(days=index)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append(
            {
                "start": datetime(d.year, d.month, d.day, 9 + index, 0, tzinfo=MT).isoformat(),
                "end": datetime(d.year, d.month, d.day, 9 + index, 30, tzinfo=MT).isoformat(),
            }
        )
    return out


def _next_saturday() -> date:
    d = date.today() + timedelta(days=14)
    while d.weekday() != 5:
        d += timedelta(days=1)
    return d


@contextmanager
def _model_available():
    """Force the "an LLM is configured" branch regardless of the developer's env.

    settings is a frozen dataclass shared by reference across modules, so this
    goes through object.__setattr__ like the `live_writes` fixture. Without it
    the test reads whatever key happens to be in .env and silently exercises a
    different branch on a machine that has none.
    """
    from app.config import settings

    previous = settings.llm_api_key
    object.__setattr__(settings, "llm_api_key", "test-key-not-used")
    try:
        yield
    finally:
        object.__setattr__(settings, "llm_api_key", previous)


def _compose(model_draft: str, slots: list[dict[str, str]]) -> tuple[str, str]:
    with (
        _model_available(),
        patch("app.scheduling.hermes_compose._hermes_offer_compose", return_value=model_draft),
        patch("app.scheduling.hermes_compose._fetch_message_headers", return_value=None),
        patch("app.scheduling.hermes_compose._load_thread_context", return_value=""),
        patch("app.scheduling.hermes_compose._thread_sender_name", return_value="Dana"),
        patch("app.scheduling.hermes_compose._conversation_id_for_thread", return_value=""),
    ):
        return compose_offer_email_with_hermes(
            proposal_sender="Dana <dana@example.com>",
            proposal_subject="Quick intro call",
            proposal_body="Can we find 30 minutes in the next couple of weeks?",
            thread_id="t-compose",
            slots=slots,
            voice_mode="lexi",
            stored_recipient_timezone="America/Denver",
            intent="referral_or_intro",
        )


def test_a_clean_draft_from_the_model_is_kept():
    slots = _weekday_slots()
    block = "\n".join(
        f"• {datetime.fromisoformat(s['start']).strftime('%A, %B %-d at %-I:%M %p')}"
        for s in slots
    )
    draft, source = _compose(f"Hi Dana,\n\nA couple of options:\n\n{block}\n\nLet's Win,\nLexi", slots)
    assert source == "hermes", "a draft that agrees with the slots must survive"


def test_a_weekend_offered_in_prose_never_reaches_the_draft():
    """The exact live failure: canonical bullets, plus an extra time in prose."""
    slots = _weekday_slots()
    saturday = _next_saturday()
    block = "\n".join(
        f"• {datetime.fromisoformat(s['start']).strftime('%A, %B %-d at %-I:%M %p')}"
        for s in slots
    )
    rogue = (
        f"Hi Dana,\n\nA couple of options:\n\n{block}\n\n"
        f"If neither works, I could also do Saturday, "
        f"{saturday.strftime('%B %-d')} at 10:00 AM.\n\nLet's Win,\nLexi"
    )
    draft, source = _compose(rogue, slots)

    assert source == "template_fallback", (
        "a draft offering an unstaged time must be replaced by the "
        "slot-derived template, not merely refused later at send"
    )
    assert "Saturday" not in draft
    assert saturday.strftime("%B %-d") not in draft


def test_the_replacement_still_offers_every_staged_slot():
    """Falling back must not lose options — the template is built FROM the slots."""
    slots = _weekday_slots(count=3)
    saturday = _next_saturday()
    draft, source = _compose(
        f"Hi Dana,\n\nHow about Saturday, {saturday.strftime('%B %-d')} at 10:00 AM?\n\nLexi",
        slots,
    )
    assert source == "template_fallback"
    for slot in slots:
        weekday = datetime.fromisoformat(slot["start"]).strftime("%A")
        assert weekday in draft, f"{weekday} option was dropped"


def test_composition_and_the_send_gate_agree_by_construction():
    """Whatever composition returns must pass the gate that guards the send."""
    from app.scheduling.draft_slot_sync import draft_matches_slots

    slots = _weekday_slots()
    saturday = _next_saturday()
    for model_output in (
        "Hi Dana,\n\nHow about Saturday, "
        f"{saturday.strftime('%B %-d')} at 10:00 AM?\n\nLexi",
        "Hi Dana,\n\nnothing parseable here at all.\n\nLexi",
        "",
    ):
        draft, _ = _compose(model_output, slots)
        ok, mismatch = draft_matches_slots(draft_body=draft, proposed_slots=slots)
        assert ok, f"composition emitted a draft the send gate refuses: {mismatch}"
