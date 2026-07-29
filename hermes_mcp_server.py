"""MCP bridge: Lexi scheduling assistant tools for Hermes (Hermes-only Teams).

Hermes (Claude OAuth) is the sole Teams front door. This server exposes
calendar, email, hold, queue, and approval actions against Kory's Outlook via Composio.
A background Lexi worker (orchestrator) starts automatically for inbound email.

Run with:
    python hermes_mcp_server.py
"""

from __future__ import annotations

import json
import os
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agents.comms_agent import execute_lexi_approval, get_lexi_pending_queue
from app.assistant import actions as lexi
from scripts.init_lexi_db import init_lexi_db


mcp = FastMCP("ai-scheduling-backend")

_WORKER_BOOTSTRAPPED = False


def _bootstrap_lexi_worker() -> None:
    """Start headless Lexi orchestrator when Hermes loads this MCP server."""
    global _WORKER_BOOTSTRAPPED
    if _WORKER_BOOTSTRAPPED:
        return
    if os.getenv("LEXI_EMBED_WORKER", "true").lower() not in {"1", "true", "yes"}:
        return
    from app.worker.runner import start_lexi_worker

    start_lexi_worker()
    _WORKER_BOOTSTRAPPED = True


class ExecuteLexiApprovalInput(BaseModel):
    model_config = ConfigDict(strict=True)
    proposal_id: int = Field(..., ge=1, description="Lexi proposal id from the pending queue.")
    decision: str = Field(
        ...,
        description="One of: approved, modified, rejected.",
    )
    selected_slot: str = Field(
        default="",
        description=(
            "ISO start time, JSON slot object, or empty string when rejecting. "
            'Example: {"start":"2026-06-03T10:00:00-06:00","end":"2026-06-03T10:30:00-06:00"}'
        ),
    )
    authorized_by: str = Field(
        ...,
        min_length=1,
        description="Azure AD object id or UPN of the approving user.",
    )
    modification_notes: str = Field(
        default="",
        max_length=500,
        description="Optional notes when decision is modified.",
    )


def _ok(data: dict[str, Any]) -> str:
    # Every result carries the real date: the model was resolving "August 10"
    # to 2025 and offering to "correct" a correctly-stored 2026 date.
    return json.dumps({"ok": True, "today": _today_mt(), **data}, default=str)


def _today_mt() -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime.now(tz=ZoneInfo("America/Denver")).strftime("%Y-%m-%d (%A)")


def _error(message: str, *, code: str = "tool_error") -> str:
    return json.dumps({"ok": False, "error_code": code, "message": message})


def _wrap(action: str, fn, **kwargs: Any) -> str:
    try:
        result = fn(**kwargs)
        if isinstance(result, dict) and result.get("ok") is False and "error_code" not in result:
            return json.dumps({"ok": False, "error_code": "action_failed", **result}, default=str)
        return _ok({"action": action, "result": result})
    except PermissionError as exc:
        # Needs Kory's go-ahead — not a missing capability. Said plainly because
        # this was being relayed to Kory as "the tool isn't available".
        return _error(
            f"{action} needs confirmation: {exc} "
            "Ask Kory to confirm, then call this tool again with confirm='true'. "
            "Do not tell him the feature is unavailable — it works once confirmed.",
            code="confirmation_required",
        )
    except Exception as exc:
        return _error(f"{action} failed: {type(exc).__name__}: {exc}", code="exception")


# ── Conversational assistant (Hermes drives dialogue; tools execute) ─────────


@mcp.tool()
def lexi_get_inbound_reply_queue() -> str:
    """New inbound emails awaiting Kory's yes/no on whether to draft a reply."""
    return _wrap("lexi_get_inbound_reply_queue", lexi.get_inbound_reply_queue_action)


@mcp.tool()
def lexi_draft_reply_for_email(subject_contains: str, voice_mode: str = "kory") -> str:
    """Kory asked in chat to draft a reply (no CC Lexi). Finds email by subject fragment, slots + card.

    voice_mode: kory (default) or lexi. Reply to Kory with kory_message only.
    """
    return _wrap(
        "lexi_draft_reply_for_email",
        lexi.draft_reply_for_subject_action,
        subject_contains=subject_contains.strip(),
        voice_mode=voice_mode.strip() or "kory",
    )


@mcp.tool()
def lexi_begin_draft_reply(proposal_id: str, voice_mode: str = "kory") -> str:
    """After Kory says yes (or chat draft): draft reply. voice_mode: kory | lexi. Quote kory_message to Kory."""
    try:
        pid = int(proposal_id)
    except ValueError:
        return _error("proposal_id must be an integer string.", code="validation_error")
    return _wrap(
        "lexi_begin_draft_reply",
        lexi.begin_draft_reply_action,
        proposal_id=pid,
        voice_mode=voice_mode.strip() or "kory",
    )


@mcp.tool()
def lexi_get_scheduling_context(proposal_id: str) -> str:
    """Facts packet for a proposal: thread, meeting type, slots, rules, voice — for Hermes compose."""
    try:
        pid = int(proposal_id)
    except ValueError:
        return _error("proposal_id must be an integer string.", code="validation_error")
    return _wrap(
        "lexi_get_scheduling_context",
        lexi.get_scheduling_context_action,
        proposal_id=pid,
    )


@mcp.tool()
def lexi_escalate_to_heidi(proposal_id: str, reason: str = "") -> str:
    """Email Heidi when scheduling cannot be completed (issue + briefing). Staged when LEXI_DRY_RUN=true."""
    try:
        pid = int(proposal_id)
    except ValueError:
        return _error("proposal_id must be an integer string.", code="validation_error")
    return _wrap(
        "lexi_escalate_to_heidi",
        lexi.escalate_to_heidi_action,
        proposal_id=pid,
        reason=(reason or "").strip(),
    )


@mcp.tool()
def lexi_retry_scheduling(proposal_id: str, guidance: str) -> str:
    """Re-search times after Kory gives a different week/window. Reply to Kory with kory_message only — one or two plain sentences."""
    try:
        pid = int(proposal_id)
    except ValueError:
        return _error("proposal_id must be an integer string.", code="validation_error")
    if not (guidance or "").strip():
        return _error("guidance is required.", code="validation_error")
    return _wrap(
        "lexi_retry_scheduling",
        lexi.retry_scheduling_with_guidance_action,
        proposal_id=pid,
        guidance=guidance.strip(),
    )


