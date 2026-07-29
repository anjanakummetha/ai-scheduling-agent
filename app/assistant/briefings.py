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
            attendees = event.get("attendees") or []
            if attendees:
                names = ", ".join(str(a) for a in attendees[:3])
                line += f" _(with {names})_"
            lines.append(line)
            lines.append("")  # blank line between events (Teams markdown needs it)

    return {
        "ok": True,
        "meeting_count": len(meetings),
        "composio_log_id": log_id,
        "events": meetings[:20],
        "kory_message": "\n".join(lines),
    }


def build_prebrief(
    *,
    attendee_name: str = "",
    attendee_email: str = "",
    meeting_subject: str = "",
    include_research: bool = True,
) -> dict[str, Any]:
    """Single pre-meeting brief with introducer + optional research."""
    email = attendee_email.strip()
    name = attendee_name.strip()
    lines = [f"**Pre-meeting brief**"]
    if meeting_subject:
        lines.append(f"**Meeting:** {meeting_subject}")
    if name or email:
        lines.append(f"**With:** {name or email}")
    else:
        # Internal-only or no attendee list — say so rather than implying we
        # looked someone up and found nothing.
        lines.append("_No outside attendee on the invite._")
        return {"ok": True, "kory_message": "\n".join(lines), "found_contact": False}

    intro = resolve_introducer_for_contact(email=email or "guest@unknown.io", sender=name)
    lines.append(format_introducer_line(intro))

    found_contact = False
    try:
        from app.integrations.hubspot_manager import enrich_prebrief_from_hubspot

        hs = enrich_prebrief_from_hubspot(email=email, name=name)
        if hs.get("ok") and hs.get("found"):
            found_contact = True
            lines.append("")
            lines.append(hs.get("kory_message", ""))
        elif hs.get("ok"):
            lines.append("_Not in HubSpot._")
    except Exception:
        pass

    research_block = ""
    if include_research and (name or email):
        try:
            from app.integrations.person_research import research_person

            bundle = research_person(
                name or email.split("@", 1)[0],
                email=email,
                include_inbox=True,
                include_news=False,
            )
            summary = (bundle.get("web_summary") or "").strip()
            if summary:
                research_block = summary[:1200]
                lines.append("\n**Background:**")
                lines.append(research_block)
            threads = bundle.get("prior_threads") or []
            if threads:
                lines.append("\n**Prior threads:**")
                for t in threads[:3]:
                    lines.append(f"• {t.get('subject') or '(no subject)'}")
        except Exception as exc:
            lines.append(f"\n_Research skipped ({type(exc).__name__})._")

    return {
        "ok": True,
        "attendee_email": email or None,
        "attendee_name": name or None,
        "found_contact": found_contact,
        "introducer": intro.__dict__ if intro else None,
        "kory_message": "\n".join(lines),
    }


def build_prebriefs_for_today(*, include_research: bool = False) -> dict[str, Any]:
    """Prebrief stub for each meeting today (research off by default to save API)."""
    cal = build_today_calendar_brief()
    if not cal.get("ok"):
        return cal

    events = cal.get("events") or []
    if not events:
        return {
            "ok": True,
            "count": 0,
            "kory_message": "**Pre-meeting briefs**\n\n_No meetings today — nothing to brief._",
        }

    sections: list[str] = ["**Pre-meeting briefs — today**\n"]
    for event in events[:8]:
        subject = str(event.get("subject") or "Meeting")
        attendee_email, attendee_name = _guess_external_attendee(event)
        brief = build_prebrief(
            attendee_name=attendee_name,
            attendee_email=attendee_email,
            meeting_subject=subject,
            include_research=include_research,
        )
        sections.append(brief.get("kory_message", ""))
        sections.append("")

    return {
        "ok": True,
        "count": len(events),
        "kory_message": "\n".join(sections).strip(),
    }


def build_daily_ceo_briefing() -> dict[str, Any]:
    """4:45 AM MT package — calendar, unanswered, pending approvals, Asana due."""
    tz = _kory_tz()
    now_local = datetime.now(tz)
    header = f"**CEO briefing — {now_local.strftime('%A, %B %d').replace(' 0', ' ')}**\n"

    parts = [header]

    cal = build_today_calendar_brief()
    parts.append(cal.get("kory_message", ""))
    parts.append("")

    unanswered = build_unanswered_brief(hours=48)
    parts.append(unanswered.get("kory_message", ""))
    parts.append("")

    pending = _pending_approval_summary()
    parts.append(pending)
    parts.append("")

    asana = _asana_due_summary()
    parts.append(asana)
    parts.append("")

    deals = _hubspot_deals_summary()
    parts.append(deals)

    prebrief_note = (
        "\n_Say **prebrief** for meeting context (who introduced + research)._"
    )
    parts.append(prebrief_note)

    return {
        "ok": True,
        "generated_at": now_local.isoformat(),
        "kory_message": "\n".join(parts).strip(),
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


def _pending_approval_summary() -> str:
    from app.agents.comms_agent import get_lexi_pending_queue

    items = get_lexi_pending_queue()
    if not items:
        return "**Pending approvals:** None — you're caught up on Lexi drafts."
    lines = [f"**Pending approvals:** {len(items)} draft(s) waiting\n"]
    for item in items[:5]:
        lines.append(f"• **{item.subject}** — {_display_sender(item.sender)}")
        lines.append("")
    if len(items) > 5:
        lines.append(f"_…and {len(items) - 5} more. Say `pending`._")
    return "\n".join(lines)


def _asana_due_summary() -> str:
    try:
        from app.integrations.asana_manager import summarize_asana_for_briefing

        return summarize_asana_for_briefing()
    except Exception as exc:
        return f"**Asana:** unavailable ({type(exc).__name__})."


def _hubspot_deals_summary() -> str:
    try:
        from app.integrations.hubspot_manager import deals_snapshot_for_brief

        snap = deals_snapshot_for_brief(limit=5)
        return snap.get("kory_message", "**Deals:** unavailable.")
    except Exception as exc:
        return f"**Deals:** unavailable ({type(exc).__name__})."


def _format_event_time(raw: Any, tz: ZoneInfo) -> str:
    if isinstance(raw, dict):
        raw = raw.get("dateTime") or raw.get("date") or ""
    if not raw:
        return "?"
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(tz).strftime("%I:%M %p").lstrip("0")
    except ValueError:
        return str(raw)[:16]


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
