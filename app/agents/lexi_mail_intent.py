"""Narrow email-to-lexi@ command router.

Email as a GENERAL command channel was scrapped (decision 2026-08-05, Group N
deferred): Teams is the command surface. Only the three sanctioned commands
from Kory's own address survive here — don't-schedule, remember-a-fact, and
remind-me-to (Asana) — plus a redirect for briefing asks. Everything else gets
a polite pointer to Teams. Do not widen these regexes; resolving free-text
instructions needs the model, and that path lives in Teams.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.config import settings


@dataclass(frozen=True)
class LexiMailIntent:
    intent: str
    reason: str
    instruction: str = ""


_DONT_SCHEDULE_RE = re.compile(
    r"\b(?:don'?t|do not|never|not)\s+(?:want to\s+)?schedule",
    re.IGNORECASE,
)
_ASANA_RE = re.compile(r"\b(?:asana|task|todo|to-do|remind me to)\b", re.IGNORECASE)
_BRIEF_RE = re.compile(
    r"\b(?:briefing|brief me|summary|morning brief|ceo brief)\b",
    re.IGNORECASE,
)
_REMEMBER_RE = re.compile(r"\bremember\s+(?:that\s+)?(.+)", re.IGNORECASE | re.DOTALL)
_FORWARD_RE = re.compile(r"^fw:|^fwd:", re.IGNORECASE)


def _lexi_addresses() -> set[str]:
    addrs: set[str] = set()
    if settings.lexi_mailbox_email:
        addrs.add(settings.lexi_mailbox_email.lower())
    if settings.lexi_cc_emails:
        for part in settings.lexi_cc_emails.split(","):
            if part.strip():
                addrs.add(part.strip().lower())
    return addrs


def _recipient_emails(raw_email: dict[str, Any]) -> set[str]:
    emails: set[str] = set()
    for key in ("to_recipients", "cc_recipients", "recipients"):
        value = raw_email.get(key)
        if isinstance(value, str):
            emails.update(re.findall(r"[\w.+-]+@[\w.-]+\.\w+", value.lower()))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    emails.update(re.findall(r"[\w.+-]+@[\w.-]+\.\w+", item.lower()))
    return emails


def is_mail_to_lexi(raw_email: dict[str, Any]) -> bool:
    """True when lexi@ is a direct To recipient (not only CC on Kory's thread)."""
    lexi_addrs = _lexi_addresses()
    if not lexi_addrs:
        return False
    to_raw = raw_email.get("to_recipients") or raw_email.get("recipients") or []
    to_set: set[str] = set()
    if isinstance(to_raw, str):
        to_set.update(re.findall(r"[\w.+-]+@[\w.-]+\.\w+", to_raw.lower()))
    elif isinstance(to_raw, list):
        for item in to_raw:
            if isinstance(item, str):
                to_set.update(re.findall(r"[\w.+-]+@[\w.-]+\.\w+", item.lower()))
    return bool(lexi_addrs & to_set)


def parse_lexi_mail_intent(*, subject: str, body: str, sender: str = "") -> LexiMailIntent:
    combined = f"{subject}\n{body}".strip()
    if _DONT_SCHEDULE_RE.search(combined):
        return LexiMailIntent("dont_schedule", "explicit_decline", instruction=combined[:500])
    if _BRIEF_RE.search(combined):
        return LexiMailIntent("briefing", "brief_request")
    if _ASANA_RE.search(combined):
        return LexiMailIntent("asana", "asana_request", instruction=combined[:500])
    remember = _REMEMBER_RE.search(combined)
    if remember:
        return LexiMailIntent(
            "remember",
            "kory_memory",
            instruction=remember.group(1).strip()[:500],
        )
    if _FORWARD_RE.match(subject.strip()):
        return LexiMailIntent("forward_instruction", "forwarded_thread", instruction=combined[:800])
    return LexiMailIntent("general", "lexi_direct_mail", instruction=combined[:500])


def handle_lexi_direct_mail(raw_email: dict[str, Any]) -> dict[str, Any]:
    """Handle mail addressed to lexi@ without scheduling triage."""
    subject = str(raw_email.get("subject") or "")
    body = str(raw_email.get("raw_body") or raw_email.get("body") or "")
    sender = str(raw_email.get("sender") or raw_email.get("sender_email") or "")
    thread_id = str(raw_email.get("thread_id") or raw_email.get("outlook_message_id") or "")

    intent = parse_lexi_mail_intent(subject=subject, body=body, sender=sender)

    if intent.intent == "dont_schedule":
        from app.storage.kory_memory import upsert_fact

        note = f"Kory declined scheduling via email: {subject[:120]}"
        upsert_fact(
            fact_key=f"dont_schedule:{thread_id[:40]}",
            fact_value=note,
            source="lexi_email",
        )
        return {
            "handled": True,
            "action": "dont_schedule",
            "message": "Noted — I won't schedule on this thread.",
            "thread_id": thread_id,
        }

    if intent.intent == "remember" and intent.instruction:
        from app.assistant.actions import remember_kory_fact_action

        key = f"email:{thread_id[:32] or subject[:32]}"
        remember_kory_fact_action(key, intent.instruction)
        return {
            "handled": True,
            "action": "remember",
            "message": "Saved to memory.",
            "thread_id": thread_id,
        }

    if intent.intent == "briefing":
        # Retired — the dashboard owns the morning package.
        return {
            "handled": True,
            "action": "briefing",
            "message": (
                "Your morning briefing is on the dashboard now. I can still send "
                "today's calendar, unanswered emails, or a pre-meeting brief."
            ),
            "thread_id": thread_id,
        }

    if intent.intent == "asana":
        from app.integrations.asana_manager import list_asana_tasks

        tasks = list_asana_tasks(bucket="due_today")
        lines = ["**Asana (from your email to Lexi)**\n"]
        for t in tasks.get("tasks", [])[:10]:
            lines.append(f"• {t.get('name')} — due {t.get('due_on') or 'no date'}")
        if not tasks.get("tasks"):
            lines.append("_No tasks due today (or Asana read unavailable)._")
        return {
            "handled": True,
            "action": "asana",
            "message": "\n".join(lines),
            "thread_id": thread_id,
            "tasks": tasks,
        }

    if intent.intent == "forward_instruction":
        if _DONT_SCHEDULE_RE.search(body):
            return handle_lexi_direct_mail(
                {**raw_email, "subject": subject, "raw_body": body}
            )
        return {
            "handled": True,
            "action": "forward_instruction",
            "message": (
                "Got your forward — tell me in Teams what you'd like "
                "(schedule, don't schedule, or an Asana task)."
            ),
            "thread_id": thread_id,
        }

    return {
        "handled": True,
        "action": "lexi_direct_ack",
        "message": (
            "Thanks — I received your note to lexi@. "
            "For scheduling, CC me on the thread with Kory; "
            "or ask in Teams: `today`, `unanswered`, `prebrief`, `pending`."
        ),
        "thread_id": thread_id,
    }