@mcp.tool()
def lexi_begin_reoffer(proposal_id: str) -> str:
    """After recipient declined offered times: find new slots and stage approval card."""
    try:
        pid = int(proposal_id)
    except ValueError:
        return _error("proposal_id must be an integer string.", code="validation_error")
    return _wrap("lexi_begin_reoffer", lexi.begin_reoffer_action, proposal_id=pid)


@mcp.tool()
def lexi_recipient_timezone(sender_email: str = "", body: str = "") -> str:
    """Detect recipient timezone from domain, body cues, or email headers (never assumes)."""
    return _wrap(
        "lexi_recipient_timezone",
        lexi.recipient_timezone_action,
        sender_email=sender_email,
        body=body,
    )


@mcp.tool()
def lexi_decline_inbound_reply(proposal_id: str, reason: str = "") -> str:
    """Kory declined to draft a reply for this inbound email."""
    try:
        pid = int(proposal_id)
    except ValueError:
        return _error("proposal_id must be an integer string.", code="validation_error")
    return _wrap(
        "lexi_decline_inbound_reply",
        lexi.decline_inbound_reply_action,
        proposal_id=pid,
        reason=reason,
    )


@mcp.tool()
def lexi_update_proposal_draft(proposal_id: str, drafted_reply: str) -> str:
    """Apply Kory's edits to a staged draft before send."""
    try:
        pid = int(proposal_id)
    except ValueError:
        return _error("proposal_id must be an integer string.", code="validation_error")
    return _wrap(
        "lexi_update_proposal_draft",
        lexi.update_proposal_draft_action,
        proposal_id=pid,
        drafted_reply=drafted_reply,
    )


@mcp.tool()
def lexi_list_calendars(role: str = "read") -> str:
    """List Outlook calendars by name (read=Kory, write=pilot/production mailbox).

    Use aliases from config/calendars.yaml: team, ifg, master, deals, heidi, etc.
    """
    return _wrap("lexi_list_calendars", lexi.list_calendars, role=role)


@mcp.tool()
def lexi_add_conflict_calendar(calendar_name: str) -> str:
    """Add an Outlook calendar to Lexi's busy/free conflict list (updates config/calendars.yaml).

    Use when Kory says e.g. "I added a new calendar — include it for scheduling."
    Calls lexi_list_calendars first if the exact name is unknown.
    """
    return _wrap(
        "lexi_add_conflict_calendar",
        lexi.add_conflict_calendar,
        calendar_name=calendar_name,
    )


@mcp.tool()
def lexi_preview_scheduling_email() -> str:
    """Show an example scheduling reply with correct Kory formatting (TZ + sign-off)."""
    return _wrap("lexi_preview_scheduling_email", lexi.preview_scheduling_email_example)


@mcp.tool()
def lexi_get_system_status() -> str:
    """Lexi runtime status (internal). Tell Kory only the kory_brief field — never outlook_timezone or connection IDs."""
    from app.worker.runner import is_worker_running

    def _status() -> dict[str, Any]:
        base = lexi.get_lexi_system_status()
        base["worker_running"] = is_worker_running()
        base["teams_mode"] = "hermes_only"
        from app.bot.teams_conversation_store import teams_delivery_ready

        base["teams_cards_ready"] = teams_delivery_ready()
        return base

    return _wrap("lexi_get_system_status", _status)


@mcp.tool()
def lexi_inbox_review(hours: str = "48") -> str:
    """48-hour inbox activity summary + open action items for Kory. Trigger: Kory says 'inbox review'."""
    try:
        window = max(1, min(168, int(hours.strip() or "48")))
    except ValueError:
        window = 48
    return _wrap("lexi_inbox_review", lexi.inbox_review_action, hours=window)


@mcp.tool()
def lexi_unanswered_brief(hours: str = "72") -> str:
    """Emails Kory may still need to reply to. Teams shortcut: `unanswered`."""
    try:
        window = max(1, min(168, int(hours.strip() or "72")))
    except ValueError:
        window = 72
    return _wrap("lexi_unanswered_brief", lexi.unanswered_brief_action, hours=window)


@mcp.tool()
def lexi_today_calendar() -> str:
    """Today's calendar for Kory. Teams shortcut: `today`."""
    return _wrap("lexi_today_calendar", lexi.today_calendar_brief_action)


@mcp.tool()
def lexi_prebrief(include_research: str = "false") -> str:
    """List today's meetings so Kory can choose one to be briefed on. Fast.

    Shows each meeting with its external attendees. Does NOT research anyone —
    call lexi_meeting_brief or lexi_precall_brief once he names one. Teams
    shortcut: `prebrief`.
    """
    return _wrap("lexi_prebrief", lexi.prebrief_action)


@mcp.tool()
def lexi_meeting_brief(meeting: str) -> str:
    """Full pre-call brief on EVERY external attendee of one meeting.

    Triggers: "prebrief my 2pm", "brief me on the ACCU call", "who am I meeting
    with at 3 and what should I know", "prep me for my next meeting".

    Accepts part of the meeting subject, an attendee's name, or "next". Takes
    ~15 seconds per attendee, so it is scoped to one meeting rather than the
    whole day. For a person who is not on today's calendar, use
    lexi_precall_brief.
    """
    return _wrap("lexi_meeting_brief", lexi.meeting_brief_action, meeting=meeting)


@mcp.tool()
def lexi_precall_brief(person: str) -> str:
    """Full pre-call brief on ONE person, by name or email address.

    Triggers: "prebrief me on <person>", "pre-call brief for <person>",
    "brief me before my call with <person>", "what should I know before
    meeting <person>".

    Covers who they are and what they do from web research, how Kory knows them
    from his own mailbox, who introduced them, and the angle for the call. Use
    this rather than lexi_lookup_person when Kory is preparing for a meeting —
    lookup_person is the quick CRM record, this is the full brief.
    """
    return _wrap("lexi_precall_brief", lexi.precall_brief_action, person=person)


