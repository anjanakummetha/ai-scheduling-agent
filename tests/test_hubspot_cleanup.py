"""The HubSpot cleanup path: junk detection, phone mining, apply and undo.

Measured against Kory's live book on 2026-08-14: 1,022 contacts, 79 with no job
title, 75 with no company, 306 with no phone — and ~30 carrying
"Prefer No Connection to Company", which is not blank, so no NOT_HAS_PROPERTY
filter ever surfaced them and no blank-only guard ever offered to fix them.

The write path is exercised here against a recorder rather than HubSpot. A
merge in the shared IFG portal is permanent and 16 of Kory's 27 duplicate pairs
involve a colleague's records.
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
    monkeypatch.setattr(hs, "owner_is_known", lambda oid: True)


@pytest.fixture
def db():
    """The undo log is written to and read from a real SQLite table, not a mock.

    Uses the suite's own database (conftest points LEXI_DATABASE_PATH at it
    before settings load — re-pointing it here would not take effect, since
    already-imported modules keep the original reference). Batch ids are uuids,
    so runs cannot collide.
    """
    from app.storage.lexi_db import get_lexi_connection

    with get_lexi_connection() as conn:
        conn.execute("DROP TABLE IF EXISTS hubspot_applied_writes")
        # Recreated empty, so "no rows" is a real answer rather than a missing
        # table — the tests that assert nothing was logged need to query it.
        hs.ensure_applied_writes_table(conn)
        conn.execute("DROP TABLE IF EXISTS hubspot_enrichment_checks")
        conn.commit()
    return True


# --- junk that is technically populated -------------------------------------


def test_the_linkedin_hidden_employer_string_counts_as_missing():
    """The single most common junk value in Kory's book: 12 per 400 sampled."""
    assert hs.is_placeholder("Prefer No Connection to Company", field="company")
    assert hs.is_placeholder("prefer no connection to company.", field="company")
    assert hs.is_placeholder("Prefer not to say", field="jobtitle")


@pytest.mark.parametrize(
    "value",
    [
        "No Limits Consulting",   # starts with "No"
        "Nonesuch Capital",       # starts with "None"
        "Tbd Ventures LLC",       # starts with "TBD"
        "NA Partners Group",      # starts with "NA"
        "None of the Above Media",
        "Alpine Investors",
        "HRTLND",
    ],
)
def test_real_company_names_that_merely_start_like_junk_are_left_alone(value):
    """Marking a real value as junk makes enrichment overwrite good data.

    That is worse than leaving junk in place, so the match is anchored to the
    whole field and never a substring.
    """
    assert not hs.is_placeholder(value, field="company")


# --- phone out of a signature ------------------------------------------------


def test_phone_is_mined_from_the_signature_block():
    fields = hs._extract_signature_fields(
        "Jane Doe\nManaging Partner at Acme Capital\nMobile: (303) 555-1234\n",
        contact_name="Jane Doe",
    )
    assert fields["phone"] == "(303) 555-1234"
    assert fields["jobtitle"] == "Managing Partner"


def test_a_direct_line_beats_the_switchboard():
    fields = hs._extract_signature_fields(
        "Jane Doe\nPartner at Acme\nOffice 303-555-0000\nCell 720-555-9999\n",
        contact_name="Jane Doe",
    )
    assert fields["phone"] == "720-555-9999"


def test_a_fax_number_is_never_written_as_a_phone_number():
    fields = hs._extract_signature_fields(
        "Jane Doe\nPartner at Acme\nFax: 303-555-0000\n", contact_name="Jane Doe"
    )
    assert "phone" not in fields


def test_no_phone_is_taken_when_the_signature_block_cannot_be_located():
    """Without a name or title anchor, any number in the body belongs to whoever
    wrote that part of the thread — usually not the contact."""
    fields = hs._extract_signature_fields("Call me on 303-555-1234 tomorrow.\n")
    assert "phone" not in fields


