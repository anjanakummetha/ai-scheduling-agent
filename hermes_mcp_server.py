"""MCP bridge: Lexi scheduling assistant tools for Hermes (Hermes-only Teams).

Hermes (Claude OAuth) is the sole Teams front door. This server exposes
calendar, email, hold, queue, and approval actions against Kory's Outlook via Composio.
A background Lexi worker (orchestrator) starts automatically for inbound email.

Run with:
    python hermes_mcp_server.py
"""

from __future__ import annotations

import functools
import json
import os
from typing import Any

import anyio

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


def _chat_text_breaks(obj: Any) -> Any:
    """Teams collapses lone newlines in markdown, so multi-line messages reach
    Kory as one run-on line. Normalize every human-facing text field before it
    leaves a tool — Hermes relays these strings near-verbatim."""
    from app.bot.teams_format import teams_markdown_breaks

    if isinstance(obj, dict):
        # drafted_reply/draft/body included: staged draft bodies are shown to
        # Kory near-verbatim too — without the break fix his bullets render as
        # one run-on line (live O-2b, proposal 6813).
        text_fields = {"message", "kory_message", "drafted_reply", "draft", "body"}
        return {
            key: (
                teams_markdown_breaks(value)
                if key in text_fields and isinstance(value, str)
                else _chat_text_breaks(value)
            )
            for key, value in obj.items()
        }
    if isinstance(obj, list):
        return [_chat_text_breaks(item) for item in obj]
    return obj


def _ok(data: dict[str, Any]) -> str:
    # Every result carries the real date: the model was resolving "August 10"
    # to 2025 and offering to "correct" a correctly-stored 2026 date.
    return json.dumps(_chat_text_breaks({"ok": True, "today": _today_mt(), **data}), default=str)


def _today_mt() -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime.now(tz=ZoneInfo("America/Denver")).strftime("%Y-%m-%d (%A)")


def _error(message: str, *, code: str = "tool_error") -> str:
    return json.dumps(_chat_text_breaks({"ok": False, "error_code": code, "message": message}))


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


def _tool(fn):
    """Register a tool whose body runs OFF the event loop.

    mcp.server.fastmcp calls sync tools directly on the event loop —
    func_metadata.call_fn_with_arg_validation does `return fn(...)` with no
    thread offload. Every tool here blocks: Composio HTTP, LLM calls, SQLite.
    So a single slow tool freezes the whole server for its duration — other
    tool calls queue behind it and the keepalive stops answering. That is the
    logged signature: a 120s tool timeout followed by "keepalive failed,
    triggering reconnect".

    functools.wraps keeps the original signature, which is what FastMCP reads to
    build the input schema, while iscoroutinefunction sees the async wrapper and
    so awaits it instead of calling it inline.
    """

    @functools.wraps(fn)
    async def _run(*args: Any, **kwargs: Any) -> Any:
        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))

    return mcp.tool()(_run)


# ── Conversational assistant (Hermes drives dialogue; tools execute) ─────────


@_tool
def lexi_get_inbound_reply_queue() -> str:
    """New inbound emails awaiting Kory's yes/no on whether to draft a reply."""
    return _wrap("lexi_get_inbound_reply_queue", lexi.get_inbound_reply_queue_action)


@_tool
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


@_tool
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


@_tool
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


@_tool
def lexi_escalate_to_kory(proposal_id: str, reason: str = "") -> str:
    """Notify Kory in Teams when scheduling cannot be completed. Kory is the
    ONLY escalation target — never offer to flag, forward, or hand off an issue
    to anyone else."""
    try:
        pid = int(proposal_id)
    except ValueError:
        return _error("proposal_id must be an integer string.", code="validation_error")
    return _wrap(
        "lexi_escalate_to_kory",
        lexi.escalate_to_kory_action,
        proposal_id=pid,
        reason=(reason or "").strip(),
    )


@_tool
def lexi_retry_scheduling(proposal_id: str, guidance: str) -> str:
    """THE tool to run whenever Kory answers a scheduling escalation with guidance —
    "retry scheduling", "lunch is fine / approved for this one", "offer next week
    instead", "make an exception", "try Friday" — AND whenever Kory wants a rejected
    or discarded draft redone ("redo it", "re-draft that", "start fresh on that
    thread"): rejected proposals are valid input, pass the redo request as `guidance`.
    Call it IMMEDIATELY with Kory's own words as `guidance`; the deterministic engine
    re-searches the calendar honoring them. Do NOT answer from your own calendar
    reading and NEVER compose the email yourself in chat instead of calling this —
    a chat-composed draft has no proposal id and can never be approved or sent; the
    engine is the authority on which slots are valid. Reply to Kory with kory_message
    only — one or two plain sentences."""
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


@_tool
def lexi_begin_reoffer(proposal_id: str) -> str:
    """After recipient declined offered times: find new slots and stage approval card."""
    try:
        pid = int(proposal_id)
    except ValueError:
        return _error("proposal_id must be an integer string.", code="validation_error")
    return _wrap("lexi_begin_reoffer", lexi.begin_reoffer_action, proposal_id=pid)


@_tool
def lexi_recipient_timezone(sender_email: str = "", body: str = "") -> str:
    """Detect recipient timezone from domain, body cues, or email headers (never assumes)."""
    return _wrap(
        "lexi_recipient_timezone",
        lexi.recipient_timezone_action,
        sender_email=sender_email,
        body=body,
    )


@_tool
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