@mcp.tool()
def lexi_today() -> str:
    """The current date and time in Kory's timezone (America/Denver).

    Call this before stating today's date or resolving any relative date
    ("next week", "August 10"). Never answer date questions from memory.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = datetime.now(tz=ZoneInfo("America/Denver"))
    return _ok(
        {
            "action": "lexi_today",
            "result": {
                "date": now.strftime("%Y-%m-%d"),
                "weekday": now.strftime("%A"),
                "time": now.strftime("%H:%M"),
                "timezone": "America/Denver (MT)",
                "year": now.year,
            },
        }
    )


@mcp.tool()
def lexi_list_asana_projects() -> str:
    """Every Asana project Lexi can read for Kory (reads span all of them).

    Use this before saying which projects are visible. Tasks are created on
    Kory's personal project only; reads cover all of these.
    """
    return _wrap("lexi_list_asana_projects", lexi.list_asana_projects_action)


@mcp.tool()
def lexi_list_asana_boards() -> str:
    """Boards (sections) available for new tasks — General, Personal, YPO, etc."""
    return _wrap("lexi_list_asana_boards", lexi.list_asana_boards_action)


@mcp.tool()
def lexi_list_asana_tasks(bucket: str = "due_today", who: str = "kory") -> str:
    """List Asana tasks: overdue | due_today | upcoming | all | completed (read-only).

    Defaults to Kory's own tasks. Shared boards hold other people's work, so
    pass who='all' only when he asks about the team — and say whose a task is
    when reporting anyone else's.
    """
    return _wrap(
        "lexi_list_asana_tasks",
        lexi.list_asana_tasks_action,
        bucket=bucket,
        mine_only=who.strip().lower() not in {"all", "everyone", "team"},
    )


@mcp.tool()
def lexi_create_asana_task(
    title: str,
    notes: str = "",
    due_on: str = "",
    section: str = "",
    confirm: str = "false",
) -> str:
    """Create an Asana task on Kory's project. Pass confirm='true' once Kory has asked for or agreed to this; if it returns confirmation_required, ask him and retry — never report it as unsupported."""
    approved = confirm.strip().lower() in {"1", "true", "yes"}
    return _wrap(
        "lexi_create_asana_task",
        lexi.create_asana_task_action,
        title=title,
        notes=notes,
        due_on=due_on,
        section=section,
        confirm=approved,
    )


@mcp.tool()
def lexi_complete_asana_task(task_gid: str, confirm: str = "false",
    owner_ack: str = "false",
) -> str:
    """Mark an Asana task complete. Pass confirm='true' once Kory has asked for or agreed to this; if it returns confirmation_required, ask him and retry — never report it as unsupported."""
    approved = confirm.strip().lower() in {"1", "true", "yes"}
    return _wrap(
        "lexi_complete_asana_task",
        lexi.complete_asana_task_action,
        task_gid=task_gid,
        confirm=approved,
        owner_ack=owner_ack.strip().lower() in {"1", "true", "yes"},
    )


@mcp.tool()
def lexi_update_asana_task(
    task_gid: str,
    title: str = "",
    notes: str = "",
    due_on: str = "",
    confirm: str = "false",
    owner_ack: str = "false",
) -> str:
    """Update an Asana task's title, notes, or due date. Due dates must be absolute (YYYY-MM-DD) and in the future — resolve relative dates against the current year. Pass confirm='true' once Kory has asked for or agreed to this; if it returns confirmation_required, ask him and retry — never report it as unsupported."""
    approved = confirm.strip().lower() in {"1", "true", "yes"}
    return _wrap(
        "lexi_update_asana_task",
        lexi.update_asana_task_action,
        task_gid=task_gid,
        title=title,
        notes=notes,
        due_on=due_on,
        confirm=approved,
        owner_ack=owner_ack.strip().lower() in {"1", "true", "yes"},
    )


@mcp.tool()
def lexi_delete_asana_task(task_gid: str, confirm: str = "false",
    owner_ack: str = "false",
) -> str:
    """Delete an Asana task. Pass confirm='true' once Kory has asked for or agreed to this; if it returns confirmation_required, ask him and retry — never report it as unsupported."""
    approved = confirm.strip().lower() in {"1", "true", "yes"}
    return _wrap(
        "lexi_delete_asana_task",
        lexi.delete_asana_task_action,
        task_gid=task_gid,
        confirm=approved,
        owner_ack=owner_ack.strip().lower() in {"1", "true", "yes"},
    )


@mcp.tool()
def lexi_search_asana_tasks(query: str) -> str:
    """Search Asana tasks by name across NON-IFG + related projects (read-only)."""
    return _wrap("lexi_search_asana_tasks", lexi.search_asana_tasks_action, query=query)


@mcp.tool()
def lexi_move_asana_task(
    task_gid: str,
    section_gid: str = "",
    section_name: str = "",
    confirm: str = "false",
) -> str:
    """Move Asana task to a section after confirm (writes blocked for now)."""
    approved = confirm.strip().lower() in {"1", "true", "yes"}
    return _wrap(
        "lexi_move_asana_task",
        lexi.move_asana_task_action,
        task_gid=task_gid,
        section_gid=section_gid,
        section_name=section_name,
        confirm=approved,
    )


@mcp.tool()
def lexi_comment_asana_task(task_gid: str, comment: str, confirm: str = "false",
    owner_ack: str = "false",
) -> str:
    """Add a comment to an Asana task. Pass confirm='true' once Kory has asked for or agreed to this; if it returns confirmation_required, ask him and retry — never report it as unsupported."""
    approved = confirm.strip().lower() in {"1", "true", "yes"}
    return _wrap(
        "lexi_comment_asana_task",
        lexi.comment_asana_task_action,
        task_gid=task_gid,
        comment=comment,
        confirm=approved,
        owner_ack=owner_ack.strip().lower() in {"1", "true", "yes"},
    )


@mcp.tool()
def lexi_hubspot_status() -> str:
    """HubSpot connection status (read-only sample)."""
    return _wrap("lexi_hubspot_status", lexi.hubspot_status_action)


@mcp.tool()
def lexi_hubspot_cleanup_proposals(inactive_days: str = "180") -> str:
    """CRM health report for Kory's contacts: where the book is thin. Read-only."""
    try:
        days = int(inactive_days.strip() or "180")
    except ValueError:
        days = 180
    return _wrap("lexi_hubspot_cleanup_proposals", lexi.hubspot_cleanup_proposals_action, inactive_days=days)


@mcp.tool()
def lexi_hubspot_health_report(all_owners: str = "false") -> str:
    """Where Kory's CRM is incomplete — counts are portal-wide, not a sample. Read-only."""
    every = all_owners.strip().lower() in {"1", "true", "yes"}
    return _wrap("lexi_hubspot_health_report", lexi.hubspot_health_report_action, all_owners=every)


