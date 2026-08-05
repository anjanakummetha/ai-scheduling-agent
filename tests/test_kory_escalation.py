"""Kory always CC'd on Lexi sends; blocked scheduling escalates to Kory only."""

from __future__ import annotations

from unittest.mock import patch

from app.integrations.outlook_email import (
    kory_on_thread,
    kory_thread_addresses,
    merge_kory_cc_addresses,
)


def _recip(addr):
    return {"emailAddress": {"address": addr}}


def test_kory_on_thread_detects_kory_in_cc():
    with patch("app.integrations.outlook_email.settings") as s:
        s.kory_cc_email = "Kory.Mitchell@iconicfounders.com"
        s.kory_sender_emails = ("kory@iconicfounders.com",)
        recips = {
            "to_recipients": [_recip("guest@example.com")],
            "cc_recipients": [_recip("kory.mitchell@iconicfounders.com")],
        }
        assert kory_on_thread(recips) is True


def test_kory_on_thread_false_when_absent():
    with patch("app.integrations.outlook_email.settings") as s:
        s.kory_cc_email = "kory.mitchell@iconicfounders.com"
        s.kory_sender_emails = ()
        recips = {
            "to_recipients": [_recip("guest@example.com")],
            "cc_recipients": [_recip("someoneelse@example.com")],
        }
        assert kory_on_thread(recips) is False


def test_merge_kory_cc_addresses_dedupes():
    with patch("app.integrations.outlook_email.settings") as mock_settings:
        mock_settings.cc_kory_enabled = True
        mock_settings.kory_cc_email = "Kory.Mitchell@iconicfounders.com"
        merged = merge_kory_cc_addresses(["kory.mitchell@iconicfounders.com", "other@example.com"])
    # Kory's real CC address is deduped against any existing entry.
    assert merged == ["kory.mitchell@iconicfounders.com", "other@example.com"]


def test_merge_kory_cc_addresses_disabled_returns_existing_only():
    with patch("app.integrations.outlook_email.settings") as mock_settings:
        mock_settings.cc_kory_enabled = False
        mock_settings.kory_cc_email = "kory.mitchell@iconicfounders.com"
        merged = merge_kory_cc_addresses(["other@example.com"])
    assert merged == ["other@example.com"]

def test_escalate_routes_to_kory(monkeypatch):
    """Kory is the only escalation target (Heidi path removed 2026-08-04)."""
    with patch("app.scheduling.kory_escalation.build_scheduling_context_packet") as mock_packet:
        mock_packet.return_value = {
            "ok": True,
            "proposal_id": 7,
            "subject": "TEST intro",
            "sender": "prospect@example.com",
        }
        with patch("app.scheduling.kory_escalation.teams_push_allowed", return_value=False):
            with patch("app.scheduling.kory_escalation._mark_needs_kory"):
                from app.scheduling.kory_escalation import escalate_to_kory

                result = escalate_to_kory(7, failure_error="No compliant slot")
    assert result["path"] == "kory_notification"
    assert "needs your input" in result["summary"].lower()
