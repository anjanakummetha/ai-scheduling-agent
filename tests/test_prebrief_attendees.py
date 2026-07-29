"""Pre-call briefs — identifying the attendee, and grounding the relationship.

Two bugs shaped this file. The calendar read never asked Graph for `attendees`,
so the brief fell back to passing the whole meeting subject as a person's name.
And a calendar invite counted as prior contact, so someone Kory had never
actually corresponded with read as a known relationship.
"""

from __future__ import annotations

from unittest.mock import patch

from app.assistant.briefings import _guess_external_attendee
from app.assistant.precall_brief import (
    _is_calendar_noise,
    gather_relationship_context,
)
from app.bot.teams_text import parse_teams_command


def _graph_attendee(address: str, name: str = "") -> dict:
    return {"emailAddress": {"address": address, "name": name}}


# --- attendee identification ------------------------------------------------


def test_calendar_reads_request_attendees():
    """Without this select the whole feature silently degrades."""
    import inspect

    from app.integrations import outlook_calendar

    source = inspect.getsource(outlook_calendar)
    reads = source.count('"OUTLOOK_GET_CALENDAR_VIEW"')
    assert source.count('"attendees"') >= reads, "every calendar view must select attendees"


def test_picks_the_external_attendee_over_ifg_colleagues():
    event = {
        "subject": "Teams Call - James Phifer (ACCU Inc) | Matt Maley & Kory Mitchell (IFG)",
        "attendees": [
            _graph_attendee("matt.maley@iconicfounders.com", "Matt Maley"),
            _graph_attendee("Kory.Mitchell@iconicfounders.com", "Kory Mitchell"),
            _graph_attendee("jamesphifer@accuinc.com", "James Phifer"),
        ],
    }
    email, name = _guess_external_attendee(event)
    assert email == "jamesphifer@accuinc.com"
    assert name == "James Phifer"


def test_falls_back_to_the_address_when_no_display_name():
    event = {"attendees": [_graph_attendee("Mia_Kamboris@autoelect.com")]}
    email, name = _guess_external_attendee(event)
    assert email == "mia_kamboris@autoelect.com"
    assert name == "Mia_Kamboris"


def test_plain_string_attendees_still_work():
    event = {"attendees": ["someone@outside.com", "kory.mitchell@iconicfounders.com"]}
    email, _ = _guess_external_attendee(event)
    assert email == "someone@outside.com"


def test_meeting_subject_is_never_used_as_a_person():
    event = {"subject": "WOB - Diagram MD structure for non-deals", "attendees": []}
    email, name = _guess_external_attendee(event)
    assert email == ""
    assert name == "", "the subject must not be passed off as an attendee"


# --- a calendar invite is not a relationship --------------------------------


def test_calendar_invites_and_bots_are_not_correspondence():
    assert _is_calendar_noise({"subject": "Updated invitation: Nick Allen - 30 min"})
    assert _is_calendar_noise({"subject": "Accepted: Intro call"})
    assert _is_calendar_noise({"subject": "Reminder: coffee", "sender": "x@y.com"})
    assert _is_calendar_noise({"subject": "Re: pricing", "sender": "calendar-notification@google.com"})
    assert not _is_calendar_noise({"subject": "Re: Keystone Data Room", "sender": "cody@upland.com"})


def test_invite_only_history_does_not_count_as_having_met():
    """Nick Allen's only mail was the invite and a Google reminder — that is a
    first meeting, and exactly the case where research matters most."""
    invites = [
        {
            "subject": "Updated invitation: Nick Allen - 30 min",
            "sender": "nick@mitchell-allen.com",
            "received_at": "2026-07-28T10:00:00Z",
            "preview": "",
        },
        {
            "subject": "Reminder: Nick Allen - 30 min",
            "sender": "calendar-notification@google.com",
            "received_at": "2026-07-28T09:00:00Z",
            "preview": "",
        },
    ]
    with patch("app.integrations.outlook_inbox.search_inbox", return_value=(invites, None)):
        with patch("app.integrations.outlook_sent.fetch_sent_to_recipient", return_value=[]):
            context = gather_relationship_context("nick@mitchell-allen.com", "Nick Allen")
    assert context["has_real_history"] is False
    assert context["real_message_count"] == 0
    assert context["first_contact"] is None


def test_genuine_correspondence_counts_and_reports_the_earliest_first():
    messages = [
        {
            "subject": "RE: Keystone QofE Check-in",
            "sender": "codyrobertson@upland-ts.com",
            "received_at": "2026-07-27T10:00:00Z",
            "preview": "Following up",
        },
        {
            "subject": "Keystone Data Room",
            "sender": "codyrobertson@upland-ts.com",
            "received_at": "2026-07-01T10:00:00Z",
            "preview": "Kicking this off",
        },
    ]
    with patch("app.integrations.outlook_inbox.search_inbox", return_value=(messages, None)):
        with patch("app.integrations.outlook_sent.fetch_sent_to_recipient", return_value=[]):
            context = gather_relationship_context("codyrobertson@upland-ts.com", "Cody Robertson")
    assert context["has_real_history"] is True
    assert context["real_message_count"] == 2
    assert context["first_contact"]["date"] == "2026-07-01", "oldest message is first contact"


# --- how Kory asks ----------------------------------------------------------


def test_prebrief_phrasings_route_correctly():
    assert parse_teams_command("prebrief") == {"action": "prebrief"}
    for text, who in (
        ("prebrief Ramzi Dagher", "Ramzi Dagher"),
        ("prebrief me on Jane Doe", "Jane Doe"),
        ("pre-call brief for James Phifer", "James Phifer"),
        ("prebrief on ramzi@x.com", "ramzi@x.com"),
    ):
        assert parse_teams_command(text) == {"action": "prebrief_person", "who": who}, text


def test_a_name_matching_two_contacts_asks_instead_of_briefing_one():
    ambiguous = {"ok": True, "ambiguous": True, "kory_message": "2 contacts match — which one?"}
    from app.assistant.precall_brief import build_precall_brief

    with patch(
        "app.integrations.hubspot_manager.enrich_prebrief_from_hubspot", return_value=ambiguous
    ):
        out = build_precall_brief(name="Chris Gavora")
    assert out["ambiguous"] is True
    assert "which one" in out["kory_message"].lower()


def test_brief_requires_someone_to_brief():
    from app.assistant.precall_brief import build_precall_brief

    assert build_precall_brief()["ok"] is False
