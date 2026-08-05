"""Regression tests for the four live-UAT scheduling bugs (2026-07-24)."""

import os
from importlib import reload


def _reload_with_lexi_mailbox(email: str):
    """Set LEXI_MAILBOX_EMAIL and reload config + delegation; return previous value."""
    key = "LEXI_MAILBOX_EMAIL"
    prev = os.environ.get(key)
    os.environ[key] = email
    import app.agents.delegation as delegation_mod
    import app.config as config_mod

    reload(config_mod)
    reload(delegation_mod)
    return prev


def _restore_lexi_mailbox(prev: str | None) -> None:
    key = "LEXI_MAILBOX_EMAIL"
    if prev is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = prev
    import app.agents.delegation as delegation_mod
    import app.config as config_mod

    reload(config_mod)
    reload(delegation_mod)


# ── Bug 2: Kory-originated delegation (reply CC lexi@) is detected ──────────────
def test_merge_list_message_fields_backfills_recipients():
    from app.integrations.outlook_email import merge_list_message_fields

    full = {"id": "m1"}  # GET_MESSAGE returned no recipients
    list_item = {
        "conversationId": "c1",
        "ccRecipients": [{"emailAddress": {"address": "lexi@iconicfounders.com"}}],
        "toRecipients": [{"emailAddress": {"address": "guest@example.com"}}],
    }
    merged = merge_list_message_fields(full, list_item)
    assert merged["conversationId"] == "c1"
    assert merged["ccRecipients"] == list_item["ccRecipients"]
    assert merged["toRecipients"] == list_item["toRecipients"]


def test_merge_does_not_clobber_present_recipients():
    from app.integrations.outlook_email import merge_list_message_fields

    full = {
        "id": "m1",
        "ccRecipients": [{"emailAddress": {"address": "real@x.com"}}],
    }
    merged = merge_list_message_fields(full, {"ccRecipients": [{"emailAddress": {"address": "wrong@x.com"}}]})
    assert merged["ccRecipients"][0]["emailAddress"]["address"] == "real@x.com"


def test_kory_sent_reply_cc_lexi_is_delegation():
    prev = _reload_with_lexi_mailbox("lexi@iconicfounders.com")
    try:
        from app.agents.delegation import detect_delegation

        raw_email = {
            "sender": "kory@iconicfounders.com",
            "cc_recipients": [{"emailAddress": {"address": "lexi@iconicfounders.com"}}],
            "to_recipients": [{"emailAddress": {"address": "guest@example.com"}}],
        }
        decision = detect_delegation(
            subject="Re: quick call next week?",
            body="Lexi, could you find us a time? Thanks!",
            sender="kory@iconicfounders.com",
            raw_email=raw_email,
        )
        assert decision.is_delegation
        assert decision.lexi_cc
    finally:
        _restore_lexi_mailbox(prev)


# ── Bug 3: vague availability + quoted history → no fabricated time/conflict ────
def test_strip_quoted_reply_cuts_history():
    from app.scheduling.inbound_availability import strip_quoted_reply

    body = (
        "Tuesday or Wednesday afternoon both work for me.\n\n"
        "On Fri, Jul 25, 2026 at 12:27 AM Anjana <a@x.com> wrote:\n"
        "> Hi Kory, would you have 30 minutes early next week?\n"
    )
    stripped = strip_quoted_reply(body)
    assert "wrote:" not in stripped.lower()
    assert "12:27" not in stripped
    assert stripped.startswith("Tuesday or Wednesday afternoon")


def test_vague_window_reply_has_no_bogus_candidate():
    from app.scheduling.inbound_availability import extract_inbound_time_candidates

    body = (
        "Tuesday or Wednesday afternoon both work for me.\n\n"
        "On Fri, Jul 25, 2026 at 12:27 AM Anjana <a@x.com> wrote:\n"
        "> Hi Kory, would you have 30 minutes early next week?\n"
    )
    candidates = extract_inbound_time_candidates(body)
    # No candidate should come from the quoted 12:27 AM header, and none on Friday.
    for c in candidates:
        hour = int(c["start"][11:13])
        assert 6 <= hour <= 21, f"implausible hour parsed: {c['start']}"
        assert "Fri" not in c["start"]  # naive: iso has no weekday, guards below
    # Weekday of each candidate must be Tue/Wed (the only days named in new text).
    from datetime import datetime

    for c in candidates:
        wd = datetime.fromisoformat(c["start"]).weekday()
        assert wd in (1, 2), f"unexpected weekday in {c['start']}"


def test_midnight_artifact_is_rejected():
    from app.scheduling.inbound_availability import extract_inbound_time_candidates

    # A bare "12:27 am" must not become a candidate (business-hours guard).
    candidates = extract_inbound_time_candidates("Let's meet Wednesday at 12:27 am")
    assert all(6 <= int(c["start"][11:13]) <= 21 for c in candidates)


# ── Bug 4: Kory-facing text never suggests handing off to anyone else ────────
def test_scrub_removes_third_party_handoff():
    from app.scheduling.kory_escalation import _scrub_third_party_mentions

    text = "The times didn't fit. Heidi has been flagged. Reply to retry."
    scrubbed = _scrub_third_party_mentions(text)
    assert "heidi" not in scrubbed.lower()
    assert "reply to retry" in scrubbed.lower()


# ── Bug 1: a sent offer is not left in the re-sendable pending state ────────────
def test_offer_sent_status_is_not_pending():
    from app.agents.comms_agent import PENDING_APPROVAL, STATUS_OFFER_SENT

    assert PENDING_APPROVAL == "pending_approval"
    assert STATUS_OFFER_SENT != PENDING_APPROVAL
