"""Kory morning briefings — unanswered mail, today's calendar, pre-meeting briefs."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.agents.inbound_filter import is_newsletter_or_bulk_mail, is_no_reply_needed_mail
from app.config import settings
from app.scheduling.introducer import (
    format_introducer_line,
    resolve_introducer_for_contact,
)

_KORY_LOCAL_PARTS = ("kory", "kory.mitchell")
_ACTION_CUES = ("?", "let me know", "please", "can you", "could you", "waiting", "when works")


def _kory_tz() -> ZoneInfo:
    try:
        return ZoneInfo(settings.scheduling_timezone)
    except Exception:
        return ZoneInfo("America/Denver")


def _is_from_kory(sender: str | None) -> bool:
    low = (sender or "").lower()
    if not low:
        return False
    for email in settings.kory_sender_emails:
        if email in low:
            return True
    return any(part in low for part in _KORY_LOCAL_PARTS)


def _needs_kory_reply(*, sender: str, subject: str, preview: str) -> bool:
    if _is_from_kory(sender):
        return False
    if is_no_reply_needed_mail(sender=sender, subject=subject, body=preview):
        return False
    if is_newsletter_or_bulk_mail(sender=sender, subject=subject, body=preview):
        return False
    text = f"{subject}\n{preview}".lower()
    if any(cue in text for cue in _ACTION_CUES):
        return True
    if re.search(r"\b(schedule|meet|coffee|call|intro|connect|available)\b", text):
        return True
    return "?" in text


_QUOTED_CHAIN_RE = re.compile(
    r"(On\s+\w{3,9},?\s+\w{3,9}\s+\d{1,2},?\s+\d{4}\b.*"  # "On Tue, Jul 21, 2026 at ..."
    r"|On\s.+?\bwrote:"                                    # "On ... wrote:"
    r"|From:\s.+"                                          # forwarded/quoted headers
    r"|-{3,}\s*Original Message"                           # Outlook reply divider
    r"|_{5,})",                                            # Outlook underscore divider
    re.IGNORECASE | re.DOTALL,
)


def _clean_snippet(text: str, *, limit: int = 110) -> str:
    """One-line snippet: cut quoted reply chains, collapse whitespace, truncate on a word."""
    s = (text or "").strip()
    match = _QUOTED_CHAIN_RE.search(s)
    if match:
        s = s[: match.start()]
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:—-") + "…"
    return s


def build_unanswered_brief(*, hours: int = 72, limit: int = 12) -> dict[str, Any]:
    """Emails that look relevant where Kory hasn't replied yet."""
    from app.integrations.outlook_inbox import search_inbox

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    try:
        messages, log_id = search_inbox(top=50)
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "kory_message": f"Couldn't read your inbox ({type(exc).__name__}). Try again shortly.",
        }

    items: list[dict[str, Any]] = []
    for msg in messages:
        received_raw = msg.get("received_at") or ""
        try:
            received = datetime.fromisoformat(received_raw.replace("Z", "+00:00"))
            if received.tzinfo is None:
                received = received.replace(tzinfo=timezone.utc)
        except ValueError:
            received = datetime.now(timezone.utc)
        if received < cutoff:
            continue
        sender = str(msg.get("sender") or "")
        subject = str(msg.get("subject") or "(no subject)")
        preview = str(msg.get("preview") or "")
        if not _needs_kory_reply(sender=sender, subject=subject, preview=preview):
            continue
        items.append(
            {
                "subject": subject,
                "sender": sender,
                "sender_name": msg.get("sender_name"),
                "received_at": received_raw,
                "preview": preview[:200],
            }
        )

    lines = [f"**Unanswered — last {hours} hours**\n"]
    if not items:
        lines.append("_No obvious unanswered threads in this window._")
    else:
        for row in items[:limit]:
            who = row.get("sender_name") or row.get("sender") or "unknown"
            snippet = _clean_snippet(str(row.get("preview") or ""))
            line = f"• **{row['subject']}** — {who}"
            if snippet:
                line += f" — _{snippet}_"
            lines.append(line)
            lines.append("")  # blank line between items (Teams markdown needs it)
        if len(items) > limit:
            lines.append(f"_…and {len(items) - limit} more._")

    return {
        "ok": True,
        "count": len(items),
        "composio_log_id": log_id,
        "kory_message": "\n".join(lines),
    }


