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


# --- meeting scope: cost and colleagues ------------------------------------


def test_colleague_on_a_personal_address_is_not_an_outside_guest():
    """Sujash appears twice on one invite — IFG address and UC Davis."""
    from app.assistant.precall_brief import external_attendees

    event = {
        "attendees": [
            _graph_attendee("Kory.Mitchell@iconicfounders.com", "Kory Mitchell"),
            _graph_attendee("sujash.barman@iconicfounders.com", "Sujash Barman"),
            _graph_attendee("sjbarman@ucdavis.edu", "Sujash Barman"),
            _graph_attendee("nick.allen.cse@gmail.com", "Nick Allen"),
        ]
    }
    assert external_attendees(event) == [("nick.allen.cse@gmail.com", "Nick Allen")]


def test_every_outside_attendee_is_returned_not_just_the_first():
    from app.assistant.precall_brief import external_attendees

    event = {
        "attendees": [
            _graph_attendee("kory.mitchell@iconicfounders.com", "Kory Mitchell"),
            _graph_attendee("a@outside.com", "Ann Alpha"),
            _graph_attendee("b@other.com", "Bob Beta"),
        ]
    }
    assert external_attendees(event) == [
        ("a@outside.com", "Ann Alpha"),
        ("b@other.com", "Bob Beta"),
    ]


def test_bare_prebrief_lists_meetings_without_researching_anyone():
    """Briefing the whole day cost ~15s per attendee and timed out."""
    from app.assistant import precall_brief as pb

    events = [
        {"subject": "Intro call", "start": {"dateTime": "2026-07-29T14:00:00"},
         "attendees": [_graph_attendee("x@outside.com", "Ex Ternal")]},
        {"subject": "Team sync", "start": {"dateTime": "2026-07-29T10:00:00"},
         "attendees": [_graph_attendee("k@iconicfounders.com", "Kory Mitchell")]},
    ]
    with patch.object(pb, "todays_meetings", return_value=events):
        with patch.object(pb, "build_precall_brief") as brief:
            out = pb.list_todays_meetings()
    brief.assert_not_called()
    assert out["briefable"] == 1
    assert "Intro call" in out["kory_message"]
    assert "internal" in out["kory_message"]


def test_meeting_match_falls_back_to_the_listing_when_nothing_matches():
    from app.assistant import precall_brief as pb

    events = [{"subject": "Intro call", "start": {"dateTime": "2026-07-29T14:00:00"}, "attendees": []}]
    with patch.object(pb, "todays_meetings", return_value=events):
        out = pb.build_meeting_brief("something unrelated entirely")
    assert out["matched"] is False
    assert "No meeting today matches" in out["kory_message"]


def test_introducer_local_part_is_rendered_as_a_name():
    from app.assistant.precall_brief import _humanize_name

    assert _humanize_name("mia.platon") == "Mia Platon"
    assert _humanize_name("heidi_heckler@x.com") == "Heidi Heckler"
    assert _humanize_name("Matt Maley") == "Matt Maley"