@_tool
def lexi_update_proposal_draft(proposal_id: str, drafted_reply: str) -> str:
    """Apply Kory's edits to a staged draft before send — VALIDATING.

    For scheduling drafts, every meeting time in the edited text is checked
    against Kory's LIVE calendar and rules, and the proposal's slots are
    re-staged to match the draft (so approval places holds for exactly the
    offered times). A time that is busy or rule-breaking is REFUSED with the
    clash named — fix the time and call again. This is the ONLY safe way to
    change offered times by hand; never present times to Kory that this tool
    (or the scheduling engine) has not validated. If it refuses, tell Kory
    which time clashed and with what — do not work around it."""
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


@_tool
def lexi_list_calendars(role: str = "read") -> str:
    """List Outlook calendars by name (read=Kory, write=pilot/production mailbox).

    Use aliases from config/calendars.yaml: team, ifg, master, deals, heidi, etc.
    """
    return _wrap("lexi_list_calendars", lexi.list_calendars, role=role)


@_tool
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


@_tool
def lexi_preview_scheduling_email() -> str:
    """Show an example scheduling reply with correct Kory formatting (TZ + sign-off)."""
    return _wrap("lexi_preview_scheduling_email", lexi.preview_scheduling_email_example)


@_tool
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


@_tool
def lexi_inbox_review(hours: str = "48") -> str:
    """48-hour inbox activity summary + open action items for Kory. Trigger: Kory says 'inbox review'."""
    try:
        window = max(1, min(168, int(hours.strip() or "48")))
    except ValueError:
        window = 48
    return _wrap("lexi_inbox_review", lexi.inbox_review_action, hours=window)


@_tool
def lexi_unanswered_brief(hours: str = "72") -> str:
    """Emails Kory may still need to reply to. Teams shortcut: `unanswered`."""
    try:
        window = max(1, min(168, int(hours.strip() or "72")))
    except ValueError:
        window = 72
    return _wrap("lexi_unanswered_brief", lexi.unanswered_brief_action, hours=window)


@_tool
def lexi_today_calendar() -> str:
    """Today's calendar for Kory. Teams shortcut: `today`."""
    return _wrap("lexi_today_calendar", lexi.today_calendar_brief_action)


@_tool
def lexi_prebrief(include_research: str = "false") -> str:
    """List today's meetings so Kory can choose one to be briefed on. Fast.

    Shows each meeting with its external attendees. Does NOT research anyone —
    call lexi_meeting_brief or lexi_precall_brief once he names one. Teams
    shortcut: `prebrief`.
    """
    return _wrap("lexi_prebrief", lexi.prebrief_action)


@_tool
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


@_tool
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


@_tool
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


@_tool
def lexi_list_asana_projects() -> str:
    """Every Asana project Lexi can read for Kory (reads span all of them).

    Use this before saying which projects are visible. Tasks default to
    Kory's personal project but can be created on any of these via the
    project parameter of lexi_create_asana_task.
    """
    return _wrap("lexi_list_asana_projects", lexi.list_asana_projects_action)


@_tool
def lexi_list_asana_boards() -> str:
    """Boards (sections) available for new tasks — General, Personal, YPO, etc."""
    return _wrap("lexi_list_asana_boards", lexi.list_asana_boards_action)


@_tool
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


@_tool
def lexi_create_asana_task(
    title: str,
    notes: str = "",
    due_on: str = "",
    section: str = "",
    project: str = "",
    assignee: str = "",
    allow_duplicate: str = "false",
    confirm: str = "false",
) -> str:
    """Create an Asana task. Call this FIRST — before you ask Kory anything.

    Call it the moment he asks for a task, with the title and whatever else he
    already said. Do NOT ask him about the project, board or due date yourself,
    and do not look up projects or boards first: this tool checks whether he
    already has an open task covering it, and when he does those questions are
    moot — he wants that one re-dated, not a second copy. Asking up front is how
    "add a task to schedule a consultation with Brooke" got three questions about
    a task he already had open.

    project, section and due_on are all required before anything is written —
    but the TOOL is what asks for them, not you. When one is missing it returns
    project_required or task_details_required with the options and the question
    to put to him: ask that, then retry with his answer. Do not choose for him,
    and do not pass "default" or "General" unless he said so. "no due date" is a
    valid answer to the date.

    If it returns possible_duplicate, tell him about the task he already has and
    ask whether to re-date it or create a second one. Pass allow_duplicate='true'
    only after he says he wants both.

    assignee: full name or email to own the task ("assign it to Heidi"). Resolved
    against the workspace. Leave blank to leave it unassigned.

    Pass confirm='true' once Kory has asked for or agreed to this; if it returns
    confirmation_required, ask him and retry — never report it as unsupported.
    Report the task created only when ok is true — if assigning failed, the task
    exists but is unowned, and that is what to tell him."""
    approved = confirm.strip().lower() in {"1", "true", "yes"}
    return _wrap(
        "lexi_create_asana_task",
        lexi.create_asana_task_action,
        title=title,
        notes=notes,
        due_on=due_on,
        section=section,
        project=project,
        assignee=assignee,
        allow_duplicate=allow_duplicate.strip().lower() in {"1", "true", "yes"},
        confirm=approved,
    )


@_tool
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


