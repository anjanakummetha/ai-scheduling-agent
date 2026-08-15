"""Outlook actions via Composio SDK — named helpers + generic executor."""

from __future__ import annotations

from typing import Any, Literal

from app.integrations.composio_client import (
    ConnectionRole,
    execute_read_tool,
    execute_tool,
    execute_write_tool,
)
from app.safety.kory_read_only import is_outlook_write_slug

VoiceMode = Literal["kory", "lexi"]
SendChannel = Literal["kory", "lexi"]

# Slugs that must never run without explicit production unlock.
PERMANENT_DENY_SLUGS = frozenset(
    {
        "OUTLOOK_DELETE_CALENDAR_PERMANENTLY",
        "OUTLOOK_DELETE_CALENDAR_GROUP_EVENT_PERMANENTLY",
        "OUTLOOK_DELETE_EVENT_PERMANENTLY",
        "OUTLOOK_DELETE_USER_CALENDAR_EVENT_PERMANENTLY",
        "OUTLOOK_DELETE_USER_EVENT_PERMANENTLY",
        "OUTLOOK_DELETE_USER_MAIL_FOLDER_PERMANENTLY",
        "OUTLOOK_PERMANENT_DELETE_MESSAGE",
        "OUTLOOK_DELETE_CONTACT_PERMANENTLY",
        "OUTLOOK_DELETE_CONTACT_FROM_CHILD_FOLDER_PERMANENTLY",
        "OUTLOOK_DELETE_CONTACT_FROM_FOLDER_PERMANENTLY",
        "OUTLOOK_DELETE_CHILD_CONTACT_FOLDER_PERMANENTLY",
        "OUTLOOK_DELETE_ME_MAIL_FOLDER_CHILD_FOLDER_PERMANENTLY",
        "OUTLOOK_DELETE_USER_CHILD_FOLDER_MESSAGE_PERMANENTLY",
    }
)

# Scheduling-focused allowlist for generic executor (extend as needed).
SCHEDULING_ALLOW_SLUGS = frozenset(
    {
        "OUTLOOK_ACCEPT_EVENT",
        "OUTLOOK_DECLINE_EVENT",
        "OUTLOOK_FIND_MEETING_TIMES",
        "OUTLOOK_GET_CALENDAR_VIEW",
        "OUTLOOK_GET_CALENDAR_SCHEDULE",
        "OUTLOOK_GET_SCHEDULE",
        "OUTLOOK_GET_EVENT",
        "OUTLOOK_GET_MESSAGE",
        "OUTLOOK_LIST_MESSAGES",
        "OUTLOOK_SEARCH_MESSAGES",
        "OUTLOOK_QUERY_EMAILS",
        "OUTLOOK_LIST_EVENTS",
        "OUTLOOK_LIST_EVENT_INSTANCES",
        "OUTLOOK_LIST_CALENDARS",
        "OUTLOOK_LIST_CALENDAR_GROUPS",
        "OUTLOOK_CREATE_ME_EVENT",
        "OUTLOOK_CREATE_CALENDAR_EVENT_IN_CALENDAR",
        "OUTLOOK_UPDATE_CALENDAR_EVENT",
        "OUTLOOK_UPDATE_CALENDAR_EVENT_IN_CALENDAR",
        "OUTLOOK_CANCEL_CALENDAR_EVENT",
        "OUTLOOK_CANCEL_EVENT",
        "OUTLOOK_DELETE_CALENDAR_EVENT",
        "OUTLOOK_CREATE_DRAFT",
        "OUTLOOK_CREATE_DRAFT_REPLY",
        "OUTLOOK_CREATE_ME_REPLY_DRAFT",
        "OUTLOOK_SEND_EMAIL",
        "OUTLOOK_SEND_DRAFT",
        "OUTLOOK_REPLY_EMAIL",
        # OUTLOOK_FORWARD_MESSAGE removed (audit 2026-08-15, A4): it is a pure
        # mail-exfiltration primitive — forward any message from Kory's mailbox
        # to any address — gated only by a model-supplied confirm boolean, and
        # it has no legitimate use in the scheduling flow (the only in-repo
        # forwarder, forward_message_from_lexi_mailbox, has zero callers). The
        # broader "route every send through a server-side, non-model token" is
        # tracked in PRE_HANDOVER_AUDIT.md as remaining A3/A4 work.
        "OUTLOOK_ADD_EVENT_ATTACHMENT",
        "OUTLOOK_ADD_MAIL_ATTACHMENT",
        "OUTLOOK_DISMISS_EVENT_REMINDER",
        "OUTLOOK_SNOOZE_EVENT_REMINDER",
        "OUTLOOK_GET_MAILBOX_SETTINGS",
        "OUTLOOK_GET_PROFILE",
        "OUTLOOK_LIST_OUTLOOK_ATTACHMENTS",
        "OUTLOOK_GET_MAIL_ATTACHMENT",
        "OUTLOOK_DOWNLOAD_OUTLOOK_ATTACHMENT",
    }
)


