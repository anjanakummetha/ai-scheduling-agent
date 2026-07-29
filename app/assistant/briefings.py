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


def _research_answer(bundle: dict[str, Any]) -> str:
    """Readable summary out of a research bundle.

    ``web_summary`` is the search payload, normally ``{"answer", "citations"}``
    — not a string. Calling .strip() on it raised AttributeError, which the
    caller swallowed as "research skipped", so background never once appeared.
    """
    raw = bundle.get("web_summary")
    if isinstance(raw, dict):
        raw = raw.get("answer") or raw.get("summary") or ""
    return str(raw or "").strip()


def _research_sources(bundle: dict[str, Any], limit: int = 3) -> list[str]:
    raw = bundle.get("web_summary")
    citations = raw.get("citations") if isinstance(raw, dict) else None
    urls: list[str] = []
    for citation in citations or []:
        url = citation.get("id") if isinstance(citation, dict) else str(citation)
        if url and url not in urls:
            urls.append(str(url))
        if len(urls) >= limit:
            break
    return urls


def _mailbox_history(email: str) -> tuple[bool, str]:
    """(has prior contact, one-line description of the most recent thread)."""
    if not email:
        return False, ""
    try:
        from app.integrations.outlook_inbox import search_inbox

        messages, _ = search_inbox(query=email, top=3)
    except Exception:
        return False, ""
    if not messages:
        return False, ""
    latest = messages[0]
    subject = str(latest.get("subject") or "").strip() or "(no subject)"
    when = str(latest.get("received_at") or "")[:10]
    return True, f"Last thread: *{subject[:70]}*" + (f" — {when}" if when else "")


def _strip_leading_name(block: str, name: str) -> str:
    """Drop the block's own name heading when the caller already printed it."""
    lines = block.splitlines()
    if not lines or not name:
        return block
    first = lines[0].replace("*", "").strip().lower()
    if first.startswith(name.strip().lower()):
        remainder = lines[0].split("—", 1)
        rest = lines[1:]
        if len(remainder) > 1 and remainder[1].strip():
            rest = [remainder[1].strip()] + rest
        return "\n".join(rest).strip()
    return block


def build_prebrief(
    *,
    attendee_name: str = "",
    attendee_email: str = "",
    meeting_subject: str = "",
    include_research: bool | None = None,
) -> dict[str, Any]:
    """Pre-meeting brief for one attendee.

    Research is the point of this for someone Kory has not met, and noise for
    someone he has — so by default (``include_research=None``) it runs only for
    new people, and known contacts get a single line. Pass True or False to
    force it either way.
    """
    email = attendee_email.strip()
    name = attendee_name.strip()

    if not (name or email):
        # Internal meeting — nothing to brief, and no one to look up.
        return {
            "ok": True,
            "skipped": True,
            "found_contact": False,
            "kory_message": "",
        }

    contact: dict[str, Any] | None = None
    hubspot_block = ""
    found_contact = False
    try:
        from app.integrations.hubspot_manager import enrich_prebrief_from_hubspot

        hs = enrich_prebrief_from_hubspot(email=email, name=name)
        if hs.get("ok") and hs.get("found"):
            found_contact = True
            contact = hs.get("contact")
            hubspot_block = hs.get("kory_message", "")
    except Exception:
        pass

    crm_contacted = bool(contact and str(contact.get("notes_last_contacted") or "").strip())
    has_mail, last_thread = _mailbox_history(email)
    met_before = crm_contacted or has_mail
    wants_research = (not met_before) if include_research is None else include_research

    header = f"**{name or email}**"
    if meeting_subject:
        header += f" — {meeting_subject}"
    lines = [header]

    body = _strip_leading_name(hubspot_block, name) if hubspot_block else ""
    if met_before:
        # He knows them: enough to jog his memory, not a page of background.
        if body:
            lines.append(body)
        if last_thread:
            lines.append(last_thread)
        if not body and not last_thread:
            lines.append("_Prior contact on record._")
    else:
        lines.append("🆕 **First meeting** — no prior contact on record.")
        if body:
            lines.append(body)

    intro = resolve_introducer_for_contact(email=email or "guest@unknown.io", sender=name)
    intro_line = format_introducer_line(intro)
    # "Introduced by: Unknown" is noise on every meeting; only show a real one.
    if intro and "unknown" not in intro_line.lower():
        lines.append(intro_line)

    research_ran = False
    if wants_research:
        try:
            from app.integrations.person_research import research_person

            company = str((contact or {}).get("company") or "")
            bundle = research_person(
                name or email.split("@", 1)[0],
                company=company,
                email=email,
                include_inbox=False,
                include_news=False,
            )
            summary = _research_answer(bundle)
            if summary:
                research_ran = True
                lines.append("")
                lines.append(f"**Background:** {summary[:900]}")
                sources = _research_sources(bundle, limit=2)
                if sources:
                    lines.append("_Sources: " + " · ".join(sources) + "_")
        except Exception as exc:
            lines.append(f"\n_Background lookup unavailable ({type(exc).__name__})._")

    return {
        "ok": True,
        "skipped": False,
        "attendee_email": email or None,
        "attendee_name": name or None,
        "found_contact": found_contact,
        "met_before": met_before,
        "research_ran": research_ran,
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

    sections: list[str] = []
    briefed = 0
    internal = 0
    new_people = 0
    for event in events[:8]:
        subject = str(event.get("subject") or "Meeting")
        attendee_email, attendee_name = _guess_external_attendee(event)
        brief = build_prebrief(
            attendee_name=attendee_name,
            attendee_email=attendee_email,
            meeting_subject=subject,
            # None means "research new people only" — the caller can force it.
            include_research=True if include_research else None,
        )
        if brief.get("skipped"):
            internal += 1
            continue
        briefed += 1
        if not brief.get("met_before"):
            new_people += 1
        sections.append(brief.get("kory_message", ""))
        sections.append("")

    if not sections:
        note = "_No external meetings today"
        note += f" ({internal} internal)._" if internal else "._"
        return {
            "ok": True,
            "count": 0,
            "kory_message": f"**Pre-meeting briefs — today**\n\n{note}",
        }

    header = f"**Pre-meeting briefs — today** ({briefed} external"
    if new_people:
        header += f", {new_people} you haven't met"
    header += ")\n"
    if internal:
        sections.append(f"_{internal} internal meeting(s) not briefed._")

    return {
        "ok": True,
        "count": briefed,
        "internal_count": internal,
        "new_people": new_people,
        "kory_message": header + "\n" + "\n".join(sections).strip(),
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