@_tool
def lexi_update_asana_task(
    task_gid: str,
    title: str = "",
    notes: str = "",
    due_on: str = "",
    assignee: str = "",
    confirm: str = "false",
    owner_ack: str = "false",
) -> str:
    """Update an Asana task's title, notes, due date, or assignee. Pass assignee='<full name or email>' to assign someone; it is resolved against the Asana workspace, and 'me'/'Kory' resolves to the connected user. Due dates must be absolute (YYYY-MM-DD) and in the future — resolve relative dates against the current year. Pass confirm='true' once Kory has asked for or agreed to this; if it returns confirmation_required, ask him and retry — never report it as unsupported."""
    approved = confirm.strip().lower() in {"1", "true", "yes"}
    return _wrap(
        "lexi_update_asana_task",
        lexi.update_asana_task_action,
        task_gid=task_gid,
        title=title,
        notes=notes,
        due_on=due_on,
        assignee=assignee,
        confirm=approved,
        owner_ack=owner_ack.strip().lower() in {"1", "true", "yes"},
    )


@_tool
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


@_tool
def lexi_search_asana_tasks(query: str) -> str:
    """Search Asana tasks by name across NON-IFG + related projects (read-only)."""
    return _wrap("lexi_search_asana_tasks", lexi.search_asana_tasks_action, query=query)


@_tool
def lexi_move_asana_task(
    task_gid: str,
    section_gid: str = "",
    section_name: str = "",
    project: str = "",
    keep_in_current: str = "false",
    confirm: str = "false",
) -> str:
    """Move an Asana task to a section or project. This is a real move: the task is unfiled from every other project. Pass keep_in_current='true' only when Kory wants it to appear in both places (Asana tasks can be multi-homed). Section names resolve within Kory's personal project unless project='<name>' says which project's boards to use. Pass confirm='true' once Kory has asked for or agreed to this."""
    approved = confirm.strip().lower() in {"1", "true", "yes"}
    return _wrap(
        "lexi_move_asana_task",
        lexi.move_asana_task_action,
        task_gid=task_gid,
        section_gid=section_gid,
        section_name=section_name,
        project=project,
        unfile_others=keep_in_current.strip().lower() not in {"1", "true", "yes"},
        confirm=approved,
    )


@_tool
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


@_tool
def lexi_hubspot_status() -> str:
    """HubSpot connection status (read-only sample)."""
    return _wrap("lexi_hubspot_status", lexi.hubspot_status_action)


@_tool
def lexi_hubspot_cleanup_proposals(inactive_days: str = "180") -> str:
    """CRM health report for Kory's contacts: where the book is thin. Read-only."""
    try:
        days = int(inactive_days.strip() or "180")
    except ValueError:
        days = 180
    return _wrap("lexi_hubspot_cleanup_proposals", lexi.hubspot_cleanup_proposals_action, inactive_days=days)


@_tool
def lexi_hubspot_health_report(all_owners: str = "false") -> str:
    """Where Kory's CRM is incomplete — counts are portal-wide, not a sample. Read-only."""
    every = all_owners.strip().lower() in {"1", "true", "yes"}
    return _wrap("lexi_hubspot_health_report", lexi.hubspot_health_report_action, all_owners=every)


@_tool
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


@_tool
def lexi_hubspot_compare_books() -> str:
    """Compare Kory's contact book against the other IFG owners. Read-only."""
    return _wrap("lexi_hubspot_compare_books", lexi.hubspot_compare_books_action)


@_tool
def lexi_hubspot_recent_changes(days: str = "7") -> str:
    """What changed in HubSpot lately: new contacts and deal stage movements. Read-only."""
    try:
        n = max(1, min(90, int(days.strip() or "7")))
    except ValueError:
        n = 7
    return _wrap("lexi_hubspot_recent_changes", lexi.hubspot_recent_changes_action, days=n)


@_tool
def lexi_hubspot_duplicate_merges(limit: str = "0") -> str:
    """Propose duplicate contact merges across Kory's whole contact book. Leave limit empty to scan everything — a partial scan only finds a duplicate when both records land in the same sample, so it reports "none" almost regardless of the truth. The result carries a coverage object; report scanned vs portal_total and never call the book clean unless complete is true. Proposals are staged; applying a staged batch performs a REAL merge in the shared IFG portal, which cannot be undone — say so before applying."""
    try:
        n = max(0, int(limit.strip() or "0"))
    except ValueError:
        n = 0
    return _wrap("lexi_hubspot_duplicate_merges", lexi.hubspot_duplicate_merges_action, limit=n)


@_tool
def lexi_hubspot_enrich_contacts(limit: str = "12", include_phone: str = "false") -> str:
    """Fill gaps in Kory's HubSpot contacts from sources that cannot be wrong.

    Call this first — the tool does the scanning and reports what it found.

    Four sources, in descending order of directness: HubSpot's own company
    records (an existing company link, or a company record matching the contact's
    email domain); the other contacts at that domain, when they all agree; the
    contact's own email signature in Kory's inbox; and their LinkedIn profile,
    but ONLY when the profile shows a role at the employer already on the record.
    Every proposed value carries the evidence behind it — pass that on to Kory
    rather than summarising it away.

    The LinkedIn tier never asks what someone's job title is. It asks whether a
    candidate profile shows a role at the employer already on file, because a
    stranger who shares the name does not also share the employer. When it cannot
    corroborate, it refuses. Kory's book contains two Chris Gavoras, so this
    matters more here than it would elsewhere.

    Fills a field only when it is blank OR holds a placeholder such as
    'Prefer No Connection to Company'. A real value is never overwritten, and
    that is re-checked at apply time so a stale batch cannot clobber something
    Kory filled in himself.

    **Works one batch at a time.** The result carries `remaining` and
    `stopped_at_time_limit`. When contacts are left, say so and offer to keep
    going — a second call continues rather than repeating, because contacts that
    yielded nothing are remembered.

    Two results are findings, not fills, and should be reported as such:
    `not_people` (shared mailboxes like accounting@ — these must never be given a
    job title) and `may_have_moved` (the profile shows they have left that
    employer, so the record is stale rather than incomplete).

    For contacts nothing could be established for, say so plainly. Do NOT offer
    to search the web yourself or promise a deeper look — the corroborated
    lookup above is already the deepest available, and anything beyond it would
    be a guess about a real person in a shared CRM. Naming the ones on a company
    domain as worth a manual look is fine.

    Pass include_phone='true' to also propose phone numbers. Phone comes only
    from signatures — no other source available to Lexi carries it, and neither
    HubSpot's enrichment nor LinkedIn will fill it.

    Nothing is written here. This returns a batch_id; applying it is a separate,
    confirmed call to lexi_hubspot_apply_batch, and every applied value can be
    undone with lexi_hubspot_undo_batch.
    """
    try:
        n = max(1, min(25, int(limit.strip() or "12")))
    except ValueError:
        n = 12
    return _wrap(
        "lexi_hubspot_enrich_contacts",
        lexi.hubspot_enrichment_action,
        limit=n,
        include_phone=include_phone.strip().lower() in {"1", "true", "yes"},
    )