@mcp.tool()
def lexi_hubspot_find_contacts(
    company: str = "",
    quiet_days: str = "0",
    lifecycle: str = "",
    limit: str = "25",
    all_owners: str = "false",
) -> str:
    """Find groups of contacts by company or by how long since Kory spoke to them.

    Triggers: "show me everyone at <company>", "who do I know at <company>",
    "who haven't I talked to in <N> months/a year", "who has gone quiet".

    Pass quiet_days for silence questions (a year = 365). Opt-outs are shown
    but labelled Do Not Contact. Read-only.
    """
    try:
        quiet = max(0, int(quiet_days.strip() or "0"))
    except ValueError:
        quiet = 0
    try:
        n = max(1, min(100, int(limit.strip() or "25")))
    except ValueError:
        n = 25
    return _wrap(
        "lexi_hubspot_find_contacts",
        lexi.hubspot_find_contacts_action,
        company=company,
        quiet_days=quiet,
        lifecycle=lifecycle,
        limit=n,
        all_owners=all_owners.strip().lower() in {"1", "true", "yes"},
    )


@mcp.tool()
def lexi_hubspot_compare_books() -> str:
    """Compare Kory's contact book against the other IFG owners. Read-only."""
    return _wrap("lexi_hubspot_compare_books", lexi.hubspot_compare_books_action)


@mcp.tool()
def lexi_hubspot_recent_changes(days: str = "7") -> str:
    """What changed in HubSpot lately: new contacts and deal stage movements. Read-only."""
    try:
        n = max(1, min(90, int(days.strip() or "7")))
    except ValueError:
        n = 7
    return _wrap("lexi_hubspot_recent_changes", lexi.hubspot_recent_changes_action, days=n)


@mcp.tool()
def lexi_hubspot_outreach_batch(goal: str = "", limit: str = "10") -> str:
    """Draft outreach emails for a HubSpot batch — approval required before send."""
    try:
        n = max(1, min(25, int(limit.strip() or "10")))
    except ValueError:
        n = 10
    return _wrap("lexi_hubspot_outreach_batch", lexi.hubspot_outreach_batch_action, goal=goal, limit=n)


@mcp.tool()
def lexi_hubspot_duplicate_merges(limit: str = "50") -> str:
    """Propose duplicate contact merges — staged only; HubSpot writes blocked for now."""
    try:
        n = max(1, min(100, int(limit.strip() or "50")))
    except ValueError:
        n = 50
    return _wrap("lexi_hubspot_duplicate_merges", lexi.hubspot_duplicate_merges_action, limit=n)


@mcp.tool()
def lexi_hubspot_enrich_contacts(limit: str = "25") -> str:
    """Propose job title/company fills from Kory's own email signatures.

    Blank fields only — an existing value is never overwritten. Staged for
    approval; nothing is written to HubSpot while writes are blocked.
    """
    try:
        n = max(1, min(50, int(limit.strip() or "25")))
    except ValueError:
        n = 25
    return _wrap("lexi_hubspot_enrich_contacts", lexi.hubspot_enrichment_action, limit=n)


@mcp.tool()
def lexi_lookup_person(name: str = "", email: str = "") -> str:
    """Who is this person? Use for ANY question about a specific individual.

    Triggers: "tell me about <person>", "who is <person>", "look up <person>",
    "what do we know about <person>", "before my call with <person>".

    Returns their job title, company, relationship stage, lead status, when Kory
    last spoke to them, and any open deal. Also flags contacts marked **Do Not
    Contact** — that warning appears nowhere else, so check here before saying
    anything about reaching out to someone. Read-only.

    Call this BEFORE searching the inbox: the inbox shows recent messages, this
    shows who the person actually is. Use both when Kory wants recent context too.
    """
    return _wrap(
        "lexi_lookup_person",
        lexi.hubspot_prebrief_enrich_action,
        email=email,
        name=name,
    )


@mcp.tool()
def lexi_hubspot_prebrief_enrich(email: str = "", name: str = "") -> str:
    """Alias of lexi_lookup_person, kept for existing prebrief shortcuts."""
    return _wrap(
        "lexi_hubspot_prebrief_enrich",
        lexi.hubspot_prebrief_enrich_action,
        email=email,
        name=name,
    )


@mcp.tool()
def lexi_hubspot_meeting_note(
    email: str,
    note: str,
    meeting_subject: str = "",
    confirm: str = "false",
    owner_ack: str = "false",
) -> str:
    """Stage a HubSpot note after a meeting (writes blocked for now).

    If the contact belongs to another IFG owner this returns
    owner_confirmation_required naming them; re-call with owner_ack=true only
    after Kory confirms he means to touch someone else's record.
    """
    approved = confirm.strip().lower() in {"1", "true", "yes"}
    ack = owner_ack.strip().lower() in {"1", "true", "yes"}
    return _wrap(
        "lexi_hubspot_meeting_note",
        lexi.hubspot_meeting_note_action,
        email=email,
        note=note,
        meeting_subject=meeting_subject,
        confirm=approved,
        owner_ack=ack,
    )


@mcp.tool()
def lexi_hubspot_outreach_candidates(
    goal: str = "",
    lifecycle: str = "",
    limit: str = "15",
) -> str:
    """Find HubSpot contacts for outreach (read-only filter)."""
    try:
        n = max(1, min(40, int(limit.strip() or "15")))
    except ValueError:
        n = 15
    return _wrap(
        "lexi_hubspot_outreach_candidates",
        lexi.hubspot_outreach_candidates_action,
        goal=goal,
        lifecycle=lifecycle,
        limit=n,
    )


@mcp.tool()
def lexi_hubspot_deals_snapshot(limit: str = "8") -> str:
    """Open HubSpot deals for CEO briefing (read-only)."""
    try:
        n = max(1, min(25, int(limit.strip() or "8")))
    except ValueError:
        n = 8
    return _wrap("lexi_hubspot_deals_snapshot", lexi.hubspot_deals_snapshot_action, limit=n)


