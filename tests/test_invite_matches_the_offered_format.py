"""The invite must be the meeting the offer email described.

Live proposal 10563 (2026-08-22): the ask said "grab 30 minutes … coffee",
triage labeled it referral_or_intro, and the offer email — which resolves the
meeting TYPE from the words, not the label — promised an in-person coffee at
a Cherry Creek venue. The invite builders then re-derived format from the raw
intent alone: referral_or_intro is a virtual intent, so the counterpart
received a Microsoft Teams meeting titled "Intro:" with a dial-in, for a
coffee. The hold was mistitled "HOLD: Intro call" the same way.

Both builders now resolve the type exactly as the offer did — subject and
body included.
"""

from __future__ import annotations

from app.scheduling.invite_builder import build_hold_action, build_invite_action

SLOT = {"start": "2026-09-01T08:30:00-06:00", "end": "2026-09-01T09:30:00-06:00"}
COFFEE_SUBJECT = "[TEST] Coffee next week — Anjana"
COFFEE_BODY = "Would love to grab 30 minutes next week — would Tuesday or Thursday work?"


def test_a_coffee_ask_gets_an_in_person_invite_despite_the_intro_label():
    action = build_invite_action(
        slot=SLOT,
        meeting_subject=COFFEE_SUBJECT,
        intent="referral_or_intro",
        attendee_email="dana@example.com",
        sender_display="Dana <dana@example.com>",
        body=COFFEE_BODY,
    )
    assert action["is_online_meeting"] is False, (
        "a coffee went out as a Teams meeting (live 10563)"
    )
    assert "Teams" not in str(action["location"]), action["location"]
    assert "Teams meeting" not in str(action["body"])
    assert "Coffee" in str(action["title"]), action["title"]


def test_the_hold_for_a_coffee_is_titled_and_placed_as_one():
    action = build_hold_action(
        slot=SLOT,
        meeting_subject=COFFEE_SUBJECT,
        intent="referral_or_intro",
        option_index=1,
        sender="Dana <dana@example.com>",
        body=COFFEE_BODY,
    )
    assert "Coffee" in str(action["title"]), action["title"]
    assert "Teams" not in str(action["location"]), action["location"]


def test_a_genuine_intro_call_still_gets_a_teams_invite():
    action = build_invite_action(
        slot=SLOT,
        meeting_subject="Intro — Dana <> Kory",
        intent="referral_or_intro",
        attendee_email="dana@example.com",
        sender_display="Dana <dana@example.com>",
        body="Would love to connect for 30 minutes to introduce our fund.",
    )
    assert action["is_online_meeting"] is True
    assert action["location"] == "Microsoft Teams"