@_tool
def lexi_hubspot_apply_batch(
    batch_id: str,
    confirm: str = "false",
    merge_pair: str = "",
    owner_ack: str = "false",
) -> str:
    """Apply a staged HubSpot batch. This writes to the real shared IFG portal.

    Pass confirm='true' once Kory has asked for or agreed to it; if it returns
    confirmation_required, ask him and call again — never report it as unsupported.

    Enrichment batches apply every proposed fill and can be undone afterwards with
    lexi_hubspot_undo_batch. Duplicate-merge batches are different: a HubSpot merge
    is PERMANENT and cannot be undone by anyone, so they apply one pair at a time
    and need merge_pair='<primary_id>:<duplicate_id>'. Tell Kory it is irreversible
    before applying one.

    If a record belongs to another IFG owner this returns owner_confirmation_required
    naming them. Re-call with owner_ack='true' only after Kory confirms he means to
    change someone else's record.
    """
    return _wrap(
        "lexi_hubspot_apply_batch",
        lexi.hubspot_apply_batch_action,
        batch_id=batch_id.strip(),
        confirm=confirm.strip().lower() in {"1", "true", "yes"},
        merge_pair=merge_pair.strip(),
        owner_ack=owner_ack.strip().lower() in {"1", "true", "yes"},
    )


@_tool
def lexi_hubspot_set_field(
    contact: str,
    field: str,
    value: str,
    confirm: str = "false",
    owner_ack: str = "false",
) -> str:
    """Set one field on one HubSpot contact because Kory told you the answer.

    Use when he supplies the value himself — "Jeremy Boka's company is Sustainable
    Sites", "his title is Managing Partner", "her number is 303-555-0134". Every
    other HubSpot tool refuses to write anything it cannot source; here the source
    is Kory, which is better provenance than any of them.

    In particular this is how you answer the enrichment scan's own questions.
    When it reports someone holding several current roles, or a value it could
    not establish, and Kory then tells you which — this is the tool that acts on
    it. Do not report back that you have no way to set it.

    contact: a contact id, an email, or a name. If the name is ambiguous the tool
    returns ambiguous_contact with the matches — ask him which, don't guess.
    field: company, jobtitle, phone, or hs_linkedin_url.

    Unlike the enrichment path this WILL overwrite an existing value, because an
    explicit instruction usually means the current value is the thing being
    corrected. The result names what it replaced and carries an undo batch id.

    Pass confirm='true' once he has asked for it. If the record belongs to another
    IFG owner this returns owner_confirmation_required naming them; re-call with
    owner_ack='true' only after he confirms he means to change their record.
    """
    return _wrap(
        "lexi_hubspot_set_field",
        lexi.hubspot_set_field_action,
        contact=contact.strip(),
        field=field.strip(),
        value=value.strip(),
        confirm=confirm.strip().lower() in {"1", "true", "yes"},
        owner_ack=owner_ack.strip().lower() in {"1", "true", "yes"},
    )


@_tool
def lexi_hubspot_undo_batch(
    batch_id: str, confirm: str = "false", force: str = "false"
) -> str:
    """Put back every field a staged enrichment batch wrote — the undo for apply_batch.

    Restores each contact's previous value, including back to blank. Works only for
    enrichment; a merge cannot be undone by this or anything else, in Lexi or in
    HubSpot. Pass confirm='true' once Kory has asked for or agreed to it.

    Fields that no longer hold what the batch wrote are LEFT ALONE and returned in
    `skipped_changed_since` — someone has edited them since, and an undo that
    discards a correction is not an undo. Report those to Kory by name. Only pass
    force='true' if he then says to roll them back regardless.
    """
    return _wrap(
        "lexi_hubspot_undo_batch",
        lexi.hubspot_undo_batch_action,
        batch_id=batch_id.strip(),
        confirm=confirm.strip().lower() in {"1", "true", "yes"},
        force=force.strip().lower() in {"1", "true", "yes"},
    )


@_tool
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


@_tool
def lexi_hubspot_prebrief_enrich(email: str = "", name: str = "") -> str:
    """Alias of lexi_lookup_person, kept for existing prebrief shortcuts."""
    return _wrap(
        "lexi_hubspot_prebrief_enrich",
        lexi.hubspot_prebrief_enrich_action,
        email=email,
        name=name,
    )


