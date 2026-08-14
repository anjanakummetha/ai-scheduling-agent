"""Writes to the shared IFG portal must not touch other people's records.

`lexi_hubspot_meeting_note` had this guard from the start. The merge and
enrichment apply paths did not — `propose_duplicate_merges` scans the whole
portal on purpose, and nothing downstream asked whose records a pair was.

The same shape as the Asana owner bug: a guard that exists but does not cover
the path that actually writes. Worse here, because a HubSpot merge is permanent
and the portal is shared with Heidi, Matt and Natalie.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import app.integrations.hubspot_manager as hs


KORY = "kory-owner-id"
HEIDI = "heidi-owner-id"


@pytest.fixture(autouse=True)
def _kory_is_kory(monkeypatch):
    monkeypatch.setattr(hs, "kory_owner_id", lambda: KORY)
    monkeypatch.setattr(hs, "owner_name", lambda oid: "Heidi Ross" if oid == HEIDI else "Kory")


def _contact(cid: str, owner: str) -> dict:
    return {"id": cid, "name": f"Contact {cid}", "hubspot_owner_id": owner}


# --- the guard itself -------------------------------------------------------


def test_merge_is_refused_when_a_record_belongs_to_someone_else():
    row = {"primary_id": "1", "duplicate_id": "2"}
    with patch.object(hs, "contacts_by_ids", return_value=[_contact("1", KORY), _contact("2", HEIDI)]):
        blocked = hs._merge_owner_block(row)
    assert blocked is not None
    assert blocked["error_code"] == "owner_confirmation_required"
    assert "Heidi Ross" in blocked["kory_message"]
    assert "permanent" in blocked["kory_message"]


def test_merge_is_allowed_when_kory_owns_both():
    row = {"primary_id": "1", "duplicate_id": "2"}
    with patch.object(hs, "contacts_by_ids", return_value=[_contact("1", KORY), _contact("2", KORY)]):
        assert hs._merge_owner_block(row) is None


def test_owner_ack_lets_a_deliberate_cross_owner_merge_through():
    """He is allowed to do it — he just has to say so first."""
    row = {"primary_id": "1", "duplicate_id": "2"}
    with patch.object(hs, "contacts_by_ids", return_value=[_contact("1", HEIDI), _contact("2", HEIDI)]):
        assert hs._merge_owner_block(row, owner_ack=True) is None


def test_guard_fails_closed_when_the_records_cannot_be_read():
    """Deliberately unlike the Asana guard, which fails open.

    An Asana mistake is reopened in one call; a HubSpot merge is not undone at
    all. Not knowing who owns a record is not permission to consume it.
    """
    row = {"primary_id": "1", "duplicate_id": "2"}
    with patch.object(hs, "contacts_by_ids", return_value=[_contact("1", KORY)]):
        blocked = hs._merge_owner_block(row)
    assert blocked is not None
    assert blocked["error_code"] == "owner_check_unavailable"


def test_unassigned_records_have_no_one_to_confirm_with():
    row = {"primary_id": "1", "duplicate_id": "2"}
    with patch.object(hs, "contacts_by_ids", return_value=[_contact("1", ""), _contact("2", "")]):
        assert hs._merge_owner_block(row) is None


# --- the proposal has to say whose records these are ------------------------


def test_proposal_marks_pairs_that_touch_a_colleagues_record():
    annotated = hs._owner_annotation(_contact("1", KORY), _contact("2", HEIDI))
    assert annotated["foreign_owners"] == ["Heidi Ross"]
    assert annotated["kory_owns_all"] is False


def test_proposal_is_quiet_when_every_record_is_his():
    annotated = hs._owner_annotation(_contact("1", KORY), _contact("2", KORY))
    assert annotated["foreign_owners"] == []
    assert annotated["kory_owns_all"] is True


# --- enrichment reports the reason, not just the count ----------------------


def test_enrichment_skips_a_colleagues_record():
    row = {"contact_id": "9", "proposed_fields": {"jobtitle": "CFO"}}
    with patch.object(hs, "contacts_by_ids", return_value=[_contact("9", HEIDI)]):
        with patch.object(hs, "execute_hubspot_tool") as write:
            assert hs._apply_field_fill(row) == "not_kory_owned"
    write.assert_not_called()


def test_enrichment_distinguishes_already_filled_from_not_his():
    """Both write nothing, and they mean opposite things to Kory."""
    row = {"contact_id": "9", "proposed_fields": {"jobtitle": "CFO"}}
    filled = {**_contact("9", KORY), "jobtitle": "Chief Financial Officer"}
    with patch.object(hs, "contacts_by_ids", return_value=[filled]):
        with patch.object(hs, "execute_hubspot_tool") as write:
            assert hs._apply_field_fill(row) == "already_filled"
    write.assert_not_called()


def test_enrichment_writes_when_the_record_is_his_and_the_field_is_blank():
    row = {"contact_id": "9", "proposed_fields": {"jobtitle": "CFO"}}
    with patch.object(hs, "contacts_by_ids", return_value=[_contact("9", KORY)]):
        with patch.object(
            hs, "execute_hubspot_tool", return_value={"successful": True, "data": {}}
        ) as write:
            assert hs._apply_field_fill(row) == "applied"
    assert write.call_count == 1
