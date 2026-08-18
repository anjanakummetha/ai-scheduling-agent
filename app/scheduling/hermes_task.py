"""Hermes-guided delegation scheduling — unified orchestrator + mandatory engine."""

from __future__ import annotations

from typing import Any

from app.scheduling.hermes_orchestrator import orchestrate_proposal_scheduling
from app.scheduling.proposal_state import ProposalStatus
from app.scheduling.session_sync import sync_scheduling_session_for_proposal

PENDING_APPROVAL = ProposalStatus.PENDING_APPROVAL


def run_delegation_scheduling_task(proposal_id: int, bundle: dict[str, Any]) -> dict[str, Any]:
    """Full delegation path: session sync → Hermes orchestrator → engine → draft (no send)."""
    sync_scheduling_session_for_proposal(proposal_id)
    result = orchestrate_proposal_scheduling(proposal_id, bundle, compose_draft=True)
    if result.get("ok") and result.get("status") == PENDING_APPROVAL:
        return {
            "ok": True,
            "proposal_id": proposal_id,
            "status": PENDING_APPROVAL,
            "path": result.get("path") or result.get("scheduling", {}).get("path"),
            "voice_mode": result.get("voice_mode"),
            "message": "Draft staged for approval.",
            "kory_message": result.get("kory_message") or "Draft is ready — review the times on the card.",
            "scheduling": result.get("scheduling"),
        }
    return result