@_tool
def lexi_hubspot_meeting_note(
    email: str,
    note: str,
    meeting_subject: str = "",
    confirm: str = "false",
    owner_ack: str = "false",
) -> str:
    """Log a HubSpot note on a contact after a meeting. This writes to the real shared IFG portal once confirmed.

    Call this as soon as Kory asks for the note — the tool finds the contact and
    reports any problem before anything is written. Do not gather details first.
    Pass confirm='true' once he has asked for or agreed to it; if it returns
    confirmation_required, ask him and call again — never report it as unsupported.

    Returns contact_not_found with near matches when no HubSpot record has that
    address, and owner_confirmation_required naming the owner when the contact
    belongs to another IFG person. Re-call with owner_ack='true' only after Kory
    confirms he means to touch someone else's record.
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


@_tool
def lexi_hubspot_deals_snapshot(limit: str = "8") -> str:
    """Open HubSpot deals for CEO briefing (read-only)."""
    try:
        n = max(1, min(25, int(limit.strip() or "8")))
    except ValueError:
        n = 8
    return _wrap("lexi_hubspot_deals_snapshot", lexi.hubspot_deals_snapshot_action, limit=n)


@_tool
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


@_tool
def lexi_handle_teams_command(text: str, authorized_by: str = "kory") -> str:
    """MANDATORY router for Lexi commands. If Kory's message starts with (or is)
    approve / reject / discard / cancel / send / draft / show draft / pending /
    inbound / today / unanswered / help — with or without #N and a reason — call
    this tool FIRST with the message text verbatim. NEVER answer these from your
    own memory or a calendar read: the queue and proposal state live in Lexi's
    database and this tool is the only authority. If the result has handled=true,
    relay its message; only if handled=false may you respond conversationally.
    Examples that MUST route here: 'reject draft 2 — test complete',
    'approve draft 1', 'pending', 'show draft 1', 'send',
    'cancel meeting #6481 — recipient request' (cancels a BOOKED meeting —
    executed proposals included; never tell Kory to cancel manually).

    Kory approves by DRAFT NUMBER — the small position number shown by
    'pending' (1, 2, 3…), which renumbers as drafts clear. Pass his message
    through verbatim; this tool resolves the number. Never invent or guess a
    number, and never substitute a raw proposal id you saw earlier in the chat:
    if you are unsure which draft he means, call this tool with 'pending' and
    ask him which one. Raw ids still resolve, so an older quoted '#6481' is
    safe to pass through unchanged.

    If an approve call TIMES OUT or errors without a clear result, the send may
    still be executing in the background. NEVER tell Kory to approve again — a
    second approve can double-send. Say the send is likely still in progress and
    to check with 'pending' or 'show draft #N' in a minute.

    Natural-language confirmations count too: if Kory says 'yes send it',
    'send the invite', 'go ahead' about a specific proposal, call this tool
    with 'approve #N' for that proposal — do NOT answer conversationally.
    NEVER claim an email or invite was sent, or that something is 'already
    done', unless a tool result IN THIS TURN says so — prior chat context is
    not evidence; the database is the only authority.

    A proposal has TWO approvals: the offer, then — after the counterpart
    picks a time — the calendar invite. An 'approve #N' (or 'send invite #N')
    on a proposal whose offer email already went out is how the INVITE gets
    sent; route it. Do not refuse it because an email 'was already sent' —
    the double-send guards live server-side and will refuse anything unsafe
    with an explanation you can relay. Refusing to route is never safety; it
    only strands the booking. (Live failure: the invite step was refused
    twice in chat because an earlier warning said 'do not approve again'.)

    EVERY retry of a command must call this tool again, even seconds after a
    refusal. Calendars change, locks clear, fixes deploy — a previous tool
    result is never evidence about the CURRENT state, and re-running is
    always safe for the same reason: the guards are server-side. Answering a
    retried command from the previous result is the exact failure this
    paragraph exists to prevent (live: 'the slot is still blocked' relayed
    from memory while the tool, when finally called, sent the invite).
    """
    from app.teams.commands import handle_teams_command

    result = handle_teams_command(text, authorized_by=authorized_by.strip() or "kory")
    return json.dumps(_chat_text_breaks({"ok": result.get("ok", False), **result}), default=str)


def _card_tool(func):
    """Register card tools only when cards are the active surface.

    Cards are PARKED (ruling 2026-08-08): text-only is the supported mode and
    no card is ever pushed — but this tool was still registered, so the model
    could be steered into the card path by a stale card resurfacing in Teams
    history. Same pattern as _campaign_tool: while lexi_teams_text_only is on,
    the function exists but Hermes never sees it.
    """
    from app.config import settings as _settings

    if getattr(_settings, "lexi_teams_text_only", True):
        return func
    return _tool(func)


@_card_tool
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
    return json.dumps(_chat_text_breaks({"ok": result.get("ok", False), **result}), default=str)


@_tool
def lexi_get_calendar_availability(days: str = "0") -> str:
    """Quick busy/free read (internal). For day-by-day week summaries use lexi_summarize_calendar_window."""
    try:
        window = int(days)
    except ValueError:
        return _error("days must be an integer string, e.g. '60'.", code="validation_error")
    return _wrap("lexi_get_calendar_availability", lexi.get_calendar_availability, days=window)


@_tool
def lexi_summarize_calendar_window(query: str) -> str:
    """Day-by-day calendar summary from live Master + work Calendar (read-only).

    Pass Kory's full ask, e.g. 'summarize my full calendar for next week'.
    Returns formatted_summary with correct dates and real events — relay to Kory; do not invent times.
    """
    return _wrap("lexi_summarize_calendar_window", lexi.summarize_calendar_window, query=query.strip())


@_tool
def lexi_check_time_slot(start_iso: str, end_iso: str) -> str:
    """Check if a start/end ISO interval conflicts with Kory's calendar."""
    return _wrap(
        "lexi_check_time_slot",
        lexi.check_time_slot,
        start_iso=start_iso,
        end_iso=end_iso,
    )


