"""Outlook conversation thread context for chained email replies."""

from __future__ import annotations

import logging
from typing import Any

from app.integrations.composio_client import execute_read_tool
from app.integrations.outlook_email import _plain_text

logger = logging.getLogger(__name__)


def extract_list_messages(data: Any) -> list[dict[str, Any]]:
    """Normalize OUTLOOK_LIST_MESSAGES payloads."""
    return _extract_messages(data)


def _extract_messages(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("value", "messages", "data"):
            nested = data.get(key)
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
    return []


def filter_by_conversation(
    messages: list[dict[str, Any]],
    conversation_id: str,
) -> list[dict[str, Any]]:
    """Keep only messages truly in `conversation_id`.

    Composio's OUTLOOK_LIST_MESSAGES ignores the `filter` argument entirely, so
    it returns the newest messages in the folder regardless of conversation.
    Without this client-side pass, unrelated mail leaks into thread context and
    can be mistaken for part of the thread.
    """
    wanted = (conversation_id or "").strip()
    if not wanted:
        return []
    kept: list[dict[str, Any]] = []
    for message in messages:
        actual = str(message.get("conversationId") or "").strip()
        # Drop only proven mismatches: if the payload omits conversationId there
        # is nothing to contradict, and discarding it would break the thread.
        if actual and actual != wanted:
            continue
        kept.append(message)
    return kept


def _list_conversation_messages(
    conversation_id: str,
    *,
    folder: str,
    top: int = 50,
) -> list[dict[str, Any]]:
    result = execute_read_tool(
        "OUTLOOK_LIST_MESSAGES",
        {
            "user_id": "me",
            "folder": folder,
            "top": top,
            "orderby": ["receivedDateTime desc"],
            "select": ["id", "subject", "from", "receivedDateTime", "bodyPreview", "conversationId"],
            "filter": f"conversationId eq '{conversation_id}'",
        },
    )
    messages = _extract_messages(result.get("data"))
    scoped = filter_by_conversation(messages, conversation_id)
    if len(scoped) != len(messages):
        logger.debug(
            "Conversation %s: dropped %d/%d unrelated messages from folder=%s.",
            conversation_id,
            len(messages) - len(scoped),
            len(messages),
            folder,
        )
    return scoped


def fetch_conversation_context(
    conversation_id: str,
    *,
    exclude_message_id: str | None = None,
    max_messages: int = 4,
) -> str:
    """Load prior messages in the same Outlook conversation for triage/drafting."""
    if not conversation_id.strip():
        return ""

    messages: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for folder in ("inbox", "sentitems"):
        try:
            for message in _list_conversation_messages(conversation_id, folder=folder):
                mid = str(message.get("id") or "")
                if mid and mid in seen_ids:
                    continue
                if mid:
                    seen_ids.add(mid)
                messages.append(message)
        except Exception as exc:
            logger.debug("Thread context fetch failed for folder=%s: %s", folder, exc)

    if not messages:
        return ""

    messages.sort(key=lambda m: str(m.get("receivedDateTime") or ""))

    blocks: list[str] = []
    for message in reversed(messages):
        mid = str(message.get("id") or "")
        if exclude_message_id and mid == exclude_message_id:
            continue
        sender = message.get("from") or {}
        email_address = sender.get("emailAddress", {}) if isinstance(sender, dict) else {}
        from_addr = email_address.get("address") or "unknown"
        received = message.get("receivedDateTime") or ""
        subject = message.get("subject") or ""
        preview = _plain_text(str(message.get("bodyPreview") or ""))[:500]
        blocks.append(
            f"--- Earlier in thread ({received}) ---\n"
            f"From: {from_addr}\n"
            f"Subject: {subject}\n"
            f"{preview}"
        )
        if len(blocks) >= max_messages:
            break

    if not blocks:
        return ""
    return "\n\n".join(reversed(blocks))
