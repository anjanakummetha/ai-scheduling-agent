"""Escalate blocked scheduling to Heidi with a structured briefing."""

from __future__ import annotations

import json
import re
from typing import Any

from app.config import settings
from app.safety.outbound_guard import heidi_email_allowed, staging_mode_label, teams_push_allowed
from app.scheduling.hermes_compose import build_scheduling_context_packet, compose_kory_guidance_with_hermes
from app.storage.lexi_db import get_lexi_connection

HEIDI_ESCALATION_PREFIX = "HEIDI_ESCALATION"
NEEDS_HEIDI = "needs_heidi"


def heidi_escalation_enabled() -> bool:
    """Off by default: blocked scheduling notifies Kory in Teams, not Heidi.

    The Heidi path is kept behind this flag for later re-enable.
    """
    import os

    return os.getenv("LEXI_HEIDI_ESCALATION_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _scrub_third_party_mentions(text: str) -> str:
    """Kory-facing messages must not reference Heidi while Heidi escalation is off.

    Drops any sentence naming Heidi (issues route to Kory only). Returns the
    original text if scrubbing would leave nothing.
    """
    if not text or heidi_escalation_enabled():
        return text
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    kept = [s for s in sentences if "heidi" not in s.lower()]
    scrubbed = " ".join(kept).strip()
    return scrubbed or "Scheduling needs your input — reply here with how you'd like to handle it."


def _notify_kory_scheduling_blocked(
    proposal_id: int,
    *,
    reason: str = "",
    failure_error: str = "",
) -> dict[str, Any]:
    """Route a blocked-scheduling situation to Kory in Teams (Heidi escalation disabled)."""
    packet = build_scheduling_context_packet(proposal_id)
    subject = str(packet.get("subject") or "thread") if packet.get("ok") else "thread"
    sender = str(packet.get("sender") or "sender") if packet.get("ok") else "sender"
    detail = (failure_error or reason or "no preference-compliant time was found").strip()
    intent = str(packet.get("intent_classification") or "") if packet.get("ok") else ""
    # Reasoning-based Teams message: explains the specific blocker and offers 2-3
    # concrete options, rather than a generic "reply here". Falls back to a plain
    # message if the LLM is unavailable.
    try:
        summary = compose_kory_guidance_with_hermes(
            proposal_id, failure_error=detail, intent=intent
        )
    except Exception:
        summary = ""
    if not summary or len(summary.strip()) < 20:
        summary = (
            f"Scheduling needs your input — \"{subject}\" from {sender}. "
            f"{detail}. Reply here with how you'd like to handle it."
        )
    summary = _scrub_third_party_mentions(summary)
    if teams_push_allowed():
        from app.bot.teams_publisher import schedule_teams_scheduling_guidance_push

        schedule_teams_scheduling_guidance_push(proposal_id, summary=summary)
    _mark_needs_kory(proposal_id, summary)
    return {
        "ok": True,
        "path": "kory_notification",
        "proposal_id": proposal_id,
        "summary": summary,
        # Callers (e.g. comms_agent send-failure path) read kory_message — provide
        # the scrubbed Kory-facing text so no Heidi default leaks through.
        "kory_message": summary,
        "teams_pushed": teams_push_allowed(),
    }


def _mark_needs_kory(proposal_id: int, summary: str) -> None:
    try:
        with get_lexi_connection() as conn:
            conn.execute(
                "UPDATE proposals SET status = ? WHERE id = ?",
                ("needs_kory", proposal_id),
            )
            conn.commit()
    except Exception:
        pass  # never block notification on a status write


def resolve_heidi_email() -> str:
    import os

    return (
        os.getenv("HEIDI_ESCALATION_EMAIL", "").strip()
        or "anjana.kummetha@iconicfounders.com"
    )


def compose_heidi_briefing(
    packet: dict[str, Any],
    *,
    failure_error: str = "",
    reason: str = "",
) -> str:
    """Plain-text briefing for Heidi (Hermes may refine when LLM available)."""
    if not packet.get("ok"):
        return reason or failure_error or "Scheduling could not be completed."

    subject = packet.get("subject") or "(no subject)"
    sender = packet.get("sender") or "unknown"
    meeting = packet.get("meeting_type_label") or packet.get("intent_classification") or "meeting"
    window = (
        packet.get("kory_scheduling_guidance")
        or packet.get("requested_window")
        or "requested window in email"
    )
    failure = (failure_error or reason or "").strip()
    tried = packet.get("scheduler_failure") or failure
    rules = packet.get("scheduling_rules_summary") or ""
    thread = (packet.get("latest_inbound_body") or "")[:800]

    guidance = ""
    if settings.llm_api_key:
        try:
            guidance = compose_kory_guidance_with_hermes(
                int(packet["proposal_id"]),
                failure_error=tried,
                intent=str(packet.get("intent_classification") or ""),
            )
        except Exception:
            guidance = ""

    escalate_to = resolve_heidi_email().split("@")[0].replace(".", " ").title()
    lines = [
        f"Hi {escalate_to},",
        "",
        f"Lexi needs help scheduling with {sender}.",
        "",
        f"Thread: {subject}",
        f"Meeting type: {meeting}",
        f"Window asked: {window}",
        "",
        "What they wrote:",
        thread.strip() or "(no body)",
        "",
        "Why Lexi could not complete this:",
        tried or "No valid times met Kory's calendar and scheduling rules.",
        "",
    ]
    if rules:
        lines.extend([f"Rules in play: {rules}", ""])
    if guidance and guidance != tried:
        lines.extend(["Suggested next steps:", guidance, ""])
    lines.extend(
        [
            "Please take over or reply with guidance for Lexi.",
            "",
            "— Lexi (automated escalation)",
        ]
    )
    return "\n".join(lines)


def escalate_to_heidi(
    proposal_id: int,
    *,
    reason: str = "",
    failure_error: str = "",
) -> dict[str, Any]:
    """Blocked scheduling handler. Default: notify Kory in Teams. When
    LEXI_HEIDI_ESCALATION_ENABLED=true: email Heidi (staged when dry-run)."""
    if not heidi_escalation_enabled():
        return _notify_kory_scheduling_blocked(
            proposal_id, reason=reason, failure_error=failure_error
        )

    packet = build_scheduling_context_packet(proposal_id)
    if packet.get("ok"):
        packet["scheduler_failure"] = (failure_error or reason or "").strip()
        packet["proposal_id"] = proposal_id

    briefing = compose_heidi_briefing(packet, failure_error=failure_error, reason=reason)
    subject_line = f"Lexi — scheduling help: {(packet.get('subject') or 'thread')[:70]}"
    kory_cc = _kory_cc_for_heidi()

    if kory_cc:
        kory_ping = (
            f"Escalating to Heidi — {(packet.get('subject') or 'thread')} "
            f"from {packet.get('sender') or 'sender'}. "
            f"You will be CC'd on the email to Heidi."
        )
    else:
        kory_ping = (
            f"Escalating to Heidi — {(packet.get('subject') or 'thread')} "
            f"from {packet.get('sender') or 'sender'}."
        )
    if teams_push_allowed():
        from app.bot.teams_publisher import schedule_teams_scheduling_guidance_push

        schedule_teams_scheduling_guidance_push(proposal_id, summary=kory_ping)

    send_result: dict[str, Any] = {"mode": staging_mode_label(), "sent": False}
    if heidi_email_allowed():
        send_result = _send_heidi_email(
            subject=subject_line,
            body=briefing,
            proposal_id=proposal_id,
            thread_subject=str(packet.get("subject") or ""),
            cc_emails=kory_cc,
        )
    else:
        send_result = {
            "mode": "staged",
            "sent": False,
            "dry_run": True,
            "to": resolve_heidi_email(),
            "cc": kory_cc,
            "subject": subject_line,
            "body_preview": briefing[:500],
        }

    forward_result = _forward_thread_to_heidi(
        proposal_id,
        briefing=briefing,
        packet=packet if packet.get("ok") else {},
    )

    _mark_escalated(
        proposal_id,
        briefing=briefing,
        send_result={**send_result, "forward": forward_result},
    )

    forward_note = ""
    if forward_result.get("forwarded"):
        forward_note = " Original thread forwarded to Heidi."
    elif forward_result.get("dry_run"):
        forward_note = " Thread forward staged (dry-run)."

    return {
        "ok": True,
        "proposal_id": proposal_id,
        "status": NEEDS_HEIDI,
        "path": "heidi_escalation",
        "heidi_email": send_result,
        "heidi_forward": forward_result,
        "briefing": briefing,
        "kory_message": kory_ping + forward_note
        if teams_push_allowed()
        else "Escalated to Heidi (staged — no Teams/email sent in dry-run).",
        "message": "Escalated to Heidi for manual scheduling.",
    }


def _kory_cc_for_heidi() -> list[str]:
    if not settings.heidi_escalation_cc_kory:
        return []
    from app.integrations.outlook_email import kory_cc_addresses

    return kory_cc_addresses()


def _forward_thread_to_heidi(
    proposal_id: int,
    *,
    briefing: str,
    packet: dict[str, Any],
) -> dict[str, Any]:
    """Forward the original scheduling thread to Heidi so she can take over."""
    import re

    from app.integrations.outlook_email import (
        forward_message_from_lexi_mailbox,
        resolve_lexi_reply_message_id,
    )

    with get_lexi_connection() as conn:
        row = conn.execute(
            """
            SELECT
                p.reply_message_id,
                p.thread_id,
                e.conversation_id,
                e.sender
            FROM proposals AS p
            INNER JOIN email_threads AS e ON e.thread_id = p.thread_id
            WHERE p.id = ?
            """,
            (proposal_id,),
        ).fetchone()
    if not row:
        return {"forwarded": False, "reason": "proposal_not_found"}

    sender = str(row["sender"] or packet.get("sender") or "")
    match = re.search(r"[\w.+-]+@[\w.-]+\.\w+", sender)
    intended = match.group(0).lower() if match else None
    conversation_id = str(row["conversation_id"] or "").strip() or None
    anchor = str(row["reply_message_id"] or row["thread_id"] or "").strip()
    if not anchor:
        return {"forwarded": False, "reason": "no_thread_anchor"}

    try:
        lexi_message_id = resolve_lexi_reply_message_id(
            anchor,
            conversation_id=conversation_id,
            intended_recipient=intended,
        )
    except Exception as exc:
        return {"forwarded": False, "reason": f"lexi_thread_not_found: {exc}"}

    subject = str(packet.get("subject") or "scheduling thread")
    escalate_to = resolve_heidi_email().split("@")[0].replace(".", " ").title()
    comment = (
        f"Hi {escalate_to},\n\n"
        f"Lexi could not complete scheduling on \"{subject}\" and needs you to take over.\n\n"
        f"{briefing[:2000]}\n\n"
        "— Lexi (automated escalation)"
    )
    return forward_message_from_lexi_mailbox(
        lexi_message_id,
        to_email=resolve_heidi_email(),
        comment=comment,
        cc_emails=_kory_cc_for_heidi(),
    )


def _send_heidi_email(
    *,
    subject: str,
    body: str,
    proposal_id: int,
    thread_subject: str,
    cc_emails: list[str] | None = None,
) -> dict[str, Any]:
    from app.integrations.outlook_email import send_outbound_email

    to_addr = resolve_heidi_email()
    cc = cc_emails or _kory_cc_for_heidi()
    try:
        message_id, log_id = send_outbound_email(
            to_email=to_addr,
            subject=subject,
            body=body,
            approved_send=True,
            send_channel="lexi",
            cc_emails=cc,
        )
        return {
            "sent": bool(message_id),
            "to": to_addr,
            "cc": cc,
            "message_id": message_id,
            "log_id": log_id,
            "dry_run": str(message_id or "").startswith("dry-run"),
        }
    except Exception as exc:
        return {"sent": False, "error": str(exc), "to": to_addr, "cc": cc}


def _mark_escalated(
    proposal_id: int,
    *,
    briefing: str,
    send_result: dict[str, Any],
) -> None:
    note = f"{HEIDI_ESCALATION_PREFIX}: {briefing[:2000]}"
    with get_lexi_connection() as conn:
        conn.execute(
            """
            UPDATE proposals
            SET status = ?,
                drafted_reply = NULL,
                proposed_slots = NULL,
                teams_approval_notified_at = NULL,
                scheduling_note = ?,
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (NEEDS_HEIDI, note, proposal_id),
        )
        conn.execute(
            """
            INSERT INTO audit_log (step_name, reference_id, log_level, message, payload)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "heidi_escalation",
                str(proposal_id),
                "INFO",
                "Escalated blocked scheduling to Heidi.",
                json.dumps(
                    {"briefing_preview": briefing[:400], "send": send_result},
                    default=str,
                ),
            ),
        )
        conn.commit()