@_tool
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


@_tool
def lexi_create_calendar_event(
    title: str,
    start_iso: str,
    end_iso: str,
    attendee_email: str = "",
    location: str = "",
    notes: str = "",
    calendar_name: str = "",
    is_online_meeting: str = "",
    allow_conflict: str = "false",
    confirm: str = "false",
) -> str:
    """Create an ordinary calendar event with the exact title given (no HOLD prefix).

    Use this for any normal event Kory asks for. Use lexi_place_calendar_hold ONLY
    for tentative placeholders while scheduling options are outstanding.
    allow_conflict='true' double-books on purpose; confirm must be 'true'.
    Returns verified=true only when the calendar confirms the event landed.
    """
    truthy = {"true", "yes", "1"}
    online: bool | None = None
    if is_online_meeting.strip():
        online = is_online_meeting.strip().lower() in truthy
    return _wrap(
        "lexi_create_calendar_event",
        lexi.create_calendar_event,
        title=title,
        start_iso=start_iso,
        end_iso=end_iso,
        attendee_email=attendee_email,
        location=location,
        notes=notes,
        calendar_name=calendar_name,
        is_online_meeting=online,
        allow_conflict=allow_conflict.strip().lower() in truthy,
        confirm=confirm.strip().lower() in truthy,
    )


@_tool
def lexi_move_calendar_event(
    event_id: str,
    start_iso: str,
    end_iso: str = "",
    allow_conflict: str = "false",
    confirm: str = "false",
) -> str:
    """Move/reschedule an existing calendar event to a new time.

    end_iso is optional — omit it to keep the meeting's current length.
    Find event_id via lexi_today_calendar / lexi_summarize_calendar_window first.
    allow_conflict='true' moves it despite a clash; confirm must be 'true'.
    Returns verified=true only after reading the event back at its new time —
    report the move as done only when that is true.
    """
    truthy = {"true", "yes", "1"}
    return _wrap(
        "lexi_move_calendar_event",
        lexi.move_calendar_event,
        event_id=event_id,
        start_iso=start_iso,
        end_iso=end_iso,
        allow_conflict=allow_conflict.strip().lower() in truthy,
        confirm=confirm.strip().lower() in truthy,
    )


@_tool
def lexi_draft_outbound_email(
    to_email: str,
    subject: str,
    body: str,
    send_channel: str = "",
) -> str:
    """Preview a NON-SCHEDULING outbound email without sending.

    NEVER use this for an email that offers meeting times or shares Kory's
    availability — call lexi_start_scheduling instead, so the slots are
    engine-validated, calendar holds are placed, and an acceptance can be
    tracked. A time offered from a plain draft has no hold and can be
    double-booked before the recipient answers.

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


@_tool
def lexi_todays_briefing(briefing_date: str = "") -> str:
    """The CEO briefing Kory was emailed this morning — MANDATORY whenever he
    refers to it.

    Triggers: "my briefing", "this morning's briefing", "the tasks from my
    briefing", "what did the briefing say about X", "that thing in my brief",
    and any request that assumes you know what he read at 4:45 AM.

    Call this BEFORE answering or acting. Do not guess what was in it, and do not
    rebuild it from the calendar or inbox — quote the stored briefing's own
    wording for names, numbers and dates. briefing_date (YYYY-MM-DD) fetches an
    earlier day; leave blank for today.
    """
    return _wrap("lexi_todays_briefing", lexi.todays_briefing_action, briefing_date=briefing_date.strip())


@_tool
def lexi_save_email_to_drafts(
    to_email: str,
    subject: str,
    body: str,
    cc_emails: str = "",
    allow_unverified_recipient: str = "false",
) -> str:
    """Save an email to Kory's Outlook Drafts for him to review and send himself.

    Triggers: "put that in my drafts", "save this as a draft", "draft it and I'll
    send it later", "add it to my drafts so I can review it".

    NEVER invent or guess to_email. Find the real address first — lexi_lookup_person
    (pass the person exactly as Kory named them, e.g. "Angelo (Morgan Stanley)"),
    lexi_search_inbox for a prior thread, or ask him. A draft to a plausible wrong
    address is worse than no draft: it looks correct and fails only on a bounce.
    The tool refuses an address with no record and offers candidates; resolve those
    rather than overriding. allow_unverified_recipient='true' only when Kory has
    given the address himself or confirmed it is a genuinely new contact.

    Nothing is sent — the draft sits in his Drafts folder with his signature
    already applied, and he sends it from Outlook when ready. No approval needed,
    because nothing leaves the mailbox. cc_emails is a comma-separated list.

    Returns verified=true only once the draft is read back from the mailbox; say
    it is saved only when that is true.
    """
    return _wrap(
        "lexi_save_email_to_drafts",
        lexi.save_email_to_drafts,
        to_email=to_email,
        subject=subject,
        body=body,
        cc_emails=cc_emails,
        allow_unverified_recipient=allow_unverified_recipient.strip().lower()
        in {"1", "true", "yes"},
    )


@_tool
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


@_tool
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


@_tool
def lexi_search_inbox(query: str = "", top: str = "10") -> str:
    """Search Kory's Outlook inbox (read-only). Use before drafting replies."""
    try:
        limit = int(top)
    except ValueError:
        return _error("top must be an integer string.", code="validation_error")
    return _wrap("lexi_search_inbox", lexi.search_inbox, query=query, top=limit)


