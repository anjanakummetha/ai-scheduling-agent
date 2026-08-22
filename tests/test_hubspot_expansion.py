"""HubSpot expansion — staging only, writes blocked."""

from __future__ import annotations

from unittest.mock import patch

import app.integrations.hubspot_manager as hs
from app.integrations.hubspot_manager import (
    deals_snapshot_for_brief,
    enrich_prebrief_from_hubspot,
    execute_hubspot_batch,
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
    # (fields, provenance) — every value records the message it came from.
    mock_sig.return_value = (
        {"jobtitle": "CEO", "company": "Signature Co"},
        {"source": "outlook_signature", "message_id": "msg-1"},
    )
    out = propose_field_enrichment(limit=5)
    assert out["proposal_count"] == 2
    first, second = out["proposals"]
    assert first["proposed_fields"] == {"jobtitle": "CEO", "company": "Signature Co"}
    assert second["proposed_fields"] == {"jobtitle": "CEO"}, "must not overwrite a real company"
    # Evidence is per field: one contact can be filled from HubSpot's own company
    # record and from a signature in the same proposal.
    assert first["evidence"]["jobtitle"]["source"] == "outlook_signature"
    assert first["evidence"]["jobtitle"]["message_id"] == "msg-1"


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


@patch("app.integrations.hubspot_manager.owner_is_known", return_value=True)
@patch("app.integrations.hubspot_manager.owner_name", return_value="Natalie Asher")
@patch("app.integrations.hubspot_manager.search_contacts")
def test_meeting_note_on_someone_elses_contact_asks_first(mock_search, _owner, _known):
    """Mirrors the Asana guard: changing a colleague's record must be deliberate.

    `owner_is_known` is stubbed alongside `owner_name`: an id HubSpot cannot
    resolve gets a different message, covered in test_hubspot_merge_ownership.
    """
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


# --- write-path review fixes (HubSpot write-test plan, step 1) --------------


@patch("app.integrations.hubspot_manager.search_contacts")
def test_meeting_note_never_files_onto_a_fuzzy_match(mock_search):
    """HubSpot free-text search is loose: a query for one person returns others.

    The old code took contacts[0] when no address matched, which filed a
    meeting's notes onto a stranger's record — silently, and unrecoverably.
    """
    mock_search.return_value = {
        "contacts": [
            {"id": "WRONG", "email": "someone.else@corp.com", "name": "Someone Else"}
        ]
    }
    out = stage_meeting_note(email="anjana@gmail.com", note="Call notes", approved=True)
    assert out["ok"] is False
    assert out["error_code"] == "contact_not_found"
    assert out["near_matches"][0]["email"] == "someone.else@corp.com"
    assert "someone.else@corp.com" in out["kory_message"]
    assert "won't guess" in out["kory_message"]


@patch("app.integrations.hubspot_manager.search_contacts")
def test_meeting_note_reports_a_failed_lookup_as_a_failure(mock_search):
    """A failed search must never read as 'that person isn't in HubSpot'."""
    mock_search.side_effect = RuntimeError("composio exploded")
    out = stage_meeting_note(email="d@x.com", note="Call notes", approved=True)
    assert out["ok"] is False
    assert out["error_code"] == "lookup_failed"
    assert "RuntimeError" in out["error"]


@patch("app.integrations.hubspot_manager.execute_hubspot_tool")
@patch("app.integrations.hubspot_manager.contacts_by_ids")
@patch("app.integrations.hubspot_manager.hubspot_writes_blocked", return_value=False)
def test_enrichment_applied_count_excludes_skipped_rows(
    _blocked, mock_by_ids, mock_tool, tmp_path, monkeypatch
):
    """'applied: 1' while writing nothing is the silent-wrong-answer class."""
    monkeypatch.setenv("LEXI_DATABASE_PATH", str(tmp_path / "lexi.db"))
    import importlib
    import app.config
    import app.storage.lexi_db as lexi_db

    importlib.reload(app.config)
    importlib.reload(lexi_db)
    from scripts.init_lexi_db import init_lexi_db
    from app.integrations.hubspot_manager import _stage_hubspot_batch

    init_lexi_db(tmp_path / "lexi.db")
    # Kory filled the title in by hand after the batch was staged: his value wins.
    mock_by_ids.return_value = [{"id": "1", "jobtitle": "Chief Financial Officer"}]
    batch_id = _stage_hubspot_batch(
        batch_type="field_enrichment",
        payload={"proposals": [{"contact_id": "1", "proposed_fields": {"jobtitle": "VP Finance"}}]},
    )
    out = execute_hubspot_batch(batch_id=batch_id, approved=True)
    assert out["applied"] == 0
    assert out["skipped"] == 1
    mock_tool.assert_not_called()



def _kory_owned(*contact_ids):
    """Stub live contact reads with records Kory owns.

    The merge guard re-reads both contacts to check ownership and fails closed
    when it cannot. Tests about merge *mechanics* have to say whose records
    these are, or they are really testing the ownership guard by accident.
    """
    from app.integrations.hubspot_manager import kory_owner_id

    def _read(ids):
        return [
            {"id": str(i), "name": f"Contact {i}", "hubspot_owner_id": kory_owner_id()}
            for i in ids
        ]

    return _read


@patch("app.integrations.hubspot_manager.contacts_by_ids", side_effect=_kory_owned())
@patch("app.integrations.hubspot_manager.execute_hubspot_tool")
@patch("app.integrations.hubspot_manager.hubspot_writes_blocked", return_value=False)
def test_merges_are_one_at_a_time_and_must_name_the_pair(
    _blocked, mock_tool, _contacts, tmp_path, monkeypatch
):
    """HubSpot merges are permanent; one approval must not apply a whole batch."""
    monkeypatch.setenv("LEXI_DATABASE_PATH", str(tmp_path / "lexi.db"))
    import importlib
    import app.config
    import app.storage.lexi_db as lexi_db

    importlib.reload(app.config)
    importlib.reload(lexi_db)
    from scripts.init_lexi_db import init_lexi_db
    from app.integrations.hubspot_manager import _stage_hubspot_batch

    init_lexi_db(tmp_path / "lexi.db")
    batch_id = _stage_hubspot_batch(
        batch_type="duplicate_merge",
        payload={
            "pairs": [
                {"primary_id": "1", "duplicate_id": "2"},
                {"primary_id": "3", "duplicate_id": "4"},
            ]
        },
    )
    # Bare approval refuses and writes nothing.
    out = execute_hubspot_batch(batch_id=batch_id, approved=True)
    assert out["ok"] is False
    assert out["applied"] == 0
    mock_tool.assert_not_called()

    # Naming one pair merges exactly that pair. The mock must answer with the
    # real client's shape — writers now check `successful`, so a bare MagicMock
    # would read as "HubSpot refused".
    mock_tool.return_value = {"data": {}, "successful": True, "log_id": "log-1"}
    out = execute_hubspot_batch(batch_id=batch_id, approved=True, merge_pair="3:4")
    assert out["ok"] is True
    assert out["applied"] == 1
    assert mock_tool.call_count == 1
    tool, args = mock_tool.call_args[0]
    assert tool == "HUBSPOT_MERGE_CONTACTS"
    assert args == {"primaryObjectId": "3", "objectIdToMerge": "4"}


@patch("app.integrations.hubspot_manager.contacts_by_ids", side_effect=_kory_owned())
@patch("app.integrations.hubspot_manager.execute_hubspot_tool")
@patch("app.integrations.hubspot_manager.hubspot_writes_blocked", return_value=False)
def test_a_refused_merge_is_reported_not_counted_as_applied(
    _blocked, mock_tool, _contacts, tmp_path, monkeypatch
):
    """HubSpot answering successful=false must not read as a completed merge.

    Every writer here used to return a hardcoded ok:True and ignore the
    response, so a refusal surfaced to Kory as "done".
    """
    monkeypatch.setenv("LEXI_DATABASE_PATH", str(tmp_path / "lexi.db"))
    import importlib
    import app.config
    import app.storage.lexi_db as lexi_db

    importlib.reload(app.config)
    importlib.reload(lexi_db)
    from scripts.init_lexi_db import init_lexi_db
    from app.integrations.hubspot_manager import _stage_hubspot_batch

    init_lexi_db(tmp_path / "lexi.db")
    batch_id = _stage_hubspot_batch(
        batch_type="duplicate_merge",
        payload={"pairs": [{"primary_id": "5", "duplicate_id": "6"}]},
    )
    mock_tool.return_value = {
        "data": {"error": "cannot merge across owners"},
        "successful": False,
        "log_id": "log-2",
    }
    out = execute_hubspot_batch(batch_id=batch_id, approved=True, merge_pair="5:6")
    assert out["ok"] is False
    assert out["applied"] == 0
    assert out["errors"]

    # An unknown pair is refused rather than guessed at.
    out = execute_hubspot_batch(batch_id=batch_id, approved=True, merge_pair="9:9")
    assert out["ok"] is False
    assert mock_tool.call_count == 1


def test_note_payload_matches_the_composio_schema():
    """Pin the exact field names HUBSPOT_CREATE_NOTE accepts.

    The note path shipped sending {"contactId": ..., "body": ...}. Neither key
    exists in the tool's schema and the required hs_timestamp was missing, so
    every meeting note was rejected or filed empty and unattached. A dry-run
    harness cannot catch this — it records what we meant to send, not what the
    tool accepts — so the field names are asserted here instead.
    """
    from app.integrations.hubspot_manager import note_payload

    args = note_payload(contact_id="12345", body="Talked about the Q3 close.")

    # Schema of HUBSPOT_CREATE_NOTE: required hs_timestamp; body is hs_note_body;
    # the contact link is an association. There is no contactId parameter.
    assert set(args) <= {
        "hs_note_body", "hs_timestamp", "hubspot_owner_id",
        "associations", "custom_properties", "hs_attachment_ids",
    }, f"sending fields the tool does not accept: {set(args)}"
    assert "hs_timestamp" in args, "hs_timestamp is required by the tool"
    assert args["hs_note_body"] == "Talked about the Q3 close."
    assert "contactId" not in args and "body" not in args

    assoc = args["associations"][0]
    assert assoc["to"]["id"] == "12345"
    assert assoc["types"][0]["associationTypeId"] == 202  # note -> contact
    assert assoc["types"][0]["associationCategory"] == "HUBSPOT_DEFINED"


@patch("app.integrations.hubspot_manager.execute_hubspot_tool")
@patch("app.integrations.hubspot_manager.hubspot_writes_blocked", return_value=False)
@patch("app.integrations.hubspot_manager.search_contacts")
def test_meeting_note_sends_the_schema_shaped_payload(mock_search, _blocked, mock_tool, tmp_path, monkeypatch):
    """The live note path must send what the tool accepts, not what we assumed."""
    monkeypatch.setenv("LEXI_DATABASE_PATH", str(tmp_path / "lexi.db"))
    from scripts.init_lexi_db import init_lexi_db

    init_lexi_db(tmp_path / "lexi.db")
    mock_tool.return_value = {"data": {"id": "note-1"}}
    mock_search.return_value = {
        "contacts": [
            {"id": "777", "email": "real@person.com", "name": "Real Person",
             "hubspot_owner_id": "159133511"}
        ]
    }

    out = stage_meeting_note(email="real@person.com", note="Met about the deal.", approved=True)
    assert out["ok"] is True

    tool, args = mock_tool.call_args[0]
    assert tool == "HUBSPOT_CREATE_NOTE"
    assert args["hs_note_body"] == "Met about the deal."
    assert args["hs_timestamp"]
    assert args["associations"][0]["to"]["id"] == "777"
    assert "contactId" not in args


@patch("app.integrations.hubspot_manager.execute_hubspot_tool")
@patch("app.integrations.hubspot_manager.hubspot_writes_blocked", return_value=False)
@patch("app.integrations.hubspot_manager.search_contacts")
def test_meeting_note_looks_up_by_exact_address_first(mock_search, _blocked, mock_tool, tmp_path, monkeypatch):
    """Resolve by EQ filter on email, not free-text query.

    Free-text search is loose AND sits behind a slower index, so a contact that
    verifiably exists reported contact_not_found. The address is what decides the
    record, so it is what gets asked.
    """
    monkeypatch.setenv("LEXI_DATABASE_PATH", str(tmp_path / "lexi.db"))
    from scripts.init_lexi_db import init_lexi_db

    init_lexi_db(tmp_path / "lexi.db")
    mock_tool.return_value = {"data": {"id": "note-1"}}
    mock_search.return_value = {
        "contacts": [
            {"id": "42", "email": "exact@person.com", "name": "Exact Person",
             "hubspot_owner_id": "159133511"}
        ]
    }

    out = stage_meeting_note(email="exact@person.com", note="Note.", approved=True)
    assert out["ok"] is True

    first_call = mock_search.call_args_list[0]
    assert first_call.kwargs.get("filters") == [
        {"propertyName": "email", "operator": "EQ", "value": "exact@person.com"}
    ], "the first lookup must be an exact address filter"
    assert "query" not in first_call.kwargs
    # Matched on the address, so no need to fall back to the loose search.
    assert mock_search.call_count == 1


@patch("app.integrations.hubspot_manager.execute_hubspot_tool")
@patch("app.integrations.hubspot_manager.hubspot_writes_blocked", return_value=False)
@patch("app.integrations.hubspot_manager.search_contacts")
def test_meeting_note_falls_back_to_loose_search_only_for_suggestions(
    mock_search, _blocked, mock_tool, tmp_path, monkeypatch
):
    """No exact match: run the loose query purely to offer near matches, write nothing."""
    monkeypatch.setenv("LEXI_DATABASE_PATH", str(tmp_path / "lexi.db"))
    from scripts.init_lexi_db import init_lexi_db

    init_lexi_db(tmp_path / "lexi.db")
    mock_search.side_effect = [
        {"contacts": []},  # exact filter: nobody has this address
        {"contacts": [{"id": "9", "email": "someone.else@co.com", "name": "Someone Else"}]},
    ]

    out = stage_meeting_note(email="nobody@nowhere.com", note="Note.", approved=True)
    assert out["ok"] is False
    assert out["error_code"] == "contact_not_found"
    assert mock_search.call_count == 2
    assert mock_search.call_args_list[1].kwargs.get("query") == "nobody@nowhere.com"
    # The loose hit is offered as a suggestion, never written to.
    assert out["near_matches"][0]["email"] == "someone.else@co.com"
    mock_tool.assert_not_called()


def _gate_that_refuses(**_kwargs):
    raise PermissionError("Kory approval required in Teams before Lexi makes changes.")


@patch("app.integrations.hubspot_manager.execute_hubspot_tool")
@patch("app.integrations.hubspot_manager.search_contacts")
def test_meeting_note_looks_the_contact_up_before_asking_kory_to_confirm(
    mock_search, mock_tool
):
    """A guard the model never reaches is not a guard.

    The approval gate used to be the first line of stage_meeting_note, so an
    unconfirmed call returned confirmation_required in 0.01s and the lookup, the
    near-match list and the ownership check never ran. Kory asked for the note,
    was asked to confirm, confirmed — and only then heard that nobody in HubSpot
    has that address. Finding the contact is a read; it needs no approval.
    """
    mock_search.side_effect = [{"contacts": []}, {"contacts": []}]

    with patch.object(hs, "assert_kory_approved_write", side_effect=_gate_that_refuses) as gate:
        out = stage_meeting_note(email="nobody@nowhere.com", note="Note.", approved=False)

    assert out["error_code"] == "contact_not_found"
    gate.assert_not_called()
    mock_tool.assert_not_called()


@patch("app.integrations.hubspot_manager.execute_hubspot_tool")
@patch("app.integrations.hubspot_manager.search_contacts")
def test_meeting_note_reports_a_colleagues_record_before_asking_kory_to_confirm(
    mock_search, mock_tool
):
    """Confirming and *then* being told it is Natalie's is the wrong order."""
    mock_search.return_value = {
        "contacts": [
            {"id": "7", "email": "her@lead.com", "name": "Her Lead",
             "hubspot_owner_id": "not-kory"}
        ]
    }

    with patch.object(hs, "assert_kory_approved_write", side_effect=_gate_that_refuses) as gate:
        with patch.object(hs, "kory_owner_id", return_value="kory-id"):
            with patch.object(hs, "owner_name", return_value="Natalie Asher"):
                with patch.object(hs, "owner_is_known", return_value=True):
                    out = stage_meeting_note(
                        email="her@lead.com", note="Note.", approved=False
                    )

    assert out["error_code"] == "owner_confirmation_required"
    assert "Natalie Asher" in out["error"]
    gate.assert_not_called()
    mock_tool.assert_not_called()


@patch("app.integrations.hubspot_manager.execute_hubspot_tool")
@patch("app.integrations.hubspot_manager.search_contacts")
def test_meeting_note_still_needs_approval_once_the_contact_checks_out(
    mock_search, mock_tool
):
    """Moving the gate must not remove it — his own contact still gets asked."""
    mock_search.return_value = {
        "contacts": [
            {"id": "7", "email": "his@lead.com", "name": "His Lead",
             "hubspot_owner_id": "kory-id"}
        ]
    }

    with patch.object(hs, "assert_kory_approved_write", side_effect=_gate_that_refuses) as gate:
        with patch.object(hs, "kory_owner_id", return_value="kory-id"):
            try:
                stage_meeting_note(email="his@lead.com", note="Note.", approved=False)
            except PermissionError:
                # The confirmation names the record, so Kory knows what he is
                # agreeing to rather than just "a HubSpot meeting note".
                assert "His Lead" in str(gate.call_args.kwargs["action"])
            else:
                raise AssertionError("an unapproved write must not reach HubSpot")

    gate.assert_called_once()
    mock_tool.assert_not_called()


def test_html_signature_path_still_carries_the_hubspot_bcc():
    """The production send path must BCC HubSpot, not just the plain-text one.

    _send_lexi_html_via_draft returns from send_outbound_email BEFORE the BCC
    block, so while the HTML signature was enabled — which it is in production —
    nothing Lexi sent carried the HubSpot logging BCC at all. This asserts on the
    draft payload actually handed to Composio, which is what regressed.
    """
    from app.integrations import outlook_email as oe

    sent: dict[str, object] = {}

    def fake_execute(slug, args, role=None):
        if slug == "OUTLOOK_CREATE_DRAFT":
            sent.update(args)
            return {"data": {"id": "draft-1"}}
        return {"data": {"id": "draft-1", "status_code": 202}}

    with patch.object(oe, "execute_tool", side_effect=fake_execute):
        with patch.object(oe, "hubspot_bcc_addresses", return_value=["12345@bcc.hubspot.com"]):
            with patch.object(oe, "_extract_draft_message_id", return_value="draft-1"):
                oe._send_lexi_html_via_draft(
                    recipient="someone@outside-company.com",
                    subject="s",
                    html_body="<p>b</p>",
                    inline_attachment={
                        "@odata.type": "#microsoft.graph.fileAttachment",
                        "name": "logo.png",
                        "contentType": "image/png",
                        "contentBytes": "AA==",
                        "contentId": "logo",
                    },
                    write_role="lexi",
                )

    # The sandbox loopback BCC rides along in test mode; what regressed is the
    # HubSpot one, so assert on that specifically rather than the whole list.
    assert "12345@bcc.hubspot.com" in (sent.get("bcc_recipients") or []), (
        f"the HTML draft path sent no HubSpot BCC: {sent.get('bcc_recipients')!r}"
    )


def test_outbound_bcc_skips_internal_recipients():
    """Internal-only mail is never logged to the CRM."""
    from app.integrations import outlook_email as oe

    with patch.object(oe, "hubspot_bcc_addresses", return_value=[]):
        assert "bcc.hubspot.com" not in " ".join(
            oe.outbound_bcc_addresses("kory.mitchell@iconicfounders.com")
        )


def test_draft_arguments_omit_bcc_when_there_is_none():
    """No empty bcc_recipients key — Composio rejects some empty list fields."""
    from app.integrations import outlook_email as oe

    args = oe._build_outlook_draft_arguments(
        recipient="someone@outside-company.com", subject="s", body="b", is_html=True
    )
    assert "bcc_recipients" not in args
