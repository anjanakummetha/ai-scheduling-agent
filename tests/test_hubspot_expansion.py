"""HubSpot expansion — staging only, writes blocked."""

from __future__ import annotations

from unittest.mock import patch

from app.integrations.hubspot_manager import (
    deals_snapshot_for_brief,
    enrich_prebrief_from_hubspot,
    execute_hubspot_batch,
    find_contacts_for_outreach,
    propose_duplicate_merges,
    propose_field_enrichment,
    propose_inactive_cleanup,
    stage_meeting_note,
)


@patch("app.integrations.hubspot_manager.hubspot_configured", return_value=True)
@patch("app.integrations.hubspot_manager.count_contacts")
@patch("app.integrations.hubspot_manager.search_contacts")
def test_cleanup_is_a_read_only_report_and_never_proposes_archiving(
    mock_search, mock_count, _cfg
):
    """Cleanup used to recommend archiving active contacts. It now only reports."""
    mock_count.return_value = 12
    mock_search.return_value = {
        "count": 1,
        "contacts": [
            {
                "id": "1",
                "email": "old@x.com",
                "name": "Old",
                "hs_lead_status": "Active / Signed",
                "lastmodifieddate": "2025-01-01T00:00:00+00:00",
            }
        ],
    }
    out = propose_inactive_cleanup(inactive_days=90, limit=10)
    assert out["ok"]
    assert "proposals" not in out, "the health report must not stage mutations"
    assert "archive" not in out["kory_message"].lower()
    assert out["total"] == 12


@patch("app.integrations.hubspot_manager.search_contacts")
def test_duplicate_merges_staged(mock_search, tmp_path, monkeypatch):
    monkeypatch.setenv("LEXI_DATABASE_PATH", str(tmp_path / "lexi.db"))
    import importlib
    import app.config
    import app.storage.lexi_db as lexi_db

    importlib.reload(app.config)
    importlib.reload(lexi_db)
    from scripts.init_lexi_db import init_lexi_db

    init_lexi_db(tmp_path / "lexi.db")
    mock_search.return_value = {
        "contacts": [
            {"id": "1", "email": "a@x.com", "name": "Ann"},
            {"id": "2", "email": "a@x.com", "name": "Ann Duplicate"},
        ]
    }
    out = propose_duplicate_merges(limit=10)
    assert out["pair_count"] == 1
    assert out["writes_blocked"] is True


@patch("app.integrations.hubspot_manager.hubspot_configured", return_value=True)
@patch("app.integrations.hubspot_manager._signature_fields_for")
@patch("app.integrations.hubspot_manager.search_contacts")
def test_enrichment_proposes_signature_fields_for_blank_only(
    mock_search, mock_sig, _cfg, tmp_path, monkeypatch
):
    monkeypatch.setenv("LEXI_DATABASE_PATH", str(tmp_path / "lexi.db"))
    import importlib
    import app.config
    import app.storage.lexi_db as lexi_db

    importlib.reload(app.config)
    importlib.reload(lexi_db)
    from scripts.init_lexi_db import init_lexi_db

    init_lexi_db(tmp_path / "lexi.db")
    mock_search.return_value = {
        "total": 2,
        "contacts": [
            {"id": "1", "email": "b@x.com", "name": "Bob", "jobtitle": "", "company": ""},
            # Already has a company — that value must never be proposed over.
            {"id": "2", "email": "c@x.com", "name": "Cid", "jobtitle": "", "company": "Real Co"},
        ],
    }
    mock_sig.return_value = {"jobtitle": "CEO", "company": "Signature Co"}
    out = propose_field_enrichment(limit=5)
    assert out["proposal_count"] == 2
    first, second = out["proposals"]
    assert first["proposed_fields"] == {"jobtitle": "CEO", "company": "Signature Co"}
    assert second["proposed_fields"] == {"jobtitle": "CEO"}, "must not overwrite a real company"


def test_signature_extraction_reads_the_senders_own_block():
    from app.integrations.hubspot_manager import _extract_signature_fields

    assert _extract_signature_fields(
        "Jane Doe\nManaging Partner at Acme Capital\n", contact_name="Jane Doe"
    ) == {"jobtitle": "Managing Partner", "company": "Acme Capital"}
    # No recognisable title -> nothing guessed.
    assert _extract_signature_fields("Jane Doe\nSent from my iPhone\n", contact_name="Jane Doe") == {}
    # HTML entities and tags must not leak into the value.
    assert _extract_signature_fields(
        "<div>Jane Doe<br>VP&nbsp;of&nbsp;Sales</div>", contact_name="Jane Doe"
    ) == {"jobtitle": "VP of Sales"}


