"""Pre-call briefs — the format Kory actually uses before meeting someone new.

Shape (from the brief Kory shared):

    Name — Pre-Call Brief
    Title | Company
    Who They Are     <- web research
    What They Do     <- web research
    Your Prior Relationship  <- Outlook history only, bulleted
    Angle for the Call
    Cell | Email

Two sources carry it: web research for who the person is, and Kory's own
mailbox for how he knows them. HubSpot contributes a single line — Kory can ask
for the CRM detail separately when he wants it.

The one rule that matters: public background may come from the web, but every
relationship claim must come from the email record. Inventing how Kory knows
someone is worse than saying we don't know.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.config import settings

_INVITE_MARKERS = (
    "invitation:",
    "updated invitation:",
    "accepted:",
    "declined:",
    "tentatively accepted:",
    "canceled:",
    "cancelled:",
    "reminder:",
    "rsvp",
)
_AUTOMATED_SENDERS = (
    "calendar-notification@",
    "noreply@",
    "no-reply@",
    "donotreply@",
    "notifications@",
    "mailer-daemon@",
)


def _is_calendar_noise(message: dict[str, Any]) -> bool:
    """Calendar invites and reminders are not evidence of a relationship."""
    subject = str(message.get("subject") or "").strip().lower()
    sender = str(message.get("sender") or "").strip().lower()
    if any(sender.startswith(prefix) for prefix in _AUTOMATED_SENDERS):
        return True
    return any(subject.startswith(marker) for marker in _INVITE_MARKERS)


def gather_relationship_context(email: str, name: str = "") -> dict[str, Any]:
    """Kory's own mail with this person, both directions, oldest first."""
    threads: list[dict[str, Any]] = []
    try:
        from app.integrations.outlook_inbox import search_inbox

        received, _ = search_inbox(query=email or name, top=25)
        for message in received or []:
            threads.append(
                {
                    "direction": "from_them"
                    if str(message.get("sender") or "").lower() == email.lower()
                    else "inbound_other",
                    "subject": message.get("subject"),
                    "from": message.get("sender"),
                    "date": str(message.get("received_at") or "")[:10],
                    "preview": str(message.get("preview") or "")[:400],
                    "calendar_noise": _is_calendar_noise(message),
                }
            )
    except Exception:
        pass

    try:
        from app.integrations.outlook_sent import fetch_sent_to_recipient

        for message in fetch_sent_to_recipient(email, top=5) or []:
            threads.append(
                {
                    "direction": "from_kory",
                    "subject": message.get("subject"),
                    "date": str(message.get("sent_at") or message.get("date") or "")[:10],
                    "preview": str(message.get("body") or message.get("preview") or "")[:400],
                    "calendar_noise": False,
                }
            )
    except Exception:
        pass

    threads.sort(key=lambda t: t.get("date") or "")
    real = [t for t in threads if not t.get("calendar_noise")]
    return {
        "threads": threads,
        "real_message_count": len(real),
        "has_real_history": bool(real),
        "first_contact": real[0] if real else None,
    }


def _light_hubspot(email: str, name: str) -> dict[str, Any]:
    """One line of CRM context — not the headline.

    Also backfills the email address when Kory asked by name, since the mailbox
    and research lookups both need one.
    """
    try:
        from app.integrations.hubspot_manager import enrich_prebrief_from_hubspot

        found = enrich_prebrief_from_hubspot(email=email, name=name)
        if found.get("ambiguous"):
            return {"ambiguous": True, "kory_message": found.get("kory_message", "")}
        if not (found.get("ok") and found.get("found")):
            return {}
        contact = found.get("contact") or {}
        open_deals = [d for d in (found.get("deals") or []) if d.get("is_open")]
        return {
            "in_hubspot": True,
            "email": contact.get("email"),
            "name": contact.get("name"),
            "title": contact.get("jobtitle"),
            "company": contact.get("company"),
            "phone": contact.get("phone"),
            "lead_status": contact.get("hs_lead_status"),
            "do_not_contact": bool(found.get("do_not_contact")),
            "open_deal": (
                f"{open_deals[0].get('dealname')} ({open_deals[0].get('stage_label')})"
                if open_deals
                else None
            ),
        }
    except Exception:
        return {}


