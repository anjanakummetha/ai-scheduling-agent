"""Regression tests for the Phase 5 defect fixes."""

from __future__ import annotations

from unittest.mock import patch

from app.integrations import hubspot_manager as h


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