def test_signature_extraction_never_returns_the_recipients_signature():
    """The real failure: a reply quoted Kory's own footer under the sender's text.

    Without the name-proximity rule this wrote "Kory Mitchell - CEO" into a
    different person's contact record.
    """
    from app.integrations.hubspot_manager import _extract_signature_fields

    reply = (
        "Sounds good, talk then.\n\n"
        "Christian Hutter\n"
        "Managing Director | Hutter Water\n\n"
        "From: Kory Mitchell <kory.mitchell@iconicfounders.com>\n"
        "Sent: Tuesday\n"
        "&nbsp; &nbsp; Kory Mitchell - CEO\n"
        "Iconic Founders Group\n"
    )
    assert _extract_signature_fields(reply, contact_name="Christian Hutter") == {
        "jobtitle": "Managing Director",
        "company": "Hutter Water",
    }
    # Only the other party's signature present -> refuse rather than mis-attribute.
    assert (
        _extract_signature_fields(
            "Thanks!\n\nKory Mitchell\nCEO\n", contact_name="Christian Hutter"
        )
        == {}
    )


@patch("app.integrations.hubspot_manager.contact_deals", return_value=[])
@patch("app.integrations.hubspot_manager.lifecycle_label_map", return_value={"1636423401": "Active"})
@patch("app.integrations.hubspot_manager.hubspot_configured", return_value=True)
@patch("app.integrations.hubspot_manager.search_contacts")
def test_prebrief_reports_real_fields_not_unknown(mock_search, _cfg, _lifecycle, _deals):
    """It used to say 'Lifecycle: unknown / Lead status: unknown' for everyone."""
    mock_search.return_value = {
        "contacts": [
            {
                "id": "1",
                "email": "c@co.com",
                "name": "Casey",
                "company": "Co",
                "jobtitle": "CFO",
                "hs_lead_status": "In Conversation",
                "lifecyclestage": "1636423401",
            }
        ]
    }
    out = enrich_prebrief_from_hubspot(email="c@co.com")
    assert out["found"] is True
    message = out["kory_message"]
    assert "CFO" in message and "Co" in message
    assert "Active" in message, "numeric lifecycle id must resolve to its label"
    assert "In Conversation" in message
    assert "unknown" not in message.lower()


@patch("app.integrations.hubspot_manager.contact_deals", return_value=[])
@patch("app.integrations.hubspot_manager.hubspot_configured", return_value=True)
@patch("app.integrations.hubspot_manager.search_contacts")
def test_person_lookup_asks_which_one_rather_than_guessing(mock_search, _cfg, _deals):
    """Two real Chris Gavoras exist. Guessing could show the wrong opt-out status."""
    mock_search.return_value = {
        "contacts": [
            {"id": "1", "email": "chris@threeshadows.co", "name": "Chris Gavora"},
            {"id": "2", "email": "cgavora@bockmanninc.com", "name": "Chris Gavora"},
        ]
    }
    out = enrich_prebrief_from_hubspot(name="Chris Gavora")
    assert out["ambiguous"] is True
    assert out["found"] is False
    assert len(out["candidates"]) == 2
    assert "which one" in out["kory_message"].lower()


@patch("app.integrations.hubspot_manager.contact_deals", return_value=[])
@patch("app.integrations.hubspot_manager.hubspot_configured", return_value=True)
@patch("app.integrations.hubspot_manager.search_contacts")
def test_person_lookup_ignores_loose_search_hits(mock_search, _cfg, _deals):
    """Searching "Mark" also returns Karen Brown via andrew.brown@markel.com."""
    mock_search.return_value = {
        "contacts": [
            {"id": "9", "email": "andrew.brown@markel.com", "name": "Karen Brown"},
            {"id": "1", "email": "mark@heartlandvc.com", "name": "Mark Accomando"},
        ]
    }
    out = enrich_prebrief_from_hubspot(name="Mark Accomando")
    assert out["found"] is True
    assert out["contact"]["name"] == "Mark Accomando"


@patch("app.integrations.hubspot_manager.contact_deals", return_value=[])
@patch("app.integrations.hubspot_manager.hubspot_configured", return_value=True)
@patch("app.integrations.hubspot_manager.search_contacts")
def test_prebrief_warns_on_do_not_contact(mock_search, _cfg, _deals):
    mock_search.return_value = {
        "contacts": [
            {"id": "1", "email": "c@co.com", "name": "Casey", "hs_lead_status": "Do Not Contact"}
        ]
    }
    out = enrich_prebrief_from_hubspot(email="c@co.com")
    assert out["do_not_contact"] is True
    assert "Do Not Contact" in out["kory_message"]


@patch("app.integrations.hubspot_manager.hubspot_writes_blocked", return_value=True)
@patch("app.integrations.hubspot_manager.search_contacts")
def test_meeting_note_staged_not_written(mock_search, _blocked, tmp_path, monkeypatch):
    monkeypatch.setenv("LEXI_DATABASE_PATH", str(tmp_path / "lexi.db"))
    import importlib
    import app.config
    import app.storage.lexi_db as lexi_db

    importlib.reload(app.config)
    importlib.reload(lexi_db)
    from scripts.init_lexi_db import init_lexi_db

    init_lexi_db(tmp_path / "lexi.db")
    mock_search.return_value = {"contacts": [{"id": "1", "email": "d@x.com", "name": "Dan"}]}
    out = stage_meeting_note(email="d@x.com", note="Great intro call", approved=True)
    assert out["dry_run"] is True
    assert out["writes_blocked"] is True