@_tool
def lexi_get_thread(message_id: str) -> str:
    """Fetch a single Kory inbox message by Outlook message id."""
    return _wrap("lexi_get_thread", lexi.get_email_thread, message_id=message_id)


@_tool
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


@_tool
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


@_tool
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


@_tool
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


@_tool
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


@_tool
def lexi_get_scheduling_session(session_id: str) -> str:
    """Load a multi-turn Hermes scheduling session."""
    return _wrap("lexi_get_scheduling_session", lexi.get_scheduling_session, session_id=session_id)


@_tool
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


@_tool
def lexi_start_scheduling(
    recipient_email: str,
    subject: str,
    meeting_intent: str,
    duration_minutes: str,
    authorized_by: str = "kory",
    require_ceo_signoff: str = "true",
    voice_mode: str = "kory",
    constraints: str = "",
) -> str:
    """Start outbound scheduling: LLM slots + draft + holds + pending_approval.

    THE tool whenever an outbound email will offer meeting times — "share my/his
    availability", "offer some times", "set up a meeting/coffee/lunch with X".
    Do NOT hand-pick times from a calendar read into lexi_draft_outbound_email:
    only this path validates slots against Kory's rules, places holds, and
    tracks the recipient's acceptance.

    "Kory" is ALWAYS Kory Mitchell, CEO of Iconic Founders Group — never any
    other person named Kory. Subjects should read like "Kory Mitchell —
    <purpose>" or reference the topic; never invent a different surname.

    voice_mode: 'kory' = email written in Kory's first person, sent from his
    mailbox. 'lexi' = Lexi writes as his assistant from her mailbox. Use 'lexi'
    when Kory says "as Lexi" or asks Lexi to reach out on his behalf.

    meeting_intent examples: lunch, dinner, coffee, meeting, internal_sync.
    duration_minutes: e.g. '60' for lunch. Set require_ceo_signoff=false only if Kory
    asked to send immediately without sign-off.

    constraints: Kory's scheduling words VERBATIM — window ("next week",
    "week of the 17th"), time of day ("mornings", "after 3"), and the
    recipient's location/timezone ("she's in Boston", "mornings her time").
    ALWAYS pass them when Kory states any — the slot search parses these
    exactly like an inbound sender's words; omitting them ignores his ask.
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
        voice_mode=voice_mode,
        constraints=constraints,
    )


# ── Outlook scheduling actions (Composio SDK behind Lexi — not Composio MCP) ──


@_tool
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


@_tool
def lexi_accept_calendar_invite(event_id: str) -> str:
    """Accept a calendar invite on Kory's calendar (read-only UAT: dry-run blocked)."""
    return _wrap("lexi_accept_calendar_invite", lexi.accept_calendar_invite_action, event_id=event_id)


@_tool
def lexi_decline_calendar_invite(event_id: str, comment: str = "") -> str:
    """Decline a calendar invite (blocked in read-only UAT)."""
    return _wrap(
        "lexi_decline_calendar_invite",
        lexi.decline_calendar_invite_action,
        event_id=event_id,
        comment=comment,
    )


@_tool
def lexi_find_meeting_times(payload_json: str) -> str:
    """Find meeting times (internal). Tell Kory only whether times exist — never mention Outlook API or engine details."""
    return _wrap("lexi_find_meeting_times", lexi.find_meeting_times_action, payload_json=payload_json)


@_tool
def lexi_get_thread_context(conversation_id: str, exclude_message_id: str = "") -> str:
    """Load prior messages in an Outlook conversation for accurate replies."""
    return _wrap(
        "lexi_get_thread_context",
        lexi.get_thread_context_action,
        conversation_id=conversation_id,
        exclude_message_id=exclude_message_id,
    )


@_tool
def lexi_remember_kory_fact(fact_key: str, fact_value: str) -> str:
    """Save an explicit long-term preference Kory stated (not chat thread memory).

    Store `fact_value` as KORY'S OWN WORDS, verbatim — scheduling rules he
    states ("no meetings on Fridays", "nothing before 8:30 AM Tuesdays",
    "I'm fine with lunch meetings", "max 1 happy hour a week") are parsed
    from that sentence and ENFORCED by the slot engine, and a paraphrase may
    not parse. After saving a scheduling rule, tell Kory it now constrains
    slot searches; he can undo it anytime with "forget that"."""
    return _wrap(
        "lexi_remember_kory_fact",
        lexi.remember_kory_fact_action,
        fact_key=fact_key,
        fact_value=fact_value,
    )


@_tool
def lexi_forget_kory_fact(fact: str) -> str:
    """Remove a saved Kory fact when he says to forget/undo/remove a remembered rule.

    Pass his words for which fact (a key, or any distinctive phrase from it).
    Matches must be unique — on an ambiguous match it returns the candidates;
    show them and ask which one. The result quotes what was deleted; confirm
    that back to Kory."""
    return _wrap("lexi_forget_kory_fact", lexi.forget_kory_fact_action, fact=fact)


@_tool
def lexi_list_kory_memory() -> str:
    """List saved Kory facts (long-term memory)."""
    return _wrap("lexi_list_kory_memory", lexi.list_kory_memory_action)


# ── Composio Search (web, travel, maps — read-only; uses COMPOSIO_API_KEY) ───


@_tool
def lexi_web_search(query: str) -> str:
    """Search the web for venues, restaurants, travel info, research. Read-only."""
    return _wrap("lexi_web_search", lexi.web_search_action, query=query)