@pytest.mark.parametrize("number", ["(800) 962-0418", "1-888-555-0000", "877.555.0100"])
def test_a_toll_free_switchboard_is_never_written_as_someones_phone(number):
    """Found by the live rehearsal: a signature offered (800) 962-0418 as its
    owner's number. On a contact record that reads as a direct line, which is
    worse than the blank it replaced."""
    fields = hs._extract_signature_fields(
        f"Jane Doe\nPartner at Acme\n{number}\n", contact_name="Jane Doe"
    )
    assert "phone" not in fields


def test_a_direct_line_still_wins_when_a_toll_free_number_sits_above_it():
    fields = hs._extract_signature_fields(
        "Jane Doe\nPartner at Acme\nMain: (800) 962-0418\nCell: 303-555-1234\n",
        contact_name="Jane Doe",
    )
    assert fields["phone"] == "303-555-1234"


def test_a_short_number_is_not_a_phone_number():
    fields = hs._extract_signature_fields(
        "Jane Doe\nPartner at Acme\nSuite 300 555 1234\n", contact_name="Jane Doe"
    )
    assert "phone" not in fields


# --- proposals ---------------------------------------------------------------


@patch("app.integrations.hubspot_manager.hubspot_configured", return_value=True)
@patch("app.integrations.hubspot_manager._signature_fields_for")
@patch("app.integrations.hubspot_manager.search_contacts")
def test_a_placeholder_company_is_offered_for_replacement(mock_search, mock_sig, _cfg, db):
    mock_search.return_value = {
        "total": 1,
        "count": 1,
        "contacts": [
            {
                "id": "1",
                "email": "b@x.com",
                "name": "Bob",
                "jobtitle": "Partner",
                "company": "Prefer No Connection to Company",
                "hubspot_owner_id": KORY,
            }
        ],
    }
    mock_sig.return_value = ({"company": "Real Capital"}, {"message_id": "m1"})

    out = hs.propose_field_enrichment(limit=5)

    assert out["proposal_count"] == 1
    row = out["proposals"][0]
    assert row["proposed_fields"] == {"company": "Real Capital"}
    # He can see it is replacing junk, not filling a blank, without opening HubSpot.
    assert row["replacing"] == {"company": "Prefer No Connection to Company"}
    assert out["placeholder_replacements"] == 1
    assert "Prefer No Connection to Company" in out["kory_message"]


@patch("app.integrations.hubspot_manager.hubspot_configured", return_value=True)
@patch("app.integrations.hubspot_manager._signature_fields_for")
@patch("app.integrations.hubspot_manager.search_contacts")
def test_phone_is_only_proposed_when_asked_for(mock_search, mock_sig, _cfg, db):
    contact = {
        "id": "1", "email": "b@x.com", "name": "Bob",
        "jobtitle": "", "company": "Real Co", "phone": "",
        "hubspot_owner_id": KORY,
    }
    mock_search.return_value = {"total": 1, "count": 1, "contacts": [contact]}
    mock_sig.return_value = ({"jobtitle": "CEO", "phone": "303-555-1234"}, {})

    without = hs.propose_field_enrichment(limit=5)
    assert without["proposals"][0]["proposed_fields"] == {"jobtitle": "CEO"}

    # Same contact twice: the second scan would skip it as already looked at.
    hs.clear_enrichment_check_cache()
    with_phone = hs.propose_field_enrichment(limit=5, include_phone=True)
    assert with_phone["proposals"][0]["proposed_fields"] == {
        "jobtitle": "CEO",
        "phone": "303-555-1234",
    }


@patch("app.integrations.hubspot_manager.hubspot_configured", return_value=True)
@patch("app.integrations.hubspot_manager._signature_fields_for")
@patch("app.integrations.hubspot_manager.search_contacts")
def test_a_contact_already_answered_is_not_offered_again(mock_search, mock_sig, _cfg, db):
    """The gap stays open until Kory applies, so a hit must be remembered too.

    Recording only the empty ones looks right and is not: a server-side sweep
    spun on the same 19 contacts for nine rounds, and "keep going" in Teams
    would have stalled the same way.
    """
    contact = {
        "id": "1", "email": "b@x.com", "name": "Bob",
        "jobtitle": "", "company": "Real Co", "hubspot_owner_id": KORY,
    }
    mock_search.return_value = {"total": 1, "count": 1, "contacts": [contact]}
    mock_sig.return_value = ({"jobtitle": "CEO"}, {})

    first = hs.propose_field_enrichment(limit=5, use_website=False)
    assert first["proposal_count"] == 1

    # Same contact, same unfilled gap — HubSpot has not been written to yet.
    second = hs.propose_field_enrichment(limit=5, use_website=False)
    assert second["proposal_count"] == 0
    assert second["skipped_recently_checked"] == 1