def _research(name: str, email: str, company: str) -> dict[str, Any]:
    try:
        from app.integrations.person_research import research_person

        bundle = research_person(
            name or email.split("@", 1)[0],
            company=company,
            email=email,
            include_inbox=False,
            include_news=True,
        )
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    raw = bundle.get("web_summary")
    answer = raw.get("answer") if isinstance(raw, dict) else raw
    citations: list[str] = []
    if isinstance(raw, dict):
        for citation in raw.get("citations") or []:
            url = citation.get("id") if isinstance(citation, dict) else str(citation)
            if url and url not in citations:
                citations.append(str(url))
    news = bundle.get("recent_news")
    news_answer = news.get("answer") if isinstance(news, dict) else news
    return {
        "ok": bool(str(answer or "").strip()),
        "background": str(answer or "").strip(),
        "recent_news": str(news_answer or "").strip()[:800],
        "sources": citations[:3],
    }


_SYSTEM = """You write pre-call briefs for Kory Mitchell, CEO of Iconic Founders Group.

Produce EXACTLY these sections, in this order, using this markdown:

**Who They Are** <2-4 sentences: current role, what they run, career history, location, education, board/affiliations>

**What They Do** <2-3 sentences: what their business or team actually does day to day>

**Your Prior Relationship**
• <bullet per real interaction: when, who reached out, what was said — quote briefly where useful>
• <scheduling history if any: rescheduled, confirmed times>

**Angle for the Call** <2-3 sentences: why this meeting matters and what is worth exploring>

Hard rules:
- "Who They Are" and "What They Do" come ONLY from the research block. If research
  is empty, write "Limited public information available." and nothing more.
- "Your Prior Relationship" comes ONLY from the email history block. Never invent
  how Kory knows someone. If there is no real correspondence, write exactly:
  "• First contact — no prior email history."
- Calendar invites and reminders are not a relationship. Do not present them as one,
  though you may mention scheduling facts (a call was moved, a time was confirmed).
- Never invent titles, numbers, dates, employers or quotes.
- No preamble, no closing line, no headers other than the four above."""


def build_precall_brief(
    *,
    name: str = "",
    email: str = "",
    meeting_subject: str = "",
    meeting_time: str = "",
) -> dict[str, Any]:
    """Full pre-call brief for one person."""
    name = (name or "").strip()
    email = (email or "").strip()
    if not (name or email):
        return {"ok": False, "error": "name or email required"}

    crm = _light_hubspot(email, name)
    if crm.get("ambiguous"):
        # Several people match that name — asking beats briefing the wrong one.
        return {
            "ok": True,
            "ambiguous": True,
            "kory_message": crm.get("kory_message", ""),
        }
    # Asked by name: take the address off the matched contact so the mailbox
    # and research lookups have something to work with.
    if not email and crm.get("email"):
        email = str(crm["email"])
    display_name = name or str(crm.get("name") or "").strip() or (
        email.split("@", 1)[0] if email else ""
    )
    company = str(crm.get("company") or "")
    history = gather_relationship_context(email, name)
    research = _research(display_name, email, company)

    introducer = None
    try:
        from app.scheduling.introducer import resolve_introducer_for_contact

        first = history.get("first_contact") or {}
        info = resolve_introducer_for_contact(
            email=email or "guest@unknown.io",
            subject=str(first.get("subject") or ""),
            body=str(first.get("preview") or ""),
            sender=str(first.get("from") or name),
        )
        if info and (info.name or "").strip():
            introducer = {"name": info.name, "email": info.email, "source": info.source}
    except Exception:
        introducer = None

    packet = {
        "person": {
            "name": display_name,
            "email": email,
            "title": crm.get("title"),
            "company": crm.get("company"),
        },
        "meeting": {"subject": meeting_subject, "when": meeting_time},
        "research": {
            "background": research.get("background") or "",
            "recent_news": research.get("recent_news") or "",
        },
        "email_history": {
            "has_real_history": history.get("has_real_history"),
            "message_count": history.get("real_message_count"),
            "threads": history.get("threads")[:12],
        },
        "introduced_by": introducer,
        "hubspot": {
            "in_hubspot": bool(crm.get("in_hubspot")),
            "lead_status": crm.get("lead_status"),
            "open_deal": crm.get("open_deal"),
        },
    }

    try:
        from app.llm.hermes_client import get_hermes_client

        client = get_hermes_client()
        response = client.chat.completions.create(
            model=settings.llm_model,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": json.dumps(packet, default=str)},
            ],
            temperature=0.3,
        )
        body = (response.choices[0].message.content or "").strip()
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "kory_message": f"Couldn't build a brief for {display_name} ({type(exc).__name__}).",
        }

    return {
        "ok": True,
        "name": display_name,
        "email": email,
        "has_real_history": history.get("has_real_history"),
        "research_ok": research.get("ok"),
        "introduced_by": introducer,
        "kory_message": _format_brief(
            name=display_name,
            email=email,
            crm=crm,
            introducer=introducer,
            meeting_subject=meeting_subject,
            meeting_time=meeting_time,
            body=body,
            sources=research.get("sources") or [],
        ),
    }