@_tool
def lexi_search_flights(query: str = "", payload_json: str = "{}") -> str:
    """Search flights. Use query='Denver to NYC March 15' or JSON with departure_id/arrival_id/outbound_date."""
    return _wrap(
        "lexi_search_flights",
        lexi.search_flights_action,
        query=query,
        payload_json=payload_json,
    )


@_tool
def lexi_search_hotels(payload_json: str) -> str:
    """Search hotels. JSON: q, check_in_date, check_out_date, adults (required: q)."""
    return _wrap("lexi_search_hotels", lexi.search_hotels_action, payload_json=payload_json)


@_tool
def lexi_search_maps(query: str) -> str:
    """Google Maps search for venues, restaurants, addresses near a location."""
    return _wrap("lexi_search_maps", lexi.search_maps_action, query=query)


@_tool
def lexi_search_news(query: str) -> str:
    """News search for context on meetings, companies, events."""
    return _wrap("lexi_search_news", lexi.search_news_action, query=query)


@_tool
def lexi_fetch_url_content(url: str, max_characters: int = 8000) -> str:
    """Fetch readable text from a public URL (docs, venue pages, articles)."""
    return _wrap(
        "lexi_fetch_url_content",
        lexi.fetch_url_content_action,
        url=url,
        max_characters=max_characters,
    )


@_tool
def lexi_execute_search_action(slug: str, arguments_json: str = "{}") -> str:
    """Run any allowlisted COMPOSIO_SEARCH_* slug (events, TripAdvisor, shopping, etc.)."""
    return _wrap(
        "lexi_execute_search_action",
        lexi.execute_search_action_action,
        slug=slug,
        arguments_json=arguments_json,
    )


@_tool
def lexi_get_family_calendar_status() -> str:
    """Check if family Google calendar (Do Not Move blocks) is configured for weekend scheduling."""
    return _wrap("lexi_get_family_calendar_status", lexi.get_family_calendar_status_action)


@_tool
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


def _pending_queue_payload() -> str:
    """Plain sync body shared by the pending-queue tools.

    A tool must never call another @_tool function: the decorator replaces its
    target with an async wrapper, so the inner call returns an un-awaited
    coroutine and the body never runs. The caller gets a coroutine repr back and
    the work is silently skipped. Shared plain helpers keep the aliases honest.
    """
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


@_tool
def get_lexi_pending_queue_tool() -> str:
    """Return Lexi proposals awaiting CEO approval (pending_approval) from inbound email."""
    return _pending_queue_payload()


@_tool
def get_pending_decisions() -> str:
    """Alias for get_lexi_pending_queue_tool."""
    return _pending_queue_payload()


def _execute_lexi_approval_payload(
    proposal_id: str,
    decision: str,
    selected_slot: str,
    authorized_by: str,
    modification_notes: str = "",
) -> str:
    """Plain sync body shared by every approval entry point.

    See _pending_queue_payload for why the approve/modify/reject tools call this
    rather than each other — an un-awaited inner tool call made those three
    silent no-ops that still looked like they had succeeded.
    """
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
        # The model composes Kory's reply from this payload, and the live
        # battery (2026-08-16) showed it can relay "Sent!" while ignoring
        # holds_confirmed=0 — the exact Aug-11 false-positive. Ship a ready
        # sentence that states the hold truth; the tool description tells the
        # model to relay it verbatim.
        if result.ok and result.email_sent and result.holds_placed_times is not None:
            if result.holds_placed_times:
                body["kory_message"] = (
                    "Sent. Holds now on the calendar: "
                    + "; ".join(result.holds_placed_times)
                    + "."
                )
            else:
                body["kory_message"] = (
                    "Sent — but NO calendar holds were placed. Tell Kory "
                    "exactly that."
                )
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


@_tool
def execute_lexi_approval_tool(
    proposal_id: str,
    decision: str,
    selected_slot: str,
    authorized_by: str,
    modification_notes: str = "",
) -> str:
    """Approve, modify-approve, or reject an inbound-email Lexi proposal.

    The result's `kory_message` states exactly what happened to calendar
    holds — relay it VERBATIM. Never say holds exist unless
    holds_placed_times lists them; an empty list means NO holds were placed
    and Kory must be told that."""
    return _execute_lexi_approval_payload(
        proposal_id=proposal_id,
        decision=decision,
        selected_slot=selected_slot,
        authorized_by=authorized_by,
        modification_notes=modification_notes,
    )


@_tool
def approve_decision(
    decision_id: str,
    selected_slot: str = "",
    authorized_by: str = "kory",
) -> str:
    """Approve a pending inbound proposal (auto-picks first slot if omitted).

    The result's `kory_message` states exactly what happened to calendar
    holds — relay it VERBATIM. Never say holds exist unless
    holds_placed_times lists them; an empty list means NO holds were placed
    and Kory must be told that."""
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
    return _execute_lexi_approval_payload(
        proposal_id=decision_id,
        decision="approved",
        selected_slot=slot_value,
        authorized_by=authorized_by,
    )


@_tool
def modify_and_approve_decision(
    decision_id: str,
    new_time: str,
    notes: str,
    authorized_by: str,
) -> str:
    """Modify and approve using new_time as selected slot start. The end time
    is derived from the proposal's offered-slot duration — never send end==start."""
    slot_payload = json.dumps({"start": new_time.strip()})
    return _execute_lexi_approval_payload(
        proposal_id=decision_id,
        decision="modified",
        selected_slot=slot_payload,
        authorized_by=authorized_by,
        modification_notes=notes,
    )


@_tool
def reject_decision(decision_id: str, reason: str, authorized_by: str = "kory") -> str:
    """Reject a pending inbound proposal."""
    return _execute_lexi_approval_payload(
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
