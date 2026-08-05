"""Regression tests for the Phase 5 defect fixes."""

from __future__ import annotations

from unittest.mock import patch

from app.integrations import hubspot_manager as h
from app.scheduling import outreach_campaign as oc


# --- HubSpot contact-list slug (was the non-existent HUBSPOT_GET_ALL_CONTACTS) ---

def test_hubspot_list_contacts_slug_is_correct():
    assert h.HUBSPOT_LIST_CONTACTS == "HUBSPOT_LIST_CONTACTS"


def test_hubspot_status_reads_via_correct_slug():
    calls = {}

    def fake_search(*, limit=25, query=""):
        calls["limit"] = limit
        return {"contacts": [{"id": "1", "email": "a@b.com"}]}

    # Patch hubspot_configured too — keyless CI has no connection id, so without
    # this the brief short-circuits to ok=False before reaching search_contacts.
    with patch.object(h, "hubspot_configured", return_value=True):
        with patch.object(h, "search_contacts", side_effect=fake_search):
            with patch.object(h, "hubspot_writes_blocked", return_value=True):
                out = h.hubspot_status_brief()
    assert out["ok"] is True


# --- Outreach send path (was a "not enabled in this build" stub) ---
# The campaign feature is parked in production; these tests opt it back on so the
# send path stays covered for whoever picks it up. tests/test_outreach_campaign.py
# covers the parked state.

def test_outreach_send_blocked_returns_dry_run(monkeypatch):
    monkeypatch.setenv("LEXI_OUTREACH_CAMPAIGNS_ENABLED", "true")
    with patch.object(oc, "get_campaign", return_value={"campaign_id": "c1"}):
        with patch.object(oc, "outreach_sends_blocked", return_value=True):
            r = oc.send_outreach_campaign(campaign_id="c1", approved=True)
    assert r["sent"] == 0
    assert r.get("sends_blocked") or r.get("dry_run")


def test_outreach_send_dispatches_staged_drafts_when_unblocked(monkeypatch):
    # Stage two drafts in a real campaign, then send with the block lifted + a mocked mailer.
    monkeypatch.setenv("LEXI_HUBSPOT_LIVE_WRITES_ENABLED", "false")
    monkeypatch.setenv("LEXI_OUTREACH_CAMPAIGNS_ENABLED", "true")
    camp = oc.create_outreach_campaign(
        name="unit send test",
        goal="general",
        pasted_list="Alice, a@example.com\nBob, b@example.com",  # one contact per line
    )
    cid = camp["campaign_id"]
    oc.approve_outreach_campaign(campaign_id=cid)

    sent_to: list[str] = []

    def fake_send(*, to_email, subject, body, approved_send=False, send_channel="kory", cc_emails=None):
        sent_to.append(to_email)
        return (f"msg-{to_email}", "log-1")

    with patch.object(oc, "outreach_sends_blocked", return_value=False):
        with patch("app.integrations.outlook_email.send_outbound_email", side_effect=fake_send):
            r = oc.send_outreach_campaign(campaign_id=cid, approved=True)

    assert r["sent"] == 2, r
    assert set(sent_to) == {"a@example.com", "b@example.com"}
    assert r["remaining_staged"] == 0