@patch("app.integrations.hubspot_manager.owner_name", return_value="Natalie Asher")
@patch("app.integrations.hubspot_manager.search_contacts")
def test_meeting_note_on_someone_elses_contact_asks_first(mock_search, _owner):
    """Mirrors the Asana guard: changing a colleague's record must be deliberate."""
    mock_search.return_value = {
        "contacts": [
            {"id": "1", "email": "d@x.com", "name": "Dan", "hubspot_owner_id": "165157278"}
        ]
    }
    out = stage_meeting_note(email="d@x.com", note="Call notes", approved=True)
    assert out["ok"] is False
    assert out["error_code"] == "owner_confirmation_required"
    assert "Natalie Asher" in out["error"]


@patch("app.integrations.hubspot_manager.search_contacts")
def test_unassigned_contact_needs_no_owner_confirmation(mock_search, tmp_path, monkeypatch):
    monkeypatch.setenv("LEXI_DATABASE_PATH", str(tmp_path / "lexi.db"))
    import importlib
    import app.config
    import app.storage.lexi_db as lexi_db

    importlib.reload(app.config)
    importlib.reload(lexi_db)
    from scripts.init_lexi_db import init_lexi_db

    init_lexi_db(tmp_path / "lexi.db")
    mock_search.return_value = {
        "contacts": [{"id": "1", "email": "d@x.com", "name": "Dan", "hubspot_owner_id": ""}]
    }
    out = stage_meeting_note(email="d@x.com", note="Call notes", approved=True)
    assert out.get("error_code") != "owner_confirmation_required"


@patch("app.integrations.hubspot_manager.search_contacts")
def test_outreach_candidates(mock_search):
    mock_search.return_value = {
        "contacts": [
            {"id": "1", "email": "e@x.com", "name": "Eve", "lifecyclestage": "lead"},
            {"id": "2", "email": "f@x.com", "name": "Frank", "lifecyclestage": "customer"},
        ]
    }
    out = find_contacts_for_outreach(lifecycle="lead", limit=10)
    assert out["count"] >= 1


@patch(
    "app.integrations.hubspot_manager.deal_stage_map",
    return_value={
        "open1": {"pipeline": "Iconic Pipeline", "stage": "Prospect", "is_closed": False},
        "won1": {"pipeline": "Closed/Won", "stage": "Sell-Side Advisory", "is_closed": True},
    },
)
@patch("app.integrations.hubspot_manager.hubspot_configured", return_value=True)
@patch("app.integrations.hubspot_manager.execute_hubspot_tool")
def test_deals_snapshot_counts_only_genuinely_open_deals(mock_tool, _cfg, _stages):
    """A past close date on a Closed/Won deal is correct, not overdue."""
    mock_tool.return_value = {
        "data": {
            "results": [
                {
                    "id": "d1",
                    "properties": {
                        "dealname": "Series A",
                        "dealstage": "open1",
                        "amount": "1000000",
                        "closedate": "2025-01-01T00:00:00Z",
                    },
                },
                {
                    "id": "d2",
                    "properties": {
                        "dealname": "Orion (won)",
                        "dealstage": "won1",
                        "amount": "1900000",
                        "closedate": "2025-01-01T00:00:00Z",
                    },
                },
            ]
        }
    }
    out = deals_snapshot_for_brief(limit=5)
    assert out["open_count"] == 1
    assert out["total_count"] == 2
    assert "Iconic Pipeline / Prospect" in out["kory_message"]
    overdue_names = [d.get("dealname") for d in out["overdue"]]
    assert overdue_names == ["Series A"], "closed deals must not count as overdue"


@patch("app.integrations.hubspot_manager.hubspot_configured", return_value=True)
@patch("app.integrations.hubspot_manager.execute_hubspot_tool")
def test_deals_snapshot_hides_placeholder_amounts(mock_tool, _cfg):
    """Many deals carry $1 placeholders; showing them implies real value."""
    from app.integrations.hubspot_manager import _format_amount

    assert _format_amount("1") == ""
    assert _format_amount(None) == ""
    assert "$500,000" in _format_amount("500000")


@patch("app.integrations.hubspot_manager.hubspot_writes_blocked", return_value=True)
def test_execute_batch_blocked(mock_blocked, tmp_path, monkeypatch):
    monkeypatch.setenv("LEXI_DATABASE_PATH", str(tmp_path / "lexi.db"))
    import importlib
    import app.config
    import app.storage.lexi_db as lexi_db

    importlib.reload(app.config)
    importlib.reload(lexi_db)
    from scripts.init_lexi_db import init_lexi_db
    from app.integrations.hubspot_manager import _stage_hubspot_batch

    init_lexi_db(tmp_path / "lexi.db")
    batch_id = _stage_hubspot_batch(batch_type="cleanup", payload={"proposals": []})
    out = execute_hubspot_batch(batch_id=batch_id, approved=True)
    assert out["dry_run"] is True
    assert out["writes_blocked"] is True
