"""HubSpot read layer — field projection, opt-out safety, pipeline awareness.

Every test here fails against the pre-fix code. The bugs they cover all produced
confident wrong answers rather than errors: blank fields read as "missing",
numeric stage ids read as stage names, and an outreach filter that matched
nothing returned arbitrary contacts instead of none.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.integrations import hubspot_manager as hm


def _contact(**overrides):
    base = {
        "id": "1",
        "properties": {
            "email": "person@example.com",
            "firstname": "Test",
            "lastname": "Person",
            "hubspot_owner_id": "159133511",
            "hs_lead_status": "In Conversation",
            "lifecyclestage": "1636423401",
            "company": "Acme",
            "jobtitle": "CEO",
        },
    }
    base["properties"].update(overrides)
    return base


@pytest.fixture(autouse=True)
def _clear_caches():
    hm._pipeline_cache = None
    hm._lifecycle_cache = None
    hm._owner_cache = None
    yield
    hm._pipeline_cache = None
    hm._lifecycle_cache = None
    hm._owner_cache = None


# --- field projection ------------------------------------------------------


def test_contact_reads_request_the_fields_they_depend_on():
    """The original bug: HubSpot returns 6 default properties unless asked."""
    with patch.object(hm, "hubspot_configured", return_value=True):
        with patch.object(hm, "execute_hubspot_tool") as ex:
            ex.return_value = {"data": {"results": [_contact()]}}
            hm.search_contacts(limit=5)
    args = ex.call_args.args[1]
    assert "properties" in args, "properties must be requested explicitly"
    for field in ("lifecyclestage", "hs_lead_status", "company", "jobtitle", "hubspot_owner_id"):
        assert field in args["properties"], f"{field} must be requested"


def test_search_does_not_silently_fall_back_to_an_unfiltered_list():
    """A failed lookup used to return arbitrary contacts as if they matched."""
    with patch.object(hm, "hubspot_configured", return_value=True):
        with patch.object(hm, "execute_hubspot_tool", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError):
                hm.search_contacts(limit=5, query="someone@example.com")


def test_search_reports_true_scope_not_just_what_it_read():
    with patch.object(hm, "hubspot_configured", return_value=True):
        with patch.object(hm, "execute_hubspot_tool") as ex:
            ex.return_value = {"data": {"results": [_contact()], "total": 2153}}
            out = hm.search_contacts(limit=1)
    assert out["count"] == 1
    assert out["total"] == 2153
    assert out["truncated"] is True


# --- opt-out safety --------------------------------------------------------


def test_do_not_contact_is_never_an_outreach_candidate():
    rows = [
        _contact(email="ok@example.com", hs_lead_status="In Conversation"),
        _contact(email="optout@example.com", hs_lead_status="Do Not Contact"),
    ]
    with patch.object(hm, "hubspot_configured", return_value=True):
        with patch.object(hm, "execute_hubspot_tool") as ex:
            ex.return_value = {"data": {"results": rows}}
            out = hm.find_contacts_for_outreach(limit=10)
    emails = [c["email"] for c in out["contacts"]]
    assert "optout@example.com" not in emails
    assert out["excluded_do_not_contact"] == 1


def test_empty_outreach_filter_returns_nothing_not_arbitrary_contacts():
    """The dangerous fallback: no match used to mean `contacts[:limit]`."""
    rows = [_contact(email=f"p{i}@example.com") for i in range(5)]
    with patch.object(hm, "hubspot_configured", return_value=True):
        with patch.object(hm, "execute_hubspot_tool") as ex:
            ex.return_value = {"data": {"results": rows}}
            out = hm.find_contacts_for_outreach(limit=10, lifecycle="nonexistent-stage")
    assert out["count"] == 0
    assert out["contacts"] == []


def test_outreach_drafts_reject_opted_out_ids_passed_directly():
    rows = [_contact(email="optout@example.com", hs_lead_status="Do Not Contact")]
    with patch.object(hm, "hubspot_configured", return_value=True):
        with patch.object(hm, "execute_hubspot_tool") as ex:
            ex.return_value = {"data": {"results": rows}}
            out = hm.propose_outreach_batch(contact_ids=["1"], goal="test")
    assert out["draft_count"] == 0
    assert out["excluded_do_not_contact"] == 1


def test_outreach_defaults_to_korys_own_contacts():
    with patch.object(hm, "hubspot_configured", return_value=True):
        with patch.object(hm, "execute_hubspot_tool") as ex:
            ex.return_value = {"data": {"results": []}}
            hm.find_contacts_for_outreach(limit=5)
    groups = ex.call_args.args[1].get("filterGroups") or []
    filters = groups[0]["filters"] if groups else []
    assert any(
        f.get("propertyName") == "hubspot_owner_id" and f.get("value") == hm.kory_owner_id()
        for f in filters
    ), "outreach must be scoped to Kory's contacts by default"


# --- pipeline awareness ----------------------------------------------------


def test_closed_flag_survives_the_string_false_trap():
    """HubSpot sends booleans as strings; "false" is truthy in Python."""
    payload = {
        "data": {
            "results": [
                {
                    "label": "Iconic Pipeline",
                    "stages": [
                        {"id": "1", "label": "Prospect", "metadata": {"isClosed": "false"}},
                        {"id": "2", "label": "Won", "metadata": {"isClosed": "true"}},
                    ],
                }
            ]
        }
    }
    with patch.object(hm, "execute_hubspot_tool", return_value=payload):
        stages = hm.deal_stage_map(refresh=True)
    assert stages["1"]["is_closed"] is False
    assert stages["2"]["is_closed"] is True


def test_closed_pipelines_are_treated_as_closed():
    payload = {
        "data": {
            "results": [
                {
                    "label": "Closed/Won",
                    "stages": [{"id": "9", "label": "Sell-Side", "metadata": {}}],
                }
            ]
        }
    }
    with patch.object(hm, "execute_hubspot_tool", return_value=payload):
        stages = hm.deal_stage_map(refresh=True)
    assert stages["9"]["is_closed"] is True


def test_lifecycle_numeric_ids_resolve_to_labels():
    payload = {"data": {"options": [{"value": "1636423401", "label": "Active"}]}}
    with patch.object(hm, "execute_hubspot_tool", return_value=payload):
        hm.lifecycle_label_map(refresh=True)
        assert hm.lifecycle_label({"lifecyclestage": "1636423401"}) == "Active"


def test_placeholder_values_are_not_treated_as_real_data():
    assert hm.is_placeholder("N/A")
    assert hm.is_placeholder("-")
    assert not hm.is_placeholder("Bertram Capital")
    assert not hm.is_placeholder("")


def test_short_job_titles_are_not_placeholders():
    """VP and PM are real titles; flagging them would 'fix' valid data."""
    assert not hm.is_placeholder("VP", field="jobtitle")
    assert not hm.is_placeholder("PM", field="jobtitle")
    assert hm.is_placeholder("WM", field="company")


# --- failures must never read as "nothing found" ---------------------------


def test_stage_history_requests_stay_under_hubspots_50_object_cap():
    """HubSpot rejects history requests over 50 objects; we used to ask for 100."""
    with patch.object(hm, "execute_hubspot_tool") as ex:
        ex.return_value = {"data": {"results": []}}
        hm.deal_stage_movements(days=7, scan=200)
    assert ex.call_args.args[1]["limit"] <= 50


def test_failed_history_lookup_is_reported_not_silently_empty():
    with patch.object(hm, "execute_hubspot_tool", side_effect=RuntimeError("50 object cap")):
        out = hm.deal_stage_movements(days=7)
    assert out["ok"] is False
    assert "50 object cap" in out["error"]


def test_recent_changes_says_so_when_movement_lookup_fails():
    fail = {"ok": False, "moves": [], "scanned": 0, "error": "RuntimeError: nope"}
    with patch.object(hm, "hubspot_configured", return_value=True):
        with patch.object(hm, "count_contacts", return_value=3):
            with patch.object(hm, "deal_stage_movements", return_value=fail):
                out = hm.recent_changes(days=7)
    assert out["movements_ok"] is False
    assert "unavailable" in out["kory_message"]
    assert "No deal stage changes" not in out["kory_message"]