@mcp.tool()
def lexi_create_outreach_campaign(
    name: str,
    goal: str = "",
    template_key: str = "generic",
    pasted_list: str = "",
    hubspot_limit: str = "0",
    hubspot_lifecycle: str = "",
    include_research: str = "false",
    custom_opener: str = "",
    custom_subject: str = "",
) -> str:
    """Start a mass outreach campaign: personalize drafts and stage them (no send, no Teams cards).

    Templates: ypo_the_turn | generic. Paste Name,email,company lines and/or set hubspot_limit.
    Drafts stay local until LEXI_OUTREACH_OUTLOOK_DRAFTS_ENABLED; sends stay blocked.
    """
    try:
        hs_limit = max(0, min(100, int((hubspot_limit or "0").strip() or "0")))
    except ValueError:
        hs_limit = 0
    research = (include_research or "").strip().lower() in {"1", "true", "yes"}
    return _wrap(
        "lexi_create_outreach_campaign",
        lexi.create_outreach_campaign_action,
        name=name,
        goal=goal,
        template_key=template_key,
        pasted_list=pasted_list,
        hubspot_limit=hs_limit,
        hubspot_lifecycle=hubspot_lifecycle,
        include_research=research,
        custom_opener=custom_opener,
        custom_subject=custom_subject,
    )


@mcp.tool()
def lexi_list_outreach_campaigns(limit: str = "20") -> str:
    """List staged/approved outreach campaigns."""
    try:
        n = max(1, min(50, int((limit or "20").strip() or "20")))
    except ValueError:
        n = 20
    return _wrap("lexi_list_outreach_campaigns", lexi.list_outreach_campaigns_action, limit=n)


@mcp.tool()
def lexi_get_outreach_campaign(campaign_id: str) -> str:
    """Show one outreach campaign and its staged drafts (text only)."""
    return _wrap(
        "lexi_get_outreach_campaign",
        lexi.get_outreach_campaign_action,
        campaign_id=campaign_id,
    )


@mcp.tool()
def lexi_approve_outreach_campaign(campaign_id: str, confirm: str = "false") -> str:
    """Mark campaign approved. Does not send while LEXI_OUTREACH_LIVE_SENDS_ENABLED is false."""
    ok = (confirm or "").strip().lower() in {"1", "true", "yes"}
    return _wrap(
        "lexi_approve_outreach_campaign",
        lexi.approve_outreach_campaign_action,
        campaign_id=campaign_id,
        confirm=ok,
    )


@mcp.tool()
def lexi_send_outreach_campaign(campaign_id: str, confirm: str = "false") -> str:
    """Send approved outreach drafts — hard-blocked for UAT (returns dry-run message)."""
    ok = (confirm or "").strip().lower() in {"1", "true", "yes"}
    return _wrap(
        "lexi_send_outreach_campaign",
        lexi.send_outreach_campaign_action,
        campaign_id=campaign_id,
        confirm=ok,
    )


@mcp.tool()
def lexi_remove_outreach_recipient(campaign_id: str, email: str) -> str:
    """Remove one recipient from a staged outreach campaign."""
    return _wrap(
        "lexi_remove_outreach_recipient",
        lexi.remove_outreach_recipient_action,
        campaign_id=campaign_id,
        email=email,
    )


@mcp.tool()
def lexi_register_teams_conversation(
    conversation_id: str,
    service_url: str = "",
) -> str:
    """Save Teams conversation id for proactive approval cards (call after Kory DMs the bot).

    Hermes: call on first message in a session, or when Kory runs /sethome.
    Requires TEAMS_CLIENT_ID + TEAMS_CLIENT_SECRET in project .env for card delivery.
    """
    from app.bot.teams_conversation_store import save_conversation_reference, teams_delivery_ready

    if not conversation_id.strip():
        return _error("conversation_id is required.", code="validation_error")
    record = save_conversation_reference(conversation_id, service_url=service_url)
    return _ok(
        {
            "action": "lexi_register_teams_conversation",
            "saved": record,
            "teams_cards_ready": teams_delivery_ready(),
        }
    )


@mcp.tool()
def lexi_handle_teams_command(text: str, authorized_by: str = "kory") -> str:
    """Execute Lexi Teams commands from Hermes chat (approve, reject, draft, pending).

    Call when Kory sends card button text or short commands, e.g.
    'Send reply to Dan Smith — Project Paint', 'Draft reply to …', 'pending', 'inbound'.
    """
    from app.teams.commands import handle_teams_command

    result = handle_teams_command(text, authorized_by=authorized_by.strip() or "kory")
    return json.dumps({"ok": result.get("ok", False), **result}, default=str)


@mcp.tool()
def lexi_handle_teams_card_submit(payload_json: str, authorized_by: str = "kory") -> str:
    """Process an editable approval Adaptive Card submit (draft edits + Send/Discard/Save).

    payload_json is the card Action.Submit data object (includes drafted_reply input).
    """
    from app.teams.commands import handle_teams_card_submit

    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError as exc:
        return _error(f"payload_json invalid: {exc}", code="validation_error")
    if not isinstance(payload, dict):
        return _error("payload_json must be a JSON object.", code="validation_error")
    result = handle_teams_card_submit(payload, authorized_by=authorized_by.strip() or "kory")
    return json.dumps({"ok": result.get("ok", False), **result}, default=str)


@mcp.tool()
def lexi_get_calendar_availability(days: str = "0") -> str:
    """Quick busy/free read (internal). For day-by-day week summaries use lexi_summarize_calendar_window."""
    try:
        window = int(days)
    except ValueError:
        return _error("days must be an integer string, e.g. '60'.", code="validation_error")
    return _wrap("lexi_get_calendar_availability", lexi.get_calendar_availability, days=window)


@mcp.tool()
def lexi_summarize_calendar_window(query: str) -> str:
    """Day-by-day calendar summary from live Master + work Calendar (read-only).

    Pass Kory's full ask, e.g. 'summarize my full calendar for next week'.
    Returns formatted_summary with correct dates and real events — relay to Kory; do not invent times.
    """
    return _wrap("lexi_summarize_calendar_window", lexi.summarize_calendar_window, query=query.strip())


@mcp.tool()
def lexi_check_time_slot(start_iso: str, end_iso: str) -> str:
    """Check if a start/end ISO interval conflicts with Kory's calendar."""
    return _wrap(
        "lexi_check_time_slot",
        lexi.check_time_slot,
        start_iso=start_iso,
        end_iso=end_iso,
    )


