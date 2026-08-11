"""Pre-call briefs — identifying the attendee, and grounding the relationship.

Two bugs shaped this file. The calendar read never asked Graph for `attendees`,
so the brief fell back to passing the whole meeting subject as a person's name.
And a calendar invite counted as prior contact, so someone Kory had never
actually corresponded with read as a known relationship.
"""

from __future__ import annotations

from datetime import date, timedelta
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
    with patch.object(pb, "upcoming_meetings", return_value=events):
        with patch.object(pb, "todays_meetings", return_value=events):
            out = pb.build_meeting_brief("something unrelated entirely")
    assert out["matched"] is False
    assert "No meeting in the next 30 days matches" in out["kory_message"]


def test_introducer_local_part_is_rendered_as_a_name():
    from app.assistant.precall_brief import _humanize_name

    assert _humanize_name("mia.platon") == "Mia Platon"
    assert _humanize_name("heidi_heckler@x.com") == "Heidi Heckler"
    assert _humanize_name("Matt Maley") == "Matt Maley"


# --- meetings beyond today --------------------------------------------------


def _event(subject: str, iso: str, guests: list[tuple[str, str]]) -> dict:
    return {
        "subject": subject,
        "start": {"dateTime": iso},
        "attendees": [_graph_attendee(email, name) for email, name in guests],
    }


def test_date_in_the_request_is_understood():
    from app.assistant.precall_brief import _extract_date_hint

    remaining, when = _extract_date_hint("justin who I am meeting August 7th")
    assert when.endswith("-08-07")
    assert "justin" in remaining.lower()
    assert "august" not in remaining.lower()

    assert _extract_date_hint("the ACCU call 8/14")[1].endswith("-08-14")
    assert _extract_date_hint("my next meeting")[1] == ""


def test_a_date_picks_between_two_people_with_the_same_first_name():
    """Two Justins on different days: the date in the ask must resolve to one.

    The meetings have to be in the future. A bare "August 7th" means the *next*
    August 7th, so once the hardcoded fixtures aged into the past the query
    resolved a year beyond them, matched neither, and fell back to returning
    both Justins — the disambiguation under test never ran.
    """
    from app.assistant.precall_brief import match_meetings

    earlier = date.today() + timedelta(days=7)
    target = date.today() + timedelta(days=10)
    events = [
        _event("Follow-Up: Endurance Plumbing", f"{earlier}T14:00:00",
               [("justin@yetiark.com", "Justin Bond")]),
        _event("Intro Call - Justin", f"{target}T12:00:00",
               [("jbertram@agilityep.com", "Justin Bertram")]),
    ]
    matched = match_meetings(
        f"justin who I am meeting {target.strftime('%B')} {target.day}th", events
    )
    assert len(matched) == 1
    assert matched[0]["subject"] == "Intro Call - Justin"


def test_without_a_date_both_matches_are_returned_so_kory_can_choose():
    from app.assistant.precall_brief import match_meetings

    events = [
        _event("Follow-Up: Endurance Plumbing", "2026-08-04T14:00:00",
               [("justin@yetiark.com", "Justin Bond")]),
        _event("Intro Call - Justin", "2026-08-07T12:00:00",
               [("jbertram@agilityep.com", "Justin Bertram")]),
    ]
    assert len(match_meetings("justin", events)) == 2


def test_filler_words_do_not_match_meetings():
    """"who I am meeting" must not score against every subject on the calendar."""
    from app.assistant.precall_brief import match_meetings

    events = [_event("Board sync", "2026-08-04T14:00:00", [("a@b.com", "Ann")])]
    assert match_meetings("who I am meeting", events) == []


def test_someone_only_on_the_calendar_is_still_found():
    """An intro call with a stranger is the normal pre-call case; they will not
    be in HubSpot."""
    from app.assistant.precall_brief import find_attendee_by_name

    events = [
        _event("Intro Call", "2026-08-07T12:00:00", [("jbertram@agilityep.com", "Justin Bertram")])
    ]
    email, name, event = find_attendee_by_name("Justin Bertram", events=events)
    assert email == "jbertram@agilityep.com"
    assert name == "Justin Bertram"
    assert event["subject"] == "Intro Call"


def test_future_meetings_show_their_date_not_just_a_time():
    from app.assistant.precall_brief import _event_when

    label = _event_when(_event("x", "2026-08-07T12:00:00", []))
    assert "Aug" in label and "12:00 PM" in label


# --- calendar display -------------------------------------------------------


def test_event_time_respects_the_zone_stated_on_the_event():
    """Graph sends a NAIVE dateTime with the zone beside it, and the calendar
    read has already converted it. Treating naive as UTC subtracted the offset
    twice and showed a 6:30 AM session as 12:30 AM."""
    from zoneinfo import ZoneInfo

    from app.assistant.briefings import _format_event_time

    mt = ZoneInfo("America/Denver")
    assert (
        _format_event_time({"dateTime": "2026-07-29T06:30:00", "timeZone": "America/Denver"}, mt)
        == "6:30 AM"
    )
    # An explicit UTC offset is still honoured.
    assert _format_event_time({"dateTime": "2026-07-29T12:30:00Z"}, mt) == "6:30 AM"
    # No zone stated at all: fall back to Kory's, not UTC.
    assert _format_event_time({"dateTime": "2026-07-29T06:30:00"}, mt) == "6:30 AM"


def test_attendees_render_as_names_not_graph_objects():
    """Selecting attendees for the prebrief made this line print raw dicts."""
    from app.assistant.briefings import _attendee_names

    graph = [
        _graph_attendee("heidi.heckler@iconicfounders.com", "Heidi Heckler"),
        _graph_attendee("nobody@outside.com"),
    ]
    assert _attendee_names(graph) == ["Heidi Heckler", "nobody@outside.com"]
    assert _attendee_names(["Plain Name"]) == ["Plain Name"]
    assert _attendee_names(None) == []