def _resolve_role_for_slug(slug: str, send_channel: SendChannel = "kory") -> ConnectionRole:
    upper = slug.upper()
    if is_outlook_write_slug(upper):
        if send_channel == "lexi":
            return "lexi"
        return "write"
    return "read"


def execute_outlook_action(
    slug: str,
    arguments: dict[str, Any],
    *,
    confirm: bool = False,
    send_channel: SendChannel = "kory",
    allow_unlisted: bool = False,
) -> dict[str, Any]:
    """Run one Composio Outlook slug with safety gates."""
    normalized = (slug or "").strip().upper()
    if not normalized.startswith("OUTLOOK_") and not normalized.startswith("MICROSOFT_OUTLOOK_"):
        raise ValueError(f"Not an Outlook slug: {slug}")

    if normalized in PERMANENT_DENY_SLUGS:
        raise PermissionError(f"Permanently blocked Outlook action: {normalized}")

    if not allow_unlisted and normalized not in SCHEDULING_ALLOW_SLUGS:
        raise PermissionError(
            f"Slug {normalized} is not on the scheduling allowlist. "
            "Add to SCHEDULING_ALLOW_SLUGS after review, or pass allow_unlisted=true."
        )

    if is_outlook_write_slug(normalized) and not confirm:
        raise PermissionError(
            f"Write action {normalized} requires confirm=true (and approval in production)."
        )

    role = _resolve_role_for_slug(normalized, send_channel=send_channel)
    if role == "read":
        return execute_read_tool(normalized, arguments)
    return execute_tool(normalized, arguments, role=role)


# ── Named scheduling helpers ─────────────────────────────────────────────────


def accept_calendar_invite(event_id: str, *, tentative: bool = False) -> dict[str, Any]:
    slug = "OUTLOOK_ACCEPT_EVENT"
    return execute_outlook_action(
        slug,
        {"user_id": "me", "event_id": event_id, "send_response": True},
        confirm=True,
        send_channel="kory",
    )


def decline_calendar_invite(event_id: str, *, comment: str = "") -> dict[str, Any]:
    payload: dict[str, Any] = {"user_id": "me", "event_id": event_id, "send_response": True}
    if comment.strip():
        payload["comment"] = comment.strip()
    return execute_outlook_action(
        "OUTLOOK_DECLINE_EVENT",
        payload,
        confirm=True,
        send_channel="kory",
    )


def find_meeting_times(payload: dict[str, Any]) -> dict[str, Any]:
    return execute_read_tool("OUTLOOK_FIND_MEETING_TIMES", payload)


def get_calendar_schedule(payload: dict[str, Any]) -> dict[str, Any]:
    return execute_read_tool("OUTLOOK_GET_SCHEDULE", payload)


def cancel_calendar_event(event_id: str, *, comment: str = "") -> dict[str, Any]:
    args: dict[str, Any] = {"user_id": "me", "event_id": event_id}
    if comment.strip():
        args["comment"] = comment.strip()
    return execute_outlook_action(
        "OUTLOOK_CANCEL_EVENT",
        args,
        confirm=True,
        send_channel="kory",
    )


def update_calendar_event(event_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    args = {"user_id": "me", "event_id": event_id, **updates}
    return execute_outlook_action(
        "OUTLOOK_UPDATE_CALENDAR_EVENT",
        args,
        confirm=True,
        send_channel="kory",
    )