@mcp.tool()
def lexi_place_calendar_hold(
    title: str,
    start_iso: str,
    end_iso: str,
    attendee_email: str = "",
    location: str = "TBD",
    notes: str = "",
    calendar_name: str = "",
    confirm: str = "false",
) -> str:
    """Place a tentative calendar hold (e.g. 'IFG Team', 'Kory Master Calendar (ALL)').

    calendar_name: optional alias — team, ifg, master, deals, heidi, ceo_daily.
    confirm must be 'true' after Kory explicitly approves in Teams chat.
    """
    confirmed = confirm.strip().lower() in {"true", "yes", "1"}
    return _wrap(
        "lexi_place_calendar_hold",
        lexi.place_calendar_hold,
        title=title,
        start_iso=start_iso,
        end_iso=end_iso,
        attendee_email=attendee_email,
        location=location,
        notes=notes,
        calendar_name=calendar_name,
        confirm=confirmed,
    )


@mcp.tool()
def lexi_draft_outbound_email(
    to_email: str,
    subject: str,
    body: str,
    send_channel: str = "",
) -> str:
    """Preview an outbound email without sending.

    send_channel: kory (Kory's voice / mailbox) or lexi (Lexi assistant). Leave blank to infer from sign-off.
    After Kory approves, call lexi_send_outbound_email with the same send_channel.
    """
    return _wrap(
        "lexi_draft_outbound_email",
        lexi.draft_outbound_email_preview,
        to_email=to_email,
        subject=subject,
        body=body,
        send_channel=send_channel.strip(),
    )


@mcp.tool()
def lexi_send_outbound_email(
    to_email: str,
    subject: str,
    body: str,
    confirm_send: str,
    authorized_by: str = "kory",
    send_channel: str = "",
) -> str:
    """Send outbound email only after Kory explicitly approves.

    send_channel: kory or lexi — must match the draft preview. Leave blank to infer from sign-off.
    confirm_send must be 'true' or 'yes' (case insensitive).
    """
    confirmed = confirm_send.strip().lower() in {"true", "yes", "1"}
    return _wrap(
        "lexi_send_outbound_email",
        lexi.send_outbound_email_confirmed,
        to_email=to_email,
        subject=subject,
        body=body,
        confirm_send=confirmed,
        authorized_by=authorized_by,
        send_channel=send_channel,
    )


@mcp.tool()
def lexi_create_reservation_reminder(
    meeting_subject: str,
    time_slot: str = "",
    notes: str = "",
    meal: str = "",
    confirm: str = "false",
) -> str:
    """Create a reservation reminder on Kory NON-IFG → Reservation Reminders (Asana).

    Use when Kory asks to book lunch/dinner. confirm must be 'true' after Kory approves in chat.
    """
    from app.integrations.asana_manager import create_booking_reminder_task

    confirmed = confirm.strip().lower() in {"true", "yes", "1"}
    meal_kind = (meal or "dinner").strip().lower()
    if meal_kind not in {"lunch", "dinner"}:
        meal_kind = "dinner"
    body = notes.strip()
    if time_slot.strip():
        body = f"Time slot: {time_slot.strip()}\n{body}".strip()
    return _wrap(
        "lexi_create_reservation_reminder",
        create_booking_reminder_task,
        meal=meal_kind,
        meeting_subject=meeting_subject,
        thread_id="hermes-manual",
        sender="kory",
        body_excerpt=body,
        approved=confirmed,
    )


@mcp.tool()
def lexi_search_inbox(query: str = "", top: str = "10") -> str:
    """Search Kory's Outlook inbox (read-only). Use before drafting replies."""
    try:
        limit = int(top)
    except ValueError:
        return _error("top must be an integer string.", code="validation_error")
    return _wrap("lexi_search_inbox", lexi.search_inbox, query=query, top=limit)


@mcp.tool()
def lexi_get_thread(message_id: str) -> str:
    """Fetch a single Kory inbox message by Outlook message id."""
    return _wrap("lexi_get_thread", lexi.get_email_thread, message_id=message_id)


@mcp.tool()
def lexi_find_slots(
    subject: str,
    body: str,
    intent: str = "",
    meeting_format: str = "",
    sender_email: str = "",
) -> str:
    """Find rule-valid meeting slots on Kory's calendar (unified schedule_from_context).

    MANDATORY for every chat request to propose times — never guess slots in prose.
    Put the full natural-language request in body (e.g. 'next week Tue-Thu 10am-4pm MT, 30-min virtual intro').
    sender_email improves timezone detection when known.
    """
    return _wrap(
        "lexi_find_slots",
        lexi.find_slots_for_request,
        subject=subject.strip(),
        body=body.strip(),
        intent=intent.strip(),
        meeting_format=meeting_format.strip(),
        sender_email=sender_email.strip(),
    )


@mcp.tool()
def lexi_preview_schedule(
    subject: str,
    body: str,
    sender_email: str = "",
    intent: str = "",
) -> str:
    """Dry-run scheduling + template draft (no send, no holds). Same engine as inbound email."""
    from app.scheduling.hermes_orchestrator import preview_scheduling_draft

    return _wrap(
        "lexi_preview_schedule",
        preview_scheduling_draft,
        subject=subject.strip(),
        body=body.strip(),
        sender_email=sender_email.strip() or None,
        intent=intent.strip() or None,
    )


@mcp.tool()
def lexi_propose_schedule(
    subject: str,
    body: str,
    sender: str = "unknown@example.com",
    thread_id: str = "",
) -> str:
    """Run full inbound triage + scheduler for a scheduling email (unified propose_schedule)."""
    return _wrap(
        "lexi_propose_schedule",
        lexi.run_propose_schedule,
        subject=subject,
        body=body,
        sender=sender,
        thread_id=thread_id,
    )


@mcp.tool()
def lexi_validate_slots(slots_json: str, intent: str = "") -> str:
    """Validate ISO slots against Kory rules AND the live calendar."""
    try:
        slots = json.loads(slots_json)
    except json.JSONDecodeError as exc:
        return _error(f"slots_json must be a JSON array: {exc}", code="validation_error")
    if not isinstance(slots, list):
        return _error("slots_json must be a JSON array.", code="validation_error")
    return _wrap(
        "lexi_validate_slots",
        lexi.validate_slots_preview,
        slots=slots,
        intent=intent,
    )


