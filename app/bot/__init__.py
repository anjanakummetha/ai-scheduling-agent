"""Teams push surface for Lexi (text-only mode is the supported surface).

The in-repo Bot Framework server (LexiTeamsBot) was removed 2026-08-15: the
Hermes gateway owns /api/messages in production, and the bot class had no
runner anywhere. Cards remain parked per the 2026-08-08 ruling.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "push_approval_card_for_proposal_id",
    "push_approval_card_to_teams",
    "schedule_teams_approval_push",
]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from app.bot import teams_publisher

        return getattr(teams_publisher, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
