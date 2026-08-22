"""HubSpot staging — no live writes in tests."""

from __future__ import annotations

from unittest.mock import patch

from app.integrations.hubspot_manager import (
    hubspot_status_brief,
    propose_inactive_cleanup,
)


def test_hubspot_not_configured():
    with patch("app.integrations.hubspot_manager.hubspot_configured", return_value=False):
        brief = hubspot_status_brief()
    assert "not connected" in brief["kory_message"].lower()


@patch("app.integrations.hubspot_manager.hubspot_configured", return_value=True)
@patch("app.integrations.hubspot_manager.count_contacts", return_value=7)
@patch("app.integrations.hubspot_manager.search_contacts")
def test_cleanup_reports_without_staging_any_mutation(mock_search, _count, _cfg):
    """Cleanup is a view now — it stages nothing, so there is no batch to apply."""
    mock_search.return_value = {
        "count": 2,
        "contacts": [
            {"id": "1", "email": "a@x.com", "name": "A", "hs_lead_status": "Active / Signed"},
            {"id": "2", "email": "b@x.com", "name": "B", "hs_lead_status": "In Conversation"},
        ],
    }
    result = propose_inactive_cleanup(limit=10)
    assert result["ok"] is True
    assert result.get("batch_id") is None
    assert result["total"] == 7
