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


def test_internal_meeting_is_skipped_entirely():
    """No outside attendee means nothing to brief — and nobody to look up."""
    with patch("app.integrations.hubspot_manager.enrich_prebrief_from_hubspot") as hs:
        out = build_prebrief(meeting_subject="WOB - internal", include_research=False)
    hs.assert_not_called()
    assert out["skipped"] is True
    assert out["kory_message"] == ""


@patch("app.assistant.briefings.resolve_introducer_for_contact", return_value=None)
@patch("app.assistant.briefings._mailbox_history", return_value=(True, "Last thread: *Re: call*"))
def test_known_attendee_gets_the_hubspot_block(_mail, _intro):
    payload = {
        "ok": True,
        "found": True,
        "contact": {"notes_last_contacted": "2026-07-22T00:00:00Z"},
        "kory_message": "**James Phifer** — President · ACCU Inc\nStage: Deal In Progress",
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
    assert out["met_before"] is True
    message = out["kory_message"]
    assert "ACCU Inc" in message
    # The header already names him; the CRM block must not repeat it.
    assert message.count("James Phifer") == 1


@patch("app.assistant.briefings.resolve_introducer_for_contact", return_value=None)
@patch("app.assistant.briefings._mailbox_history", return_value=(False, ""))
def test_attendee_with_no_record_anywhere_reads_as_a_first_meeting(_mail, _intro):
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
    assert out["met_before"] is False
    assert "First meeting" in out["kory_message"]


@patch("app.assistant.briefings._mailbox_history", return_value=(False, ""))
def test_unknown_introducer_is_not_printed_as_noise(_mail):
    """"Introduced by: Unknown" appeared on every meeting and told him nothing."""
    with patch(
        "app.integrations.hubspot_manager.enrich_prebrief_from_hubspot",
        return_value={"ok": True, "found": False},
    ):
        with patch("app.assistant.briefings.resolve_introducer_for_contact", return_value=None):
            out = build_prebrief(
                attendee_name="Nick Allen",
                attendee_email="nick@x.com",
                include_research=False,
            )
    assert "unknown" not in out["kory_message"].lower()


# --- research targeting: new people only -----------------------------------


def _no_hubspot():
    return patch(
        "app.integrations.hubspot_manager.enrich_prebrief_from_hubspot",
        return_value={"ok": True, "found": False},
    )


@patch("app.assistant.briefings.resolve_introducer_for_contact", return_value=None)
@patch("app.assistant.briefings._mailbox_history", return_value=(False, ""))
@patch("app.integrations.person_research.research_person")
def test_research_runs_for_someone_kory_has_not_met(mock_research, _mail, _intro):
    mock_research.return_value = {"web_summary": {"answer": "She founded Acme in 2019."}}
    with _no_hubspot():
        out = build_prebrief(attendee_name="New Person", attendee_email="new@outside.com")
    mock_research.assert_called_once()
    assert out["met_before"] is False
    assert out["research_ran"] is True
    assert "First meeting" in out["kory_message"]
    assert "founded Acme" in out["kory_message"]


@patch("app.assistant.briefings.resolve_introducer_for_contact", return_value=None)
@patch("app.assistant.briefings._mailbox_history", return_value=(True, "Last thread: *Re: hi* — 2026-07-01"))
@patch("app.integrations.person_research.research_person")
def test_research_is_skipped_for_someone_he_already_knows(mock_research, _mail, _intro):
    with _no_hubspot():
        out = build_prebrief(attendee_name="Old Friend", attendee_email="known@outside.com")
    mock_research.assert_not_called()
    assert out["met_before"] is True
    assert out["research_ran"] is False
    assert "Last thread" in out["kory_message"]


def test_research_answer_survives_the_search_payload_shape():
    """web_summary is {"answer", "citations"}; .strip() on it raised AttributeError
    and the caller swallowed it, so background never appeared."""
    from app.assistant.briefings import _research_answer, _research_sources

    bundle = {
        "web_summary": {
            "answer": "James Phifer is President of ACCU, Inc.",
            "citations": [{"id": "https://accuinc.com/meet-the-team/"}],
        }
    }
    assert _research_answer(bundle).startswith("James Phifer")
    assert _research_sources(bundle) == ["https://accuinc.com/meet-the-team/"]
    # A plain string still works, and a missing payload yields nothing.
    assert _research_answer({"web_summary": "plain"}) == "plain"
    assert _research_answer({}) == ""


def test_attendee_name_is_not_printed_twice():
    from app.assistant.briefings import _strip_leading_name

    block = "**James Phifer** — President · ACCU Inc\nStage: Deal In Progress"
    out = _strip_leading_name(block, "James Phifer")
    assert out.startswith("President · ACCU Inc")
    assert "James Phifer" not in out
    # An unrelated block is left alone.
    assert _strip_leading_name("Stage: Active", "James Phifer") == "Stage: Active"
