"""Blocked scheduling escalates to Kory in Teams — Kory is the only escalation
target (decision 2026-08-04: the Heidi escalation path was removed entirely)."""

from __future__ import annotations

import re
from typing import Any

from app.safety.outbound_guard import teams_push_allowed
from app.scheduling.hermes_compose import build_scheduling_context_packet, compose_kory_guidance_with_hermes
from app.scheduling.proposal_state import ProposalStatus, transition
from app.storage.lexi_db import get_lexi_connection


def _scrub_third_party_mentions(text: str) -> str:
    """Kory-facing messages must never suggest handing the issue to someone
    else — issues route to Kory only. Drops any sentence naming a hand-off
    target; returns a plain ask if scrubbing would leave nothing."""
    if not text:
        return text
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    kept = [s for s in sentences if "heidi" not in s.lower()]
    scrubbed = " ".join(kept).strip()
    return scrubbed or "Scheduling needs your input — reply here with how you'd like to handle it."


def escalate_to_kory(
    proposal_id: int,
    *,
    reason: str = "",
    failure_error: str = "",
) -> dict[str, Any]:
    """Route a blocked-scheduling situation to Kory in Teams."""
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
    summary = _with_actionable_footer(summary, proposal_id)
    if teams_push_allowed():
        from app.bot.teams_publisher import schedule_teams_scheduling_guidance_push

        schedule_teams_scheduling_guidance_push(proposal_id, summary=summary)
    _mark_needs_kory(proposal_id, summary)
    return {
        "ok": True,
        "path": "kory_notification",
        "proposal_id": proposal_id,
        "summary": summary,
        # Callers (e.g. comms_agent send-failure path) read kory_message —
        # provide the scrubbed Kory-facing text.
        "kory_message": summary,
        "teams_pushed": teams_push_allowed(),
    }


def _with_actionable_footer(summary: str, proposal_id: int) -> str:
    """Escalations are proactive pushes — Kory's reply arrives WITHOUT this
    message in the agent's context, so a bare "YES" is unanswerable (live D6).
    Every escalation therefore ends with self-contained, #N-anchored replies."""
    footer = (
        f"How to respond: reply with guidance (e.g. \"try the week after\"), "
        f"or \"reject #{proposal_id} — reason\" to drop it."
    )
    text = (summary or "").strip()
    if f"#{proposal_id}" in text:
        return text
    return f"{text}\n\n{footer}"


def _mark_needs_kory(proposal_id: int, summary: str) -> None:
    try:
        with get_lexi_connection() as conn:
            transition(
                conn,
                proposal_id,
                to=ProposalStatus.NEEDS_KORY,
                reason=summary or "Escalated to Kory.",
                actor="lexi",
            )
            conn.commit()
    except Exception:
        pass  # never block notification on a status write
