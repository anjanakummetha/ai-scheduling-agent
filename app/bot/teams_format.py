"""Polished Teams notification formatting for inbound email prompts."""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import settings
from app.scheduling.email_format import normalize_draft_for_display

MT = ZoneInfo(settings.scheduling_timezone)

_LONE_NEWLINE_RE = re.compile(r"(?<!\n)\n(?!\n)")


def teams_markdown_breaks(text: str) -> str:
    """Teams renders message text as markdown, where a single newline collapses
    into a space — multi-line messages arrive as one run-on paragraph. Double
    every lone newline so each line actually breaks; existing blank lines are
    left alone."""
    return _LONE_NEWLINE_RE.sub("\n\n", text or "")


def display_subject(subject: str | None, *, max_len: int = 72) -> str:
    """Clean subject for Teams display (no Re:/Fw:, trimmed)."""
    text = (subject or "(no subject)").strip()
    text = re.sub(r"^(re|fw|fwd):\s*", "", text, flags=re.IGNORECASE).strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def display_sender(sender: str | None) -> str:
    """Prefer the learned display name; fall back to the address's local part.

    This renders Teams card titles, so deriving straight from the local part put
    "Anjanakummetha" in front of Kory while the profile store already held
    "Anjana Kummetha".
    """
    raw = (sender or "unknown").strip()
    if "@" not in raw:
        return raw
    from app.storage.recipient_profiles import display_name_for_email

    return display_name_for_email(raw)


def format_received_at(received_at: str | None) -> str:
    if not received_at:
        return "—"
    try:
        dt = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
        local = dt.astimezone(MT)
        return local.strftime("%a %b %-d, %Y · %-I:%M %p MT")
    except ValueError:
        return received_at


def format_reply_prompt_card_text(item: dict) -> str:
    """Minimal Teams text: subject, from, ask to draft."""
    subject = display_subject(item.get("subject"))
    sender = display_sender(item.get("sender"))

    return (
        f"**{subject}**\n"
        f"From {sender}\n\n"
        "Should I draft a reply?"
    )


def format_draft_ready_text(
    *,
    subject: str | None,
    sender: str | None,
    draft: str,
    slots: list | None = None,
    voice_mode: str = "kory",
    proposal_id: int | None = None,
    scheduling_note: str = "",
) -> str:
    """Clean draft preview for Teams after Kory says yes."""
    from app.scheduling.email_format import format_slot_for_email, infer_recipient_timezone

    title = display_subject(subject)
    body = normalize_draft_for_display(draft, max_chars=None, voice_mode=voice_mode)
    header = f"**{title}**" if proposal_id is None else f"**#{proposal_id} — {title}**"
    lines = [
        header,
        f"From {display_sender(sender)}",
    ]
    note = (scheduling_note or "").strip()
    if note:
        # The gate's caveat (e.g. "no availability in the requested week —
        # offering the following week"). Kory must see it before approving.
        lines.append(f"⚠️ {note}")
    lines.extend(["", body])
    if slots:
        recipient_tz = infer_recipient_timezone(sender)
        lines.append("")
        lines.append("**Times offered**")
        for index, slot in enumerate(slots[:3], start=1):
            lines.append(
                f"{index}. {format_slot_for_email(slot, recipient_tz=recipient_tz)}"
            )
    ref = f"#{proposal_id}" if proposal_id is not None else "#N"
    lines.extend(
        [
            "",
            f"_Not sent._ Reply **approve {ref}** to send, **reject {ref} — reason** "
            "to discard, or tell me what to change in the draft.",
        ]
    )
    return "\n".join(lines)
