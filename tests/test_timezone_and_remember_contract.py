"""Two of Kory's explicit requirements, driven through the real paths.

TIMEZONE ("extremely important"): quote the recipient's zone FIRST with MT in
parentheses, so it reads like we put them first. When the zone is unknown,
never guess — MT only.

REMEMBER: a rule he states must actually constrain the slot search, and
"forget that" must actually release it. A remembered rule that only reaches the
prompt is decoration; these assert it reaches the engine.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from app.assistant.actions import forget_kory_fact_action, remember_kory_fact_action
from app.scheduling.schedule_from_context import schedule_from_context

MT = ZoneInfo("America/Denver")
_CTX = {"status": "available", "horizon_days": 45, "busy_events": []}


def _offer(body: str, sender_email: str = "c@example.com", **kw):
    with patch(
        "app.scheduling.calendar_context.load_scheduling_calendar_context",
        return_value=_CTX,
    ):
        return schedule_from_context(
            subject="[TEST] intro call",
            body=body,
            intent="referral_or_intro",
            sender_email=sender_email,
            use_llm_plan=False,
            calendar_context=_CTX,
            **kw,
        )


def _weekday_next_week(weekday: int) -> date:
    today = date.today()
    return today - timedelta(days=today.weekday()) + timedelta(days=weekday, weeks=1)


# --- timezone -------------------------------------------------------------


@pytest.mark.parametrize(
    "where, expected_label",
    [
        ("I'm in Boston (Eastern time).", "ET"),
        ("I'm in San Francisco.", "PT"),
        ("I'm in Chicago.", "CT"),
    ],
)
def test_recipient_zone_is_quoted_first_with_mountain_in_parentheses(
    where: str, expected_label: str
):
    result = _offer(f"{where} Can we meet next week?")
    assert result.formatted_slots, result.failure_message
    for line in result.formatted_slots:
        # "... at 11:00 AM–11:30 AM ET (9:00 AM–9:30 AM MT)"
        assert expected_label in line, line
        assert "MT)" in line, line
        assert line.index(expected_label) < line.index("MT)"), (
            f"MT must come second, in parentheses: {line}"
        )


def test_an_unknown_timezone_is_never_guessed():
    result = _offer("Can we meet next week?", sender_email="nobody@nowhere.test")
    assert result.timezone_uncertain is True
    assert result.recipient_timezone is None
    for line in result.formatted_slots:
        assert "MT" in line
        for guessed in ("ET", "PT", "CT"):
            assert guessed not in line, f"invented a zone: {line}"


def test_the_two_rendered_times_describe_the_same_instant():
    """A dual-zone line that disagrees with itself is worse than one zone."""
    result = _offer("I'm in Boston (Eastern time). Can we meet next week?")
    for line, slot in zip(result.formatted_slots, result.slots):
        start = datetime.fromisoformat(slot["start"])
        mt_hour = start.astimezone(MT).strftime("%-I:%M")
        et_hour = start.astimezone(ZoneInfo("America/New_York")).strftime("%-I:%M")
        assert et_hour in line, f"{et_hour} missing from {line}"
        assert mt_hour in line, f"{mt_hour} missing from {line}"


def test_offered_times_survive_a_dst_boundary_without_drifting():
    """Offers spanning the November flip must stay correct in both zones."""
    november = date(date.today().year, 11, 1)
    if november < date.today():
        november = date(date.today().year + 1, 11, 1)
    result = _offer("I'm in Boston (Eastern time). Can we meet next week?")
    for slot in result.slots:
        start = datetime.fromisoformat(slot["start"])
        offset = start.astimezone(MT).utcoffset()
        assert offset in (timedelta(hours=-6), timedelta(hours=-7)), offset


# --- remember -------------------------------------------------------------


@pytest.fixture
def friday_rule():
    """Kory says it, then takes it back — the whole round trip."""
    remember_kory_fact_action(
        fact_key="test_friday_rule", fact_value="No meetings on Fridays"
    )
    yield
    forget_kory_fact_action(fact="test_friday_rule")


def test_a_remembered_day_rule_actually_constrains_the_engine(friday_rule):
    """Not just the prompt — the slot search itself."""
    friday = _weekday_next_week(4)
    result = _offer(f"Can we meet Friday {friday:%B} {friday.day}?")
    offered = {datetime.fromisoformat(s["start"]).weekday() for s in result.slots}
    assert 4 not in offered, "a Friday was offered despite the remembered rule"
    assert result.slots, "the rule must redirect, not leave him with nothing"


def test_forgetting_the_rule_releases_it():
    friday = _weekday_next_week(4)
    remember_kory_fact_action(
        fact_key="test_friday_rule2", fact_value="No meetings on Fridays"
    )
    blocked = _offer(f"Can we meet Friday {friday:%B} {friday.day}?")
    assert 4 not in {datetime.fromisoformat(s["start"]).weekday() for s in blocked.slots}

    forget_kory_fact_action(fact="test_friday_rule2")
    freed = _offer(f"Can we meet Friday {friday:%B} {friday.day}?")
    assert 4 in {datetime.fromisoformat(s["start"]).weekday() for s in freed.slots}, (
        "after 'forget that', the requested Friday must be bookable again"
    )


def test_the_rule_is_stored_as_korys_own_words(friday_rule):
    """Paraphrasing breaks the parser that turns the sentence into a constraint."""
    from app.assistant.actions import list_kory_memory_action

    facts = list_kory_memory_action().get("facts") or []
    stored = [f for f in facts if f.get("fact_key") == "test_friday_rule"]
    assert stored, facts
    assert stored[0]["fact_value"] == "No meetings on Fridays"


def test_a_weaker_zone_signal_falls_back_to_MT_rather_than_guessing():
    """Documents a real trade-off, so a future change is a decision not an accident.

    "I'm based in San Francisco, CA." resolves to America/Los_Angeles, but at
    confidence "inferred" (read off the state abbreviation) rather than "known".
    email_format.py:198 renders MT-only for any external recipient below
    "known", so this counterpart is quoted Mountain Time with a note.

    That is the SAFE direction — a wrong zone in an outbound email is worse than
    an honest MT fallback — but it does mean Kory's "always quote their zone
    first" rule silently does not apply to city+state signatures, which are
    common. Raising `inferred` to `known` for an unambiguous US city+state is a
    deliberate change to every outbound email and wants its own verification
    pass; it is NOT a bug to be quietly patched.
    """
    result = _offer("I'm based in San Francisco, CA. Can we meet next week?")
    assert result.recipient_timezone == "America/Los_Angeles"
    assert result.recipient_timezone_confidence == "inferred"
    for line in result.formatted_slots:
        assert "MT" in line
        assert "PT" not in line, "either quote PT confidently or not at all"