@patch("app.integrations.hubspot_manager.hubspot_configured", return_value=True)
@patch("app.integrations.hubspot_manager._signature_fields_for")
@patch("app.integrations.hubspot_manager.search_contacts")
def test_clearing_the_cache_makes_them_available_again(mock_search, mock_sig, _cfg, db):
    contact = {
        "id": "1", "email": "b@x.com", "name": "Bob",
        "jobtitle": "", "company": "Real Co", "hubspot_owner_id": KORY,
    }
    mock_search.return_value = {"total": 1, "count": 1, "contacts": [contact]}
    mock_sig.return_value = ({"jobtitle": "CEO"}, {})

    assert hs.propose_field_enrichment(limit=5, use_website=False)["proposal_count"] == 1
    hs.clear_enrichment_check_cache()
    assert hs.propose_field_enrichment(limit=5, use_website=False)["proposal_count"] == 1


# --- the person tier, wired in ----------------------------------------------


def _person_contact(**over):
    base = {
        "id": "1", "email": "chris@leftbankholdings.com", "name": "Chris Lefkovitz",
        "jobtitle": "", "company": "Leftbank Holdings", "hubspot_owner_id": KORY,
    }
    base.update(over)
    return base


@patch("app.integrations.hubspot_manager.hubspot_configured", return_value=True)
@patch("app.integrations.hubspot_manager._signature_fields_for", return_value=({}, {}))
@patch("app.integrations.hubspot_manager.search_contacts")
def test_a_corroborated_profile_fills_what_the_inbox_could_not(mock_search, _sig, _cfg, db):
    mock_search.return_value = {"total": 1, "count": 1, "contacts": [_person_contact()]}
    import app.integrations.hubspot_person_lookup as pl

    resolved = {
        "fields": {"jobtitle": "Founder and President", "hs_linkedin_url": "https://linkedin.com/in/x"},
        "evidence": {
            "jobtitle": {
                "source": "linkedin_profile_corroborated",
                "detail": "their profile lists Leftbank Holdings, which is the employer already on the record (https://linkedin.com/in/x).",
                "confidence": "corroborated",
            },
            "hs_linkedin_url": {"source": "linkedin_profile_corroborated", "detail": "x"},
        },
    }
    with patch.object(pl, "resolve_person", return_value=resolved) as lookup:
        out = hs.propose_field_enrichment(limit=5, use_website=False)

    # The employer already on the record is what it was checked against.
    assert lookup.call_args.kwargs["known_company"] == "Leftbank Holdings"
    row = out["proposals"][0]
    assert row["proposed_fields"]["jobtitle"] == "Founder and President"
    assert row["proposed_fields"]["hs_linkedin_url"] == "https://linkedin.com/in/x"
    assert "Leftbank Holdings" in row["evidence"]["jobtitle"]["detail"]
    assert out["sources"]["linkedin_profile_corroborated"] == 2


@patch("app.integrations.hubspot_manager.hubspot_configured", return_value=True)
@patch("app.integrations.hubspot_manager._signature_fields_for", return_value=({}, {}))
@patch("app.integrations.hubspot_manager.search_contacts")
def test_a_shared_mailbox_is_reported_not_listed_as_unresolved(mock_search, _sig, _cfg, db):
    """"I could not establish anything for this one" invites someone to go and
    find a job title for accounting@. Saying what it actually is does not."""
    contact = _person_contact(name="Exuma Funds General Mailbox", email="info@exumafunds.com")
    mock_search.return_value = {"total": 1, "count": 1, "contacts": [contact]}
    import app.integrations.hubspot_person_lookup as pl

    with patch.object(
        pl, "resolve_person",
        return_value={"skip": "not_a_person", "reason": "info@ is a role mailbox, not a person"},
    ):
        out = hs.propose_field_enrichment(limit=5, use_website=False)

    assert out["proposal_count"] == 0
    assert out["not_people"][0]["name"] == "Exuma Funds General Mailbox"
    assert out["needs_research"] == []
    assert "not people" in out["kory_message"]


