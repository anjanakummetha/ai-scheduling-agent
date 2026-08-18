"""Every scheduling and Outlook tool, invoked the way Teams invokes it.

An audit on 2026-08-18 found 27 of the 44 scheduling/Outlook tools were not
named in a single test. They were reachable by the model and never exercised —
and the one time a tool of this class was driven through the gateway path
(`pending`, via teams_parity), it raised ToolError in Teams while 1,221 unit
tests passed.

The property asserted here is deliberately narrow and absolute:

    **No tool may raise.**

A tool that returns {"ok": false, "error": ...} is a bad answer Lexi can explain
to Kory. A tool that raises becomes a ToolError with no usable message, which is
what "she broke again" looks like from the chat window. Composio is blocked in
this suite, so every one of these runs with its network dependency failing —
which is exactly the condition (an outage, an expired token, a throttle) where
raising instead of reporting does the most damage.

Calls go through mcp._tool_manager.call_tool, so registration and FastMCP
argument coercion are covered too.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.teams_parity import call_tool, registered_tool_names

MT = ZoneInfo("America/Denver")


def _iso(days_ahead: int, hour: int) -> str:
    day = date.today() + timedelta(days=days_ahead)
    return datetime(day.year, day.month, day.day, hour, 0, tzinfo=MT).isoformat()


# (tool, kwargs) — argument shapes a model would plausibly send.
READ_TOOLS: list[tuple[str, dict]] = [
    ("lexi_get_calendar_availability", {"days": "7"}),
    ("lexi_today_calendar", {}),
    ("lexi_list_calendars", {"role": "all"}),
    ("lexi_get_family_calendar_status", {}),
    ("lexi_summarize_calendar_window", {"query": "next week"}),
    ("lexi_check_time_slot", {"start_iso": _iso(7, 9), "end_iso": _iso(7, 10)}),
    ("lexi_recipient_timezone", {"sender_email": "c@example.com", "body": "I'm in Boston"}),
    ("lexi_get_inbound_reply_queue", {}),
    ("lexi_list_kory_memory", {}),
    ("lexi_preview_scheduling_email", {}),
    ("lexi_find_slots", {"subject": "Intro", "body": "Can we meet next week?",
                         "intent": "referral_or_intro", "meeting_format": "",
                         "sender_email": "c@example.com"}),
    ("lexi_preview_schedule", {"subject": "Intro", "body": "Can we meet next week?",
                               "sender_email": "c@example.com", "intent": "referral_or_intro"}),
    ("lexi_propose_schedule", {"subject": "Intro", "body": "Can we meet next week?",
                               "sender": "c@example.com", "thread_id": "no-such-thread"}),
    ("lexi_meeting_brief", {"meeting": "Weekly standup"}),
    ("lexi_validate_slots", {"slots_json": f'[{{"start":"{_iso(7,9)}","end":"{_iso(7,10)}"}}]',
                             "intent": "referral_or_intro"}),
    ("lexi_validate_scheduling_cases", {"preset": "core", "cases_json": ""}),
    ("lexi_find_meeting_times", {"payload_json": "{}"}),
    ("lexi_get_scheduling_session", {"session_id": "no-such-session"}),
    ("lexi_get_scheduling_context", {"proposal_id": "999999999"}),
    ("lexi_hubspot_cleanup_proposals", {"inactive_days": "180"}),
]

# Write-capable. Every one is called WITHOUT confirmation, so a correct tool
# must refuse rather than act. Kory's approval gate is the only thing between
# these and his real calendar and mailbox.
UNCONFIRMED_WRITE_TOOLS: list[tuple[str, dict]] = [
    ("lexi_create_calendar_event", {"title": "[TEST] parity", "start_iso": _iso(9, 9),
                                    "end_iso": _iso(9, 10), "confirm": "false"}),
    ("lexi_move_calendar_event", {"event_id": "no-such-event", "start_iso": _iso(9, 11),
                                  "end_iso": _iso(9, 12), "confirm": "false"}),
    ("lexi_place_calendar_hold", {"title": "[TEST] hold", "start_iso": _iso(9, 13),
                                  "end_iso": _iso(9, 14), "confirm": "false"}),
    ("lexi_accept_calendar_invite", {"event_id": "no-such-event"}),
    ("lexi_decline_calendar_invite", {"event_id": "no-such-event", "comment": "conflict"}),
    ("lexi_send_outbound_email", {"to_email": "anjanakummetha@gmail.com",
                                  "subject": "[TEST] parity", "body": "test",
                                  "confirm_send": "false"}),
    ("lexi_draft_outbound_email", {"to_email": "anjanakummetha@gmail.com",
                                   "subject": "[TEST] parity", "body": "test"}),
    ("lexi_save_email_to_drafts", {"to_email": "anjanakummetha@gmail.com",
                                   "subject": "[TEST] parity", "body": "test"}),
    ("lexi_draft_reply_for_email", {"subject_contains": "no-such-subject-xyz"}),
    ("lexi_execute_outlook_action", {"slug": "OUTLOOK_LIST_MESSAGES",
                                     "arguments_json": "{}", "confirm": "false"}),
    ("lexi_retry_scheduling", {"proposal_id": "999999999", "guidance": "Thursday only"}),
    ("lexi_update_proposal_draft", {"proposal_id": "999999999", "drafted_reply": "Hi"}),
    ("lexi_escalate_to_kory", {"proposal_id": "999999999", "reason": "test"}),
    ("lexi_decline_inbound_reply", {"proposal_id": "999999999", "reason": "test"}),
    ("lexi_begin_draft_reply", {"proposal_id": "999999999"}),
    ("lexi_add_conflict_calendar", {"calendar_name": "no-such-calendar"}),
    ("lexi_upsert_scheduling_session", {"session_id": "parity-session", "channel": "teams",
                                        "context_json": "{}", "status": "open"}),
]

ALL_TOOLS = READ_TOOLS + UNCONFIRMED_WRITE_TOOLS


def _ids(pairs):
    return [name for name, _ in pairs]


def test_the_audit_covers_every_scheduling_tool_that_exists():
    """If a new scheduling tool is added, it must be listed here."""
    import re

    registered = registered_tool_names()
    relevant = {
        t
        for t in registered
        if re.search(
            r"calendar|schedul|hold|invite|meeting|slot|outlook|remember|forget"
            r"|memory|reply|send|draft|retry|escalat|propos|timezone",
            t,
        )
    }
    exercised = {name for name, _ in ALL_TOOLS} | {
        "lexi_handle_teams_command",  # covered by test_teams_parity_flows
        "lexi_remember_kory_fact",
        "lexi_forget_kory_fact",
        "lexi_start_scheduling",
        "lexi_hubspot_meeting_note",
    }
    missing = sorted(relevant - exercised)
    assert not missing, f"scheduling tools with no parity coverage: {missing}"


@pytest.mark.parametrize("name,kwargs", ALL_TOOLS, ids=_ids(ALL_TOOLS))
def test_no_scheduling_tool_raises_when_its_dependencies_fail(name: str, kwargs: dict):
    """Composio is blocked here. Every tool must REPORT, never raise.

    A raised exception reaches Teams as ToolError with no usable text, which is
    indistinguishable from Lexi being broken.
    """
    result = call_tool(name, **kwargs)
    assert result is not None, f"{name} returned nothing at all"


@pytest.mark.parametrize(
    "name,kwargs", UNCONFIRMED_WRITE_TOOLS, ids=_ids(UNCONFIRMED_WRITE_TOOLS)
)
def test_an_unconfirmed_write_never_reports_success(name: str, kwargs: dict):
    """Called without confirmation, a write tool must not claim it acted."""
    from tests.teams_parity import _as_dict

    payload = _as_dict(call_tool(name, **kwargs))
    blob = str(payload).lower()
    if payload.get("ok") is True:
        # Some of these legitimately succeed at a read-only sub-step. What must
        # never appear is a claim that something was sent, booked or moved.
        for claim in ("email sent", "invite sent", "event created", "hold placed",
                      "meeting booked", "successfully sent"):
            assert claim not in blob, f"{name} claimed '{claim}' without confirmation"
