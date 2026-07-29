"""Pre-meeting brief — identifying who Kory is actually meeting.

The calendar read never asked Graph for `attendees`, so the field was always
None and the brief fell back to passing the whole meeting subject as a person's
name. That produced lookups like "Teams Call - James Phifer (ACCU Inc) | Matt
Maley & Kory Mitchell (IFG)", which match nothing, and the brief then reported
missing introducer data rather than admitting it never identified anyone.
"""

from __future__ import annotations

from unittest.mock import patch

from app.assistant.briefings import _guess_external_attendee, build_prebrief


def _graph_attendee(address: str, name: str = "") -> dict:
    return {"emailAddress": {"address": address, "name": name}}


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
    """An internal meeting has no outside attendee — that is not a name."""
    event = {"subject": "WOB - Diagram MD structure for non-deals", "attendees": []}
    email, name = _guess_external_attendee(event)
    assert email == ""
    assert name == "", "the subject must not be passed off as an attendee"


def test_internal_meeting_says_so_instead_of_looking_nobody_up():
    with patch("app.integrations.hubspot_manager.enrich_prebrief_from_hubspot") as hs:
        out = build_prebrief(meeting_subject="WOB - internal", include_research=False)
    hs.assert_not_called()
    assert out["found_contact"] is False
    assert "No outside attendee" in out["kory_message"]


@patch("app.assistant.briefings.resolve_introducer_for_contact", return_value=None)
@patch("app.assistant.briefings.format_introducer_line", return_value="**Introduced by:** Unknown")
def test_known_attendee_gets_the_hubspot_block(_fmt, _intro):
    payload = {
        "ok": True,
        "found": True,
        "kory_message": "**James Phifer** - President · ACCU Inc",
    }
    with patch(
        "app.integrations.hubspot_manager.enrich_prebrief_from_hubspot", return_value=payload
    ):
        out = build_prebrief(
            attendee_name="James Phifer",
            attendee_email="jamesphifer@accuinc.com",
            meeting_subject="Teams Call",
            include_research=False,
        )
    assert out["found_contact"] is True
    assert "ACCU Inc" in out["kory_message"]


@patch("app.assistant.briefings.resolve_introducer_for_contact", return_value=None)
@patch("app.assistant.briefings.format_introducer_line", return_value="**Introduced by:** Unknown")
def test_unknown_attendee_is_reported_as_not_in_hubspot(_fmt, _intro):
    with patch(
        "app.integrations.hubspot_manager.enrich_prebrief_from_hubspot",
        return_value={"ok": True, "found": False},
    ):
        out = build_prebrief(
            attendee_name="Nick Allen",
            attendee_email="nick@mitchell-allen.com",
            include_research=False,
        )
    assert out["found_contact"] is False
    assert "Not in HubSpot" in out["kory_message"]