@patch("app.integrations.hubspot_manager.hubspot_configured", return_value=True)
@patch("app.integrations.hubspot_manager._signature_fields_for", return_value=({}, {}))
@patch("app.integrations.hubspot_manager.search_contacts")
def test_someone_who_has_left_is_surfaced_rather_than_filled(mock_search, _sig, _cfg, db):
    mock_search.return_value = {"total": 1, "count": 1, "contacts": [_person_contact()]}
    import app.integrations.hubspot_person_lookup as pl

    with patch.object(
        pl, "resolve_person",
        return_value={
            "skip": "may_have_moved",
            "reason": "Chris Lefkovitz appears to have left Leftbank Holdings (profile shows the role ending 2025-04-01).",
            "profile_url": "https://linkedin.com/in/x",
        },
    ):
        out = hs.propose_field_enrichment(limit=5, use_website=False)

    assert out["proposal_count"] == 0
    assert out["may_have_moved"][0]["profile_url"] == "https://linkedin.com/in/x"
    assert "moved on" in out["kory_message"]


@patch("app.integrations.hubspot_manager.hubspot_configured", return_value=True)
@patch("app.integrations.hubspot_manager._signature_fields_for")
@patch("app.integrations.hubspot_manager.search_contacts")
def test_the_profile_lookup_is_skipped_when_cheaper_sources_answered(mock_search, mock_sig, _cfg, db):
    """It costs three network calls. Nothing should reach it that a signature
    already closed."""
    mock_search.return_value = {"total": 1, "count": 1, "contacts": [_person_contact()]}
    mock_sig.return_value = ({"jobtitle": "CEO"}, {})
    import app.integrations.hubspot_person_lookup as pl

    with patch.object(pl, "resolve_person") as lookup:
        hs.propose_field_enrichment(limit=5, use_website=False)
    lookup.assert_not_called()


@patch("app.integrations.hubspot_manager.hubspot_configured", return_value=True)
@patch("app.integrations.hubspot_manager._signature_fields_for", return_value=({}, {}))
@patch("app.integrations.hubspot_manager.search_contacts")
def test_the_profile_tier_can_be_turned_off(mock_search, _sig, _cfg, db):
    mock_search.return_value = {"total": 1, "count": 1, "contacts": [_person_contact()]}
    import app.integrations.hubspot_person_lookup as pl

    with patch.object(pl, "resolve_person") as lookup:
        hs.propose_field_enrichment(limit=5, use_website=False, use_person_lookup=False)
    lookup.assert_not_called()


@patch("app.integrations.hubspot_manager.hubspot_configured", return_value=True)
@patch("app.integrations.hubspot_manager._signature_fields_for", return_value=({}, {}))
@patch("app.integrations.hubspot_manager.search_contacts")
def test_a_placeholder_company_is_not_passed_off_as_a_known_employer(mock_search, _sig, _cfg, db):
    """Corroborating against "Prefer No Connection to Company" would match
    nothing and quietly disable the only guard that matters."""
    contact = _person_contact(company="Prefer No Connection to Company")
    mock_search.return_value = {"total": 1, "count": 1, "contacts": [contact]}
    import app.integrations.hubspot_person_lookup as pl

    with patch.object(pl, "resolve_person", return_value=None) as lookup:
        hs.propose_field_enrichment(limit=5, use_website=False)
    assert lookup.call_args.kwargs["known_company"] == ""


# --- apply, and the undo log ------------------------------------------------


def _stage(proposals: list[dict]) -> str:
    return hs._stage_hubspot_batch(
        batch_type="field_enrichment", payload={"proposals": proposals}
    )


