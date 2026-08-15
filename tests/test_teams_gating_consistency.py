"""Teams-push gating consistency (plan Phase 5).

Daily briefing, 24h reminders, and hold-release notifications must all respect
teams_push_allowed() (which includes the dry-run check) — not just the
suppress-push flag — so nothing pushes to Kory during a dry-run.
"""

from __future__ import annotations

from unittest.mock import patch

from app.jobs import kory_briefings as kb
from app.jobs import hold_lifecycle as hl



def test_24h_reminder_not_pushed_when_teams_push_disallowed():
    with patch("app.safety.outbound_guard.teams_push_allowed", return_value=False):
        with patch("app.bot.teams_publisher.push_approval_text_to_teams") as mock_push:
            kb._notify_kory_24h_reminder(
                proposal_id=1, subject="s", sender="x@y.com", status="pending_approval"
            )
    mock_push.assert_not_called()


def test_hold_release_not_pushed_when_teams_push_disallowed():
    with patch("app.safety.outbound_guard.teams_push_allowed", return_value=False):
        with patch("app.bot.teams_publisher.push_approval_text_to_teams") as mock_push:
            hl._maybe_notify_holds_released(
                {"subject": "s", "sender": "x@y.com", "proposal_id": 1},
                ["2026-08-24T10:00:00-06:00"],
            )
    mock_push.assert_not_called()
