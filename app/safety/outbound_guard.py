"""Gate outbound Teams, email, and calendar writes during UAT / dry-run."""

from __future__ import annotations

import os

from app.config import settings


def _truthy(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).lower() in {"1", "true", "yes"}


def outbound_writes_allowed() -> bool:
    """False when LEXI_DRY_RUN — no Composio mail/calendar writes."""
    return not settings.lexi_dry_run


def teams_push_allowed() -> bool:
    """False during dry-run or when Teams push is explicitly suppressed.

    LEXI_FORCE_TEAMS_PUSH overrides the dry-run block for live Teams UAT: cards
    push to Teams so the approval UX can be exercised, while the underlying sends
    stay dry-run-simulated (nothing actually leaves a mailbox).
    """
    if not settings.lexi_teams_enabled:
        return False
    if _truthy("LEXI_SUPPRESS_TEAMS_PUSH"):
        return False
    if getattr(settings, "lexi_suppress_teams_push", False):
        return False
    if _truthy("LEXI_FORCE_TEAMS_PUSH"):
        return True
    if settings.lexi_dry_run:
        return False
    return True


def escalation_email_allowed() -> bool:
    """Escalation emails respect dry-run (stage only). Unused since the
    escalation path became Teams-to-Kory only; kept for the dry-run script."""
    return outbound_writes_allowed()


def staging_mode_label() -> str:
    if settings.lexi_dry_run:
        return "dry_run"
    if not teams_push_allowed():
        return "teams_suppressed"
    return "live"