@patch("app.integrations.hubspot_manager.hubspot_writes_blocked", return_value=False)
def test_applying_a_batch_records_what_each_write_replaced(_blocked, db):
    batch_id = _stage(
        [{
            "contact_id": "9",
            "proposed_fields": {"company": "Real Capital"},
            "evidence": {
                "company": {
                    "source": "hubspot_company_association",
                    "detail": "HubSpot already links this contact to company #77 (Real Capital).",
                }
            },
        }]
    )
    live = {
        "id": "9", "hubspot_owner_id": KORY,
        "company": "Prefer No Connection to Company",
    }
    with patch.object(hs, "contacts_by_ids", return_value=[live]):
        with patch.object(hs, "execute_hubspot_tool", return_value={"successful": True}):
            out = hs.execute_hubspot_batch(batch_id=batch_id, approved=True)

    assert out["applied"] == 1
    assert out["undo_batch_id"] == batch_id

    from app.storage.lexi_db import get_lexi_connection

    with get_lexi_connection() as conn:
        rows = conn.execute(
            "SELECT contact_id, field, old_value, new_value, source "
            "FROM hubspot_applied_writes WHERE batch_id = ?",
            (batch_id,),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["old_value"] == "Prefer No Connection to Company"
    assert rows[0]["new_value"] == "Real Capital"
    # The log carries the human-readable reason, not an opaque id: six months on,
    # "HubSpot already links this contact to company #77" is what makes a bad
    # fill judgeable without re-deriving where it came from.
    assert "links this contact to company #77" in rows[0]["source"]


@patch("app.integrations.hubspot_manager.hubspot_writes_blocked", return_value=False)
def test_nothing_is_logged_when_hubspot_refuses_the_write(_blocked, db):
    """Composio answers 200 with successful:false. An undo log that records a
    write which never landed would put back a value nobody changed."""
    batch_id = _stage([{"contact_id": "9", "proposed_fields": {"company": "Real Capital"}}])
    live = {"id": "9", "hubspot_owner_id": KORY, "company": ""}
    with patch.object(hs, "contacts_by_ids", return_value=[live]):
        with patch.object(hs, "execute_hubspot_tool", return_value={"successful": False}):
            out = hs.execute_hubspot_batch(batch_id=batch_id, approved=True)

    assert out["applied"] == 0
    assert out["errors"]

    from app.storage.lexi_db import get_lexi_connection

    with get_lexi_connection() as conn:
        rows = conn.execute(
            "SELECT 1 FROM hubspot_applied_writes WHERE batch_id = ?", (batch_id,)
        ).fetchall()
    assert rows == []


@patch("app.integrations.hubspot_manager.hubspot_writes_blocked", return_value=False)
def test_undo_puts_the_previous_value_back_including_blank(_blocked, db):
    batch_id = _stage(
        [{"contact_id": "9", "proposed_fields": {"jobtitle": "CEO", "company": "Real Capital"}}]
    )
    live = {"id": "9", "hubspot_owner_id": KORY, "jobtitle": "", "company": "N/A"}
    with patch.object(hs, "contacts_by_ids", return_value=[live]):
        with patch.object(hs, "execute_hubspot_tool", return_value={"successful": True}):
            hs.execute_hubspot_batch(batch_id=batch_id, approved=True)

    sent: list[dict] = []

    def _record(slug, args):
        sent.append(args)
        return {"successful": True}

    # Still holding exactly what the batch wrote, so the undo is unambiguous.
    after = {"id": "9", "hubspot_owner_id": KORY, "jobtitle": "CEO", "company": "Real Capital"}
    with patch.object(hs, "contacts_by_ids", return_value=[after]):
        with patch.object(hs, "execute_hubspot_tool", side_effect=_record):
            out = hs.revert_hubspot_batch(batch_id=batch_id, approved=True)

    assert out["ok"] is True
    assert out["reverted"] == 2
    # One call for the contact, not one per field.
    assert len(sent) == 1
    assert sent[0]["properties"] == {"jobtitle": "", "company": "N/A"}


@patch("app.integrations.hubspot_manager.hubspot_writes_blocked", return_value=False)
def test_undo_leaves_a_field_alone_once_someone_has_edited_it(_blocked, db):
    """The apply path re-checks before writing; the undo path did not.

    A batch can sit applied for weeks. If Kory corrects one of these fields by
    hand and then asks for the batch back, restoring the old value silently
    throws his correction away and calls it an undo.
    """
    batch_id = _stage(
        [{"contact_id": "9", "proposed_fields": {"jobtitle": "CEO", "company": "Real Capital"}}]
    )
    live = {"id": "9", "hubspot_owner_id": KORY, "jobtitle": "", "company": ""}
    with patch.object(hs, "contacts_by_ids", return_value=[live]):
        with patch.object(hs, "execute_hubspot_tool", return_value={"successful": True}):
            hs.execute_hubspot_batch(batch_id=batch_id, approved=True)

    # He has since fixed the title by hand. The company is untouched.
    edited = {
        "id": "9", "name": "Rob Walters", "hubspot_owner_id": KORY,
        "jobtitle": "Managing Partner", "company": "Real Capital",
    }
    sent: list[dict] = []
    with patch.object(hs, "contacts_by_ids", return_value=[edited]):
        with patch.object(hs, "execute_hubspot_tool", side_effect=lambda s, a: sent.append(a) or {"successful": True}):
            out = hs.revert_hubspot_batch(batch_id=batch_id, approved=True)

    assert out["reverted"] == 1
    assert sent[0]["properties"] == {"company": ""}
    assert [r["field"] for r in out["skipped_changed_since"]] == ["jobtitle"]
    assert "Managing Partner" in out["kory_message"]

    # And his edit is still standing in the log as un-reverted, so a later
    # forced undo can still find it.
    from app.storage.lexi_db import get_lexi_connection

    with get_lexi_connection() as conn:
        pending = conn.execute(
            "SELECT field FROM hubspot_applied_writes "
            "WHERE batch_id = ? AND reverted_at IS NULL",
            (batch_id,),
        ).fetchall()
    assert [r["field"] for r in pending] == ["jobtitle"]


@patch("app.integrations.hubspot_manager.hubspot_writes_blocked", return_value=False)
def test_force_rolls_back_even_a_field_that_was_edited(_blocked, db):
    """Sometimes the batch itself is the thing that has to go, edits and all."""
    batch_id = _stage([{"contact_id": "9", "proposed_fields": {"jobtitle": "CEO"}}])
    live = {"id": "9", "hubspot_owner_id": KORY, "jobtitle": ""}
    with patch.object(hs, "contacts_by_ids", return_value=[live]):
        with patch.object(hs, "execute_hubspot_tool", return_value={"successful": True}):
            hs.execute_hubspot_batch(batch_id=batch_id, approved=True)

    edited = {"id": "9", "hubspot_owner_id": KORY, "jobtitle": "Something Else"}
    with patch.object(hs, "contacts_by_ids", return_value=[edited]) as read:
        with patch.object(hs, "execute_hubspot_tool", return_value={"successful": True}):
            out = hs.revert_hubspot_batch(batch_id=batch_id, approved=True, force=True)

    assert out["reverted"] == 1
    assert out["skipped_changed_since"] == []
    read.assert_not_called()


@patch("app.integrations.hubspot_manager.hubspot_writes_blocked", return_value=False)
def test_undo_stops_rather_than_restore_blind_when_hubspot_cannot_be_read(_blocked, db):
    batch_id = _stage([{"contact_id": "9", "proposed_fields": {"jobtitle": "CEO"}}])
    hs._record_applied_write(
        batch_id=batch_id, contact_id="9", field="jobtitle", old_value="", new_value="CEO"
    )
    with patch.object(hs, "contacts_by_ids", side_effect=RuntimeError("Composio down")):
        with patch.object(hs, "execute_hubspot_tool") as write:
            out = hs.revert_hubspot_batch(batch_id=batch_id, approved=True)
    assert out["error_code"] == "precheck_failed"
    write.assert_not_called()


@patch("app.integrations.hubspot_manager.hubspot_writes_blocked", return_value=False)
def test_undo_is_not_repeatable(_blocked, db):
    """A second undo must not re-write stale values over whatever came after."""
    batch_id = _stage([{"contact_id": "9", "proposed_fields": {"jobtitle": "CEO"}}])
    live = {"id": "9", "hubspot_owner_id": KORY, "jobtitle": ""}
    with patch.object(hs, "contacts_by_ids", return_value=[live]):
        with patch.object(hs, "execute_hubspot_tool", return_value={"successful": True}):
            hs.execute_hubspot_batch(batch_id=batch_id, approved=True)
    after = {"id": "9", "hubspot_owner_id": KORY, "jobtitle": "CEO"}
    with patch.object(hs, "contacts_by_ids", return_value=[after]):
        with patch.object(hs, "execute_hubspot_tool", return_value={"successful": True}):
            hs.revert_hubspot_batch(batch_id=batch_id, approved=True)

    with patch.object(hs, "execute_hubspot_tool") as write:
        second = hs.revert_hubspot_batch(batch_id=batch_id, approved=True)
    assert second["error_code"] == "nothing_to_revert"
    write.assert_not_called()


def test_undo_needs_kory_before_it_touches_anything(db):
    batch_id = _stage([{"contact_id": "9", "proposed_fields": {"jobtitle": "CEO"}}])
    with patch.object(hs, "assert_kory_approved_write", side_effect=PermissionError("no")) as gate:
        with pytest.raises(PermissionError):
            hs.revert_hubspot_batch(batch_id=batch_id, approved=False)
    gate.assert_called_once()


@patch("app.integrations.hubspot_manager.hubspot_writes_blocked", return_value=True)
def test_undo_writes_nothing_while_live_writes_are_blocked(_blocked, db):
    batch_id = _stage([{"contact_id": "9", "proposed_fields": {"jobtitle": "CEO"}}])
    hs._record_applied_write(
        batch_id=batch_id, contact_id="9", field="jobtitle", old_value="", new_value="CEO"
    )
    with patch.object(hs, "execute_hubspot_tool") as write:
        out = hs.revert_hubspot_batch(batch_id=batch_id, approved=True)
    assert out["dry_run"] is True
    assert out["would_revert"] == 1
    write.assert_not_called()


@patch("app.integrations.hubspot_manager.hubspot_writes_blocked", return_value=False)
def test_a_colleagues_record_is_neither_written_nor_logged(_blocked, db):
    batch_id = _stage([{"contact_id": "9", "proposed_fields": {"jobtitle": "CEO"}}])
    with patch.object(hs, "contacts_by_ids", return_value=[{"id": "9", "hubspot_owner_id": HEIDI}]):
        with patch.object(hs, "execute_hubspot_tool") as write:
            out = hs.execute_hubspot_batch(batch_id=batch_id, approved=True)

    write.assert_not_called()
    assert out["not_kory_owned"] == 1
    assert "other people at IFG" in out["kory_message"]

    from app.storage.lexi_db import get_lexi_connection

    with get_lexi_connection() as conn:
        assert conn.execute(
            "SELECT 1 FROM hubspot_applied_writes WHERE batch_id = ?", (batch_id,)
        ).fetchall() == []


@patch("app.integrations.hubspot_manager.hubspot_writes_blocked", return_value=False)
def test_a_merge_result_never_offers_an_undo_that_does_not_exist(_blocked, db):
    """HubSpot cannot undo a merge, so Lexi must not imply she can."""
    batch_id = hs._stage_hubspot_batch(
        batch_type="duplicate_merge",
        payload={"pairs": [{"primary_id": "1", "duplicate_id": "2"}]},
    )
    both_his = [
        {"id": "1", "hubspot_owner_id": KORY},
        {"id": "2", "hubspot_owner_id": KORY},
    ]
    with patch.object(hs, "contacts_by_ids", return_value=both_his):
        with patch.object(hs, "execute_hubspot_tool", return_value={"successful": True}):
            out = hs.execute_hubspot_batch(
                batch_id=batch_id, approved=True, merge_pair="1:2"
            )

    assert out["applied"] == 1
    assert "undo_batch_id" not in out
    assert "permanent" in out["kory_message"]
    assert "cannot be undone" in out["kory_message"]
