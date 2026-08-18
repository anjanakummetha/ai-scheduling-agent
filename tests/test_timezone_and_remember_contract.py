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


def test_a_city_and_state_signature_gets_their_zone_quoted_first():
    """Kory's rule applies to city+state signatures, which are very common.

    "San Francisco, CA" is a direct statement of where someone is — as reliable
    as "I'm on Pacific time" in the body, which has always scored "known". It
    used to score "inferred" purely because a different function produced it,
    and email_format renders MT-only for anything below "known". So a prospect
    signing off with their city got Mountain Time and a "couldn't identify your
    time zone" note, while the signal sat right there in the signature.

    Confidence now describes the SIGNAL rather than its source — see
    _SIGNATURE_CITY_PATTERNS. The safety property this replaced is still pinned
    by the two tests below.
    """
    result = _offer(
        "Can we meet next week?\n\n--\nDana Reyes\nFounder, Northbeam\n"
        "San Francisco, CA"
    )
    assert result.recipient_timezone == "America/Los_Angeles"
    assert result.recipient_timezone_confidence == "known"
    assert result.formatted_slots, result.failure_message
    for line in result.formatted_slots:
        assert "PT" in line, line
        assert "MT)" in line, line
        assert line.index("PT") < line.index("MT)"), (
            f"their zone must come first, MT in parentheses: {line}"
        )


def test_a_bare_city_mention_still_falls_back_to_MT():
    """The safety property, kept where it belongs.

    A city name with no state or country is not a statement of residence — "I'll
    be in London next week" matches the same pattern. A wrong zone in an
    outbound email is worse than an honest MT fallback, so weak signals still
    get MT only.
    """
    result = _offer("Can we meet next week? I'll be in London for a few days.")
    assert result.recipient_timezone_confidence != "known"
    for line in result.formatted_slots:
        assert "MT" in line
        assert "GMT" not in line and "BST" not in line, (
            "either quote their zone confidently or not at all"
        )


def test_a_phone_area_code_still_falls_back_to_MT():
    """An area code travels with the person, not with where they live."""
    result = _offer(
        "Can we meet next week?\n\n--\nDana Reyes\nm: (415) 555-0142"
    )
    assert result.recipient_timezone_confidence != "known"
    for line in result.formatted_slots:
        assert "MT" in line
        assert "PT" not in line, "an area code is a proxy, not a location"