@mcp.tool()
def lexi_validate_scheduling_cases(preset: str = "", cases_json: str = "[]") -> str:
    """MANDATORY for 'validate these slots' chat asks — live calendar + Kory rules per slot.

    Use preset=july_slot_check for the standard July rules test, OR pass cases_json as a JSON array:
    [{"label":"...", "intent":"coffee", "start_iso":"2026-07-07T08:30:00-06:00", "meeting_format":"in_person"}, ...]

    Returns formatted_summary — relay to Kory verbatim; do not re-validate in prose.
    """
    try:
        cases = json.loads(cases_json or "[]")
    except json.JSONDecodeError as exc:
        return _error(f"cases_json must be a JSON array: {exc}", code="validation_error")
    if cases_json.strip() and not isinstance(cases, list):
        return _error("cases_json must be a JSON array.", code="validation_error")
    return _wrap(
        "lexi_validate_scheduling_cases",
        lexi.validate_scheduling_cases_action,
        preset=preset.strip(),
        cases=cases if cases else None,
    )


@mcp.tool()
def lexi_get_scheduling_session(session_id: str) -> str:
    """Load a multi-turn Hermes scheduling session."""
    return _wrap("lexi_get_scheduling_session", lexi.get_scheduling_session, session_id=session_id)


@mcp.tool()
def lexi_upsert_scheduling_session(
    session_id: str = "",
    channel: str = "hermes",
    context_json: str = "{}",
    status: str = "",
) -> str:
    """Create or update a scheduling session for interrupted Hermes flows."""
    try:
        context = json.loads(context_json or "{}")
    except json.JSONDecodeError as exc:
        return _error(f"context_json invalid: {exc}", code="validation_error")
    if not isinstance(context, dict):
        return _error("context_json must be a JSON object.", code="validation_error")
    kwargs: dict[str, Any] = {
        "session_id": session_id,
        "channel": channel,
        "context": context,
    }
    if status.strip():
        kwargs["status"] = status.strip()
    return _wrap("lexi_upsert_scheduling_session", lexi.upsert_scheduling_session, **kwargs)


@mcp.tool()
def lexi_start_scheduling(
    recipient_email: str,
    subject: str,
    meeting_intent: str,
    duration_minutes: str,
    authorized_by: str = "kory",
    require_ceo_signoff: str = "true",
) -> str:
    """Start outbound scheduling: LLM slots + draft + holds + pending_approval.

    meeting_intent examples: lunch, dinner, coffee, meeting, internal_sync.
    duration_minutes: e.g. '60' for lunch. Set require_ceo_signoff=false only if Kory
    asked to send immediately without sign-off.
    """
    try:
        duration = int(duration_minutes)
    except ValueError:
        return _error("duration_minutes must be an integer string.", code="validation_error")
    from app.safety.approval_gate import immediate_send_allowed, kory_approves_all

    signoff = require_ceo_signoff.strip().lower() in {"true", "yes", "1"}
    if kory_approves_all() and not immediate_send_allowed():
        signoff = True
    return _wrap(
        "lexi_start_scheduling",
        lexi.start_outbound_scheduling,
        recipient_email=recipient_email,
        subject=subject,
        meeting_intent=meeting_intent,
        duration_minutes=duration,
        authorized_by=authorized_by,
        require_ceo_signoff=signoff,
    )


# ── Outlook scheduling actions (Composio SDK behind Lexi — not Composio MCP) ──


@mcp.tool()
def lexi_execute_outlook_action(
    slug: str,
    arguments_json: str = "{}",
    confirm: str = "false",
    send_channel: str = "kory",
) -> str:
    """Run one Outlook Composio slug (allowlisted). confirm=true for writes. Blocked in read-only UAT."""
    confirmed = confirm.strip().lower() in {"true", "yes", "1"}
    return _wrap(
        "lexi_execute_outlook_action",
        lexi.execute_outlook_action_action,
        slug=slug,
        arguments_json=arguments_json,
        confirm=confirmed,
        send_channel=send_channel,
    )


@mcp.tool()
def lexi_accept_calendar_invite(event_id: str) -> str:
    """Accept a calendar invite on Kory's calendar (read-only UAT: dry-run blocked)."""
    return _wrap("lexi_accept_calendar_invite", lexi.accept_calendar_invite_action, event_id=event_id)


@mcp.tool()
def lexi_decline_calendar_invite(event_id: str, comment: str = "") -> str:
    """Decline a calendar invite (blocked in read-only UAT)."""
    return _wrap(
        "lexi_decline_calendar_invite",
        lexi.decline_calendar_invite_action,
        event_id=event_id,
        comment=comment,
    )


@mcp.tool()
def lexi_find_meeting_times(payload_json: str) -> str:
    """Find meeting times (internal). Tell Kory only whether times exist — never mention Outlook API or engine details."""
    return _wrap("lexi_find_meeting_times", lexi.find_meeting_times_action, payload_json=payload_json)


@mcp.tool()
def lexi_get_thread_context(conversation_id: str, exclude_message_id: str = "") -> str:
    """Load prior messages in an Outlook conversation for accurate replies."""
    return _wrap(
        "lexi_get_thread_context",
        lexi.get_thread_context_action,
        conversation_id=conversation_id,
        exclude_message_id=exclude_message_id,
    )


@mcp.tool()
def lexi_remember_kory_fact(fact_key: str, fact_value: str) -> str:
    """Save an explicit long-term preference Kory stated (not chat thread memory)."""
    return _wrap(
        "lexi_remember_kory_fact",
        lexi.remember_kory_fact_action,
        fact_key=fact_key,
        fact_value=fact_value,
    )


@mcp.tool()
def lexi_list_kory_memory() -> str:
    """List saved Kory facts (long-term memory)."""
    return _wrap("lexi_list_kory_memory", lexi.list_kory_memory_action)


# ── Composio Search (web, travel, maps — read-only; uses COMPOSIO_API_KEY) ───


@mcp.tool()
def lexi_web_search(query: str) -> str:
    """Search the web for venues, restaurants, travel info, research. Read-only."""
    return _wrap("lexi_web_search", lexi.web_search_action, query=query)


@mcp.tool()
def lexi_search_flights(query: str = "", payload_json: str = "{}") -> str:
    """Search flights. Use query='Denver to NYC March 15' or JSON with departure_id/arrival_id/outbound_date."""
    return _wrap(
        "lexi_search_flights",
        lexi.search_flights_action,
        query=query,
        payload_json=payload_json,
    )


@mcp.tool()
def lexi_search_hotels(payload_json: str) -> str:
    """Search hotels. JSON: q, check_in_date, check_out_date, adults (required: q)."""
    return _wrap("lexi_search_hotels", lexi.search_hotels_action, payload_json=payload_json)