def build_today_calendar_brief() -> dict[str, Any]:
    """Today's meetings on Kory's calendar (Mountain Time day boundary)."""
    tz = _kory_tz()
    now_local = datetime.now(tz)
    start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    start_iso = start.isoformat()
    end_iso = end.isoformat()

    try:
        from app.integrations.outlook_calendar import get_calendar_events

        events, log_id = get_calendar_events(start_iso, end_iso)
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "kory_message": f"Couldn't load today's calendar ({type(exc).__name__}).",
        }

    meetings = [
        e
        for e in events
        if not e.get("isCancelled")
        and str(e.get("showAs") or "").lower() not in {"free", "workingelsewhere"}
    ]
    meetings.sort(key=lambda e: str(e.get("start") or ""))

    date_label = now_local.strftime("%A, %B %d").replace(" 0", " ")
    lines = [f"**Calendar today — {date_label}**\n"]
    if not meetings:
        lines.append("_No meetings on the calendar today._")
    else:
        for event in meetings[:20]:
            subject = str(event.get("subject") or "(no title)")
            start_t = _format_event_time(event.get("start"), tz)
            line = f"• **{start_t}** — {subject}"
            # Attendees are Graph objects now that the calendar read selects
            # them; str() on those printed the whole dict into the message.
            names = [n for n in _attendee_names(event.get("attendees")) if not _is_from_kory(n)]
            if names:
                shown = ", ".join(names[:3])
                if len(names) > 3:
                    shown += f" +{len(names) - 3}"
                line += f" _(with {shown})_"
            lines.append(line)
            lines.append("")  # blank line between events (Teams markdown needs it)

    return {
        "ok": True,
        "meeting_count": len(meetings),
        "composio_log_id": log_id,
        "events": meetings[:20],
        "kory_message": "\n".join(lines),
    }


def _display_sender(sender: Any) -> str:
    """Human-readable sender from a name/email string, a JSON string, or a Graph
    ``{'emailAddress': {...}}`` object."""
    value = sender
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("{"):
            import ast
            import json

            parsed = None
            try:
                parsed = ast.literal_eval(s)  # handles Python dict repr (single quotes)
            except (ValueError, SyntaxError):
                try:
                    parsed = json.loads(s)
                except ValueError:
                    return s
            value = parsed
        else:
            return s or "unknown"
    if isinstance(value, dict):
        addr = value.get("emailAddress") if isinstance(value.get("emailAddress"), dict) else value
        return str(addr.get("name") or addr.get("address") or "unknown")
    return str(value or "unknown")


def _format_event_time(raw: Any, tz: ZoneInfo) -> str:
    """Event start as a local time string.

    Graph hands back ``{"dateTime": "...", "timeZone": "..."}`` with a NAIVE
    dateTime, and the calendar read has already converted it to Kory's zone.
    Assuming naive meant UTC therefore subtracted the offset a second time and
    showed a 6:30 AM session as 12:30 AM.
    """
    stated_zone = ""
    if isinstance(raw, dict):
        stated_zone = str(raw.get("timeZone") or "")
        raw = raw.get("dateTime") or raw.get("date") or ""
    if not raw:
        return "?"
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return str(raw)[:16]
    if dt.tzinfo is None:
        source = tz
        if stated_zone:
            try:
                source = ZoneInfo(stated_zone)
            except Exception:
                source = tz
        dt = dt.replace(tzinfo=source)
    return dt.astimezone(tz).strftime("%I:%M %p").lstrip("0")


def _attendee_names(attendees: Any) -> list[str]:
    """Display names from Graph attendee objects (or plain strings)."""
    names: list[str] = []
    for attendee in attendees or []:
        if isinstance(attendee, dict):
            email_address = attendee.get("emailAddress") or {}
            label = str(email_address.get("name") or email_address.get("address") or "").strip()
        else:
            label = str(attendee).strip()
        if label and label not in names:
            names.append(label)
    return names


def _guess_external_attendee(event: dict[str, Any]) -> tuple[str, str]:
    """First non-IFG attendee as (email, display name).

    Graph returns attendees as {"emailAddress": {"address", "name"}}; the older
    string handling is kept for any caller that passes plain addresses. Returns
    empty strings when no outside attendee can be identified — the meeting
    subject is NOT a person's name, and passing it as one produced lookups like
    "Teams Call – James Phifer (ACCU Inc) | Matt Maley & Kory Mitchell (IFG)"
    that could never match a contact.
    """
    for attendee in event.get("attendees") or []:
        address = ""
        display = ""
        if isinstance(attendee, dict):
            email_address = attendee.get("emailAddress") or {}
            address = str(email_address.get("address") or "").strip()
            display = str(email_address.get("name") or "").strip()
        else:
            address = str(attendee).strip()
        low = address.lower()
        if not low or "@" not in low:
            continue
        if _is_from_kory(low):
            continue
        if "iconicfounders" in low or "ifg.vc" in low:
            continue
        if display and "@" not in display:
            return low, display
        return low, low.split("@", 1)[0].replace(".", " ").title()
    return "", ""
