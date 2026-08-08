"""Tests for hold/invite payload builder and Kory-style calendar titles."""

from zoneinfo import ZoneInfo

from app.scheduling.calendar_title import (
    build_confirmed_calendar_title,
    build_hold_calendar_title,
    extract_requested_attendees,
    merge_invite_attendees,
    parse_guest_profile,
)
from app.scheduling.invite_builder import (
    build_hold_action,
    build_invite_action,
    default_location_for_intent,
    is_online_meeting,
)


def test_virtual_call_gets_teams():
    assert default_location_for_intent("virtual_30") == "Microsoft Teams"
    assert is_online_meeting("virtual_30", "Microsoft Teams") is True


def test_coffee_gets_cherry_creek_no_teams():
    loc = default_location_for_intent("coffee")
    assert "Cherry Creek" in loc or "Aviano" in loc or "Olive" in loc
    assert is_online_meeting("coffee", loc) is False


def test_hold_title_podcast_guest():
    guest = parse_guest_profile(
        sender="Anthony Garcia <anthony@example.com>",
        subject="RE: YPO podcast guest",
        body="Happy to record for The Turn.",
    )
    title = build_hold_calendar_title(
        intent="podcast",
        guest=guest,
        subject="RE: YPO podcast guest",
        body="The Turn podcast",
    )
    assert title.startswith("HOLD: Intro call w/ Anthony Garcia")
    assert "podcast guest" in title.lower() or "The Turn" in title


def test_confirmed_intro_with_company_and_time():
    guest = parse_guest_profile(
        sender="KD <kd@bloomatree.com>",
        subject="Intro",
        body="",
    )
    guest = guest.__class__(name="KD", company="Blooma Tree", email="kd@bloomatree.com")
    title = build_confirmed_calendar_title(
        intent="referral_or_intro",
        guest=guest,
        slot_start="2026-06-17T14:00:00-07:00",
        recipient_timezone=ZoneInfo("America/Los_Angeles"),
    )
    assert title.startswith("Intro: KD (Blooma Tree) <> Kory Mitchell (IFG)")
    assert "2 pm PT" in title or "2:00 pm PT" in title


def test_confirmed_podcast_turn_format():
    guest = parse_guest_profile(
        sender="Chris Doyle <chris@billd.com>",
        subject="The Turn podcast",
        body="",
    )
    guest = guest.__class__(name="Chris Doyle", company="Billd", email="chris@billd.com")
    title = build_confirmed_calendar_title(
        intent="podcast",
        guest=guest,
        slot_start="2026-06-17T17:30:00-05:00",
        subject="The Turn",
        body="podcast recording",
        recipient_timezone=ZoneInfo("America/Chicago"),
    )
    assert "Intro: Chris Doyle (Billd) <> Kory Mitchell (IFG)" in title
    assert "The Turn" in title
    assert "CT" in title


def test_coffee_title_kory_matt():
    guest = parse_guest_profile(
        sender="Tom Patton <tom@evergreensurety.com>",
        subject="Coffee?",
        body="",
    )
    guest = guest.__class__(
        name="Tom Patton",
        company="Evergreen Surety",
        email="tom@evergreensurety.com",
    )
    # "Kory/Matt" only when the thread says Matt is joining — matching the
    # attendee logic, so the title never names someone not on the invite.
    title = build_confirmed_calendar_title(
        intent="coffee",
        guest=guest,
        slot_start="2026-06-17T09:00:00-06:00",
        body="Matt will join as well.",
        recipient_timezone=ZoneInfo("America/Denver"),
    )
    assert title.startswith("Coffee: Tom Patton (Evergreen Surety) <> Kory/Matt (Iconic Founders)")

    solo_title = build_confirmed_calendar_title(
        intent="coffee",
        guest=guest,
        slot_start="2026-06-17T09:00:00-06:00",
        recipient_timezone=ZoneInfo("America/Denver"),
    )
    assert solo_title.startswith("Coffee: Tom Patton (Evergreen Surety) <> Kory Mitchell (IFG)")


def test_extract_kory_requested_attendees():
    body = (
        "Looping in Lexi — please include sarah@ea.com on the invite.\n"
        "Also add john.partner@firm.com."
    )
    extras = extract_requested_attendees(
        body,
        primary_email="guest@example.com",
        intent="referral_or_intro",
    )
    assert "sarah@ea.com" in extras
    assert "john.partner@firm.com" in extras


def test_merge_invite_attendees_primary_plus_extra():
    attendees = merge_invite_attendees(
        "guest@example.com",
        None,
        text="Please include assistant@company.com on the calendar invite.",
        intent="referral_or_intro",
    )
    assert attendees[0] == "guest@example.com"
    assert "assistant@company.com" in attendees


def test_hold_action_uses_kory_format():
    action = build_hold_action(
        slot={"start": "2026-06-17T16:00:00-04:00", "end": "2026-06-17T16:30:00-04:00"},
        meeting_subject="RE: intro",
        intent="referral_or_intro",
        option_index=1,
        sender="Braden Edwards <braden@example.com>",
        body="Looking forward to connecting.",
    )
    assert action["title"].startswith("HOLD:")
    assert "Braden Edwards" in action["title"]
    assert action["is_online_meeting"] is False


def test_invite_action_multiple_attendees_and_teams():
    action = build_invite_action(
        slot={"start": "2026-06-17T16:00:00-04:00", "end": "2026-06-17T16:30:00-04:00"},
        meeting_subject="RE: intro call",
        intent="referral_or_intro",
        attendee_email="braden@example.com",
        sender_display="Braden Edwards <braden@example.com>",
        body="Kory asked to include ea@company.com on the invite.",
        recipient_timezone=ZoneInfo("America/New_York"),
    )
    assert action["is_online_meeting"] is True
    assert action["location"] == "Microsoft Teams"
    assert "braden@example.com" in action["attendees"]
    assert "ea@company.com" in action["attendees"]
    assert action["title"].startswith("Intro:")
    assert "Braden Edwards" in action["title"]