def _format_brief(
    *,
    name: str,
    email: str,
    crm: dict[str, Any],
    introducer: dict[str, Any] | None,
    meeting_subject: str,
    meeting_time: str,
    body: str,
    sources: list[str],
) -> str:
    title = str(crm.get("title") or "").strip()
    company = str(crm.get("company") or "").strip()
    role_line = " | ".join(part for part in (title, company) if part)

    lines = [f"📋 **{name}** — Pre-Call Brief"]
    if role_line:
        lines.append(f"*{role_line}*")
    when = " · ".join(part for part in (meeting_subject, meeting_time) if part)
    if when:
        lines.append(f"_{when}_")
    if introducer:
        lines.append(f"**Introduced by:** {introducer['name']}")
    else:
        lines.append("**Introduced by:** Direct outreach")
    lines.append("")
    lines.append(body.strip())

    if crm.get("do_not_contact"):
        lines.append("\n⚠️ **Marked Do Not Contact in HubSpot.**")
    elif crm.get("open_deal"):
        lines.append(f"\n_HubSpot: {crm['open_deal']}_")

    contact_bits = []
    if crm.get("phone"):
        contact_bits.append(f"📞 {crm['phone']}")
    if email:
        contact_bits.append(f"✉️ {email}")
    if contact_bits:
        lines.append("\n" + " | ".join(contact_bits))
    if sources:
        lines.append(f"_Sources: {' · '.join(sources[:2])}_")
    return "\n".join(lines).strip()


def build_precall_briefs_for_today() -> dict[str, Any]:
    """A brief per external meeting today; internal meetings are skipped."""
    from app.assistant.briefings import _guess_external_attendee, build_today_calendar_brief

    cal = build_today_calendar_brief()
    if not cal.get("ok"):
        return cal
    events = cal.get("events") or []

    sections: list[str] = []
    internal = 0
    for event in events[:6]:
        attendee_email, attendee_name = _guess_external_attendee(event)
        if not (attendee_email or attendee_name):
            internal += 1
            continue
        brief = build_precall_brief(
            name=attendee_name,
            email=attendee_email,
            meeting_subject=str(event.get("subject") or ""),
            meeting_time=_event_time(event),
        )
        if brief.get("ok"):
            sections.append(brief["kory_message"])

    if not sections:
        note = "_No external meetings today"
        note += f" ({internal} internal)._" if internal else "._"
        return {"ok": True, "count": 0, "kory_message": f"**Pre-call briefs — today**\n\n{note}"}

    header = f"**Pre-call briefs — today** ({len(sections)} external)\n"
    footer = f"\n_{internal} internal meeting(s) not briefed._" if internal else ""
    return {
        "ok": True,
        "count": len(sections),
        "internal_count": internal,
        "kory_message": header + "\n\n---\n\n".join(sections) + footer,
    }


def _event_time(event: dict[str, Any]) -> str:
    raw = event.get("start")
    if isinstance(raw, dict):
        raw = raw.get("dateTime") or raw.get("date") or ""
    text = str(raw or "")
    match = re.search(r"T(\d{2}):(\d{2})", text)
    if not match:
        return ""
    hour, minute = int(match.group(1)), match.group(2)
    suffix = "AM" if hour < 12 else "PM"
    hour12 = hour % 12 or 12
    return f"{hour12}:{minute} {suffix}"