@mcp.tool()
def lexi_search_maps(query: str) -> str:
    """Google Maps search for venues, restaurants, addresses near a location."""
    return _wrap("lexi_search_maps", lexi.search_maps_action, query=query)


@mcp.tool()
def lexi_search_news(query: str) -> str:
    """News search for context on meetings, companies, events."""
    return _wrap("lexi_search_news", lexi.search_news_action, query=query)


@mcp.tool()
def lexi_fetch_url_content(url: str, max_characters: int = 8000) -> str:
    """Fetch readable text from a public URL (docs, venue pages, articles)."""
    return _wrap(
        "lexi_fetch_url_content",
        lexi.fetch_url_content_action,
        url=url,
        max_characters=max_characters,
    )


@mcp.tool()
def lexi_execute_search_action(slug: str, arguments_json: str = "{}") -> str:
    """Run any allowlisted COMPOSIO_SEARCH_* slug (events, TripAdvisor, shopping, etc.)."""
    return _wrap(
        "lexi_execute_search_action",
        lexi.execute_search_action_action,
        slug=slug,
        arguments_json=arguments_json,
    )


@mcp.tool()
def lexi_get_family_calendar_status() -> str:
    """Check if family Google calendar (Do Not Move blocks) is configured for weekend scheduling."""
    return _wrap("lexi_get_family_calendar_status", lexi.get_family_calendar_status_action)


@mcp.tool()
def lexi_research_person(
    name: str,
    company: str = "",
    email: str = "",
    include_inbox: str = "true",
) -> str:
    """Pre-meeting research: web + news + prior Kory inbox threads about this person."""
    use_inbox = include_inbox.strip().lower() in {"true", "yes", "1"}
    return _wrap(
        "lexi_research_person",
        lexi.research_person_action,
        name=name,
        company=company,
        email=email,
        include_inbox=use_inbox,
    )


# ── Inbound email approval queue (existing Teams / dashboard flow) ────────────


@mcp.tool()
def get_lexi_pending_queue_tool() -> str:
    """Return Lexi proposals awaiting CEO approval (pending_approval) from inbound email."""
    try:
        items = get_lexi_pending_queue()
        from app.bot.teams_text import format_pending_approval_digest

        payload = [item.to_dict() for item in items]
        return _ok(
            {
                "count": len(payload),
                "queue": payload,
                "formatted_list": [item.teams_summary_line() for item in items],
                "formatted_digest": format_pending_approval_digest(items),
            }
        )
    except Exception as exc:
        return _error(
            f"Failed to load Lexi pending queue: {type(exc).__name__}: {exc}",
            code="queue_load_failed",
        )


@mcp.tool()
def get_pending_decisions() -> str:
    """Alias for get_lexi_pending_queue_tool."""
    return get_lexi_pending_queue_tool()


@mcp.tool()
def execute_lexi_approval_tool(
    proposal_id: str,
    decision: str,
    selected_slot: str,
    authorized_by: str,
    modification_notes: str = "",
) -> str:
    """Approve, modify-approve, or reject an inbound-email Lexi proposal."""
    try:
        parsed = ExecuteLexiApprovalInput(
            proposal_id=int(proposal_id),
            decision=decision.strip().lower(),
            selected_slot=selected_slot,
            authorized_by=authorized_by.strip(),
            modification_notes=modification_notes,
        )
        if parsed.decision == "rejected":
            slot_value = ""
        else:
            slot_value = parsed.selected_slot.strip()
            pending = next(
                (item for item in get_lexi_pending_queue() if item.proposal_id == parsed.proposal_id),
                None,
            )
            needs_slot = bool(
                pending and (pending.proposed_slots or pending.holds)
            )
            if needs_slot and not slot_value:
                return _error(
                    "selected_slot is required for approved/modified scheduling proposals.",
                    code="validation_error",
                )

        result = execute_lexi_approval(
            proposal_id=parsed.proposal_id,
            decision=parsed.decision,
            selected_slot=slot_value,
            authorized_by=parsed.authorized_by,
            modification_notes=parsed.modification_notes.strip() or None,
            decision_source="hermes_mcp",
        )
        body = result.to_dict()
        body["ok"] = result.ok
        return json.dumps(body, default=str)
    except ValidationError as exc:
        return _error(f"Invalid tool input: {exc}", code="validation_error")
    except ValueError as exc:
        return _error(str(exc), code="value_error")
    except Exception as exc:
        return _error(
            f"Lexi execution failed: {type(exc).__name__}: {exc}",
            code="execution_failed",
        )


@mcp.tool()
def approve_decision(
    decision_id: str,
    selected_slot: str = "",
    authorized_by: str = "kory",
) -> str:
    """Approve a pending inbound proposal (auto-picks first slot if omitted)."""
    slot_value = selected_slot.strip()
    if not slot_value:
        for item in get_lexi_pending_queue():
            if item.proposal_id == int(decision_id):
                if item.proposed_slots:
                    slot_value = json.dumps(item.proposed_slots[0])
                elif not (item.holds or []):
                    slot_value = ""
                break
        if slot_value == "" and any(
            item.proposal_id == int(decision_id) and (item.proposed_slots or item.holds)
            for item in get_lexi_pending_queue()
        ):
            return _error(
                "selected_slot is required when the proposal has scheduling slots.",
                code="validation_error",
            )
    return execute_lexi_approval_tool(
        proposal_id=decision_id,
        decision="approved",
        selected_slot=slot_value,
        authorized_by=authorized_by,
    )


@mcp.tool()
def modify_and_approve_decision(
    decision_id: str,
    new_time: str,
    notes: str,
    authorized_by: str,
) -> str:
    """Modify and approve using new_time as selected slot start."""
    slot_payload = json.dumps({"start": new_time.strip(), "end": new_time.strip()})
    return execute_lexi_approval_tool(
        proposal_id=decision_id,
        decision="modified",
        selected_slot=slot_payload,
        authorized_by=authorized_by,
        modification_notes=notes,
    )


@mcp.tool()
def reject_decision(decision_id: str, reason: str, authorized_by: str = "kory") -> str:
    """Reject a pending inbound proposal."""
    return execute_lexi_approval_tool(
        proposal_id=decision_id,
        decision="rejected",
        selected_slot="",
        authorized_by=authorized_by.strip() or "kory",
        modification_notes=reason,
    )


if __name__ == "__main__":
    init_lexi_db()
    _bootstrap_lexi_worker()
    mcp.run(transport="stdio")
