"""HubSpot write test — steps 2 & 4, scoped to ONE disposable contact.

Run with the write flag overridden FOR THIS PROCESS ONLY:

    LEXI_HUBSPOT_LIVE_WRITES_ENABLED=true .venv/bin/python hs_write_test.py

The running lexi-hermes / lexi-api services keep writes OFF the whole time, so
there is no window in which the live gateway could write to the CRM on its own.

Every write here targets the contact whose email is TEST_EMAIL. The script
refuses to run if that address resolves to anything that is not the disposable
test record.

HubSpot is ONE portal shared by all of IFG — an owner id is a property, not a
partition — so the test contact is visible to everyone until it is gone. It is
therefore archived automatically at the end of the run, pass or fail, and the
note is read back before that so the evidence survives the cleanup. Pass --keep
to leave it in place for eyeballing in the UI.

Cleanup uses HUBSPOT_ARCHIVE_CONTACT — HubSpot's recycle bin, restorable for 90
days. Never the GDPR variants: those erase permanently AND blacklist the address
from ever being added to this portal again.
"""

import json
import os
import sys
import time
from pathlib import Path

# Running as `python scripts/hs_write_test.py` puts scripts/ on sys.path, not the
# repo root, so `app` is not importable without this. Same idiom as verify_stack.py.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TEST_EMAIL = "anjanakummetha@gmail.com"
TEST_FIRST = "LEXI TEST"
TEST_LAST = "DELETE ME"
KEEP = "--keep" in sys.argv

if os.getenv("LEXI_HUBSPOT_LIVE_WRITES_ENABLED", "").lower() not in {"1", "true", "yes"}:
    sys.exit("refusing to run: LEXI_HUBSPOT_LIVE_WRITES_ENABLED is not true for this process")

from app.config import settings  # noqa: E402
from app.integrations import hubspot_manager as hs  # noqa: E402
from app.integrations.composio_client import execute_tool  # noqa: E402

print(f"writes_blocked (this process): {hs.hubspot_writes_blocked()}")
print(f"kory_owner_id                : {hs.kory_owner_id()}")
print(f"bcc_enabled                  : {settings.hubspot_bcc_enabled}")
assert not hs.hubspot_writes_blocked(), "flag override did not take effect"


def find_test_contact():
    found = hs.search_contacts(
        limit=10, filters=[{"propertyName": "email", "operator": "EQ", "value": TEST_EMAIL}]
    )
    return (found.get("contacts") or [None])[0]


def banner(title):
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def read_contact_by_id(contact_id, *, archived=False):
    """Direct by-id read — returns (record_or_None, errored).

    hubspot_manager.contacts_by_ids() looks like a by-id read but is implemented
    over the SEARCH api, so it inherits the same index lag and reports a record
    created seconds ago as missing. HUBSPOT_READ_CONTACT hits the object
    endpoint and is consistent immediately after a write.

    `errored` is returned separately so a failed call is never mistaken for
    "the record isn't there" — that conflation is what made the first run's
    archive check weaker than it looked.
    """
    try:
        result = execute_tool(
            "HUBSPOT_READ_CONTACT",
            {
                "contactId": str(contact_id),
                "archived": archived,
                "properties": ["email", "firstname", "lastname", "hubspot_owner_id",
                               "hs_lead_status"],
            },
            role="hubspot",
        )
    except Exception:
        # A 404 for a live read on an archived contact surfaces as an exception.
        return None, True
    data = result.get("data") or {}
    if not data.get("id"):
        return None, False
    props = data.get("properties") or {}
    first = props.get("firstname") or ""
    last = props.get("lastname") or ""
    return {
        "id": data.get("id"),
        "email": props.get("email"),
        "name": f"{first} {last}".strip(),
        "hubspot_owner_id": props.get("hubspot_owner_id"),
        "hs_lead_status": props.get("hs_lead_status"),
    }, False


CONTACT_ID = ""


def cleanup():
    """Archive the test contact. Always runs — the portal is shared by all of IFG."""
    banner("CLEANUP — archive the disposable contact")
    if not CONTACT_ID:
        print("  no contact was created — nothing to clean up.")
        return
    if KEEP:
        print(f"  --keep given: leaving contact {CONTACT_ID} ({TEST_EMAIL}) in the portal.")
        print("  Archive it yourself in HubSpot, or re-run without --keep.")
        return
    try:
        # ARCHIVE, never the GDPR delete: GDPR erases permanently and blacklists
        # the address from ever being re-added to this portal.
        execute_tool("HUBSPOT_ARCHIVE_CONTACT", {"contactId": CONTACT_ID}, role="hubspot")
        # Positive proof, both directions: gone from the live view AND present in
        # the archived view. Absence from the live view alone is not evidence —
        # a failed call looks identical, and a search-based check would also be
        # fooled by index lag.
        live, _ = read_contact_by_id(CONTACT_ID, archived=False)
        binned, _ = read_contact_by_id(CONTACT_ID, archived=True)
        if live:
            print(f"  ⚠️  archive returned but contact {CONTACT_ID} is STILL LIVE "
                  f"({live.get('email')}) — delete it by hand in HubSpot.")
        elif binned:
            print(f"  ✅ archived contact {CONTACT_ID} — gone from the shared portal, "
                  "confirmed present in the recycle bin (restorable 90 days).")
        else:
            print(f"  ⚠️  contact {CONTACT_ID} reads as neither live nor archived. "
                  "Check it by hand in HubSpot.")
    except Exception as exc:
        print(f"  ⚠️  ARCHIVE FAILED: {type(exc).__name__}: {exc}")
        print(f"  DELETE CONTACT {CONTACT_ID} ({TEST_EMAIL}) BY HAND in HubSpot.")


# Everything that can create or write runs inside this try. The create used to sit
# above it, so a failure between creating the contact and entering the block left
# the record sitting in a portal the whole company can see.
try:
    # ------------------------------------------------------------ STEP 2a
    banner("STEP 2a — create the disposable contact (only if absent)")
    contact = find_test_contact()
    if contact:
        CONTACT_ID = str(contact.get("id") or "")
        print(f"  already exists: id={CONTACT_ID} name={contact.get('name')!r}")
    else:
        # Composio's HUBSPOT_CREATE_CONTACT takes FLAT top-level fields, not the
        # nested {"properties": {...}} the HubSpot REST API itself uses. Passing
        # the nested shape is silently accepted and creates an empty contact —
        # which is what produced three blank records before this was spotted.
        created = execute_tool(
            "HUBSPOT_CREATE_CONTACT",
            {
                "email": TEST_EMAIL,
                "firstname": TEST_FIRST,
                "lastname": TEST_LAST,
                "hubspot_owner_id": hs.kory_owner_id(),
                "company": "LEXI TEST — safe to delete",
            },
            role="hubspot",
        )
        data = created.get("data") or {}
        # Take the id straight from the create response. Searching for the record
        # we just made loses it: HubSpot's search index trails writes by seconds,
        # and a miss there means an orphaned contact nobody knows to delete.
        CONTACT_ID = str(data.get("id") or "")
        print("  create result:", json.dumps(data, default=str)[:300])
        print(f"  created id: {CONTACT_ID or '(NONE — see payload above)'}")

    if not CONTACT_ID:
        raise RuntimeError("no contact id after create — stopping before any write")

    # Read the canonical record back with the direct by-id endpoint and check the
    # scope guard against that.
    record, read_failed = read_contact_by_id(CONTACT_ID)
    if read_failed or record is None:
        raise RuntimeError(
            f"could not read contact {CONTACT_ID} back by id "
            f"({'call failed' if read_failed else 'no record'}) — no writes attempted"
        )
    print(f"\n  TEST CONTACT id={CONTACT_ID} email={record.get('email')} "
          f"name={record.get('name')!r} owner={record.get('hubspot_owner_id')}")

    # Hard scope guard: never write against a record that isn't the test one.
    if TEST_FIRST.lower() not in str(record.get("name") or "").lower():
        raise RuntimeError(
            f"SAFETY STOP: id {CONTACT_ID} reads back as {record.get('name')!r}, "
            "not the test record — no writes attempted"
        )
    if str(record.get("hubspot_owner_id")) != hs.kory_owner_id():
        print(f"  WARNING: owner is {record.get('hubspot_owner_id')}, "
              f"expected {hs.kory_owner_id()}")

    # ------------------------------------------------------------ STEP 2a-wait
    banner("STEP 2a-wait — wait for the new contact to become searchable")
    # stage_meeting_note resolves its target by SEARCH, and HubSpot's search index
    # trails writes by seconds. Without this wait, 2b reports contact_not_found on
    # a record that demonstrably exists — testing the index, not the note path.
    # Poll the SAME lookup the note path uses. Polling find_test_contact() instead
    # reported "indexed after 1 attempt" while the note path still could not see
    # the record — they were querying two different indexes.
    searchable = None
    for attempt in range(1, 13):
        hits = hs.search_contacts(
            limit=5, filters=[{"propertyName": "email", "operator": "EQ", "value": TEST_EMAIL}]
        ).get("contacts") or []
        searchable = next(
            (c for c in hits if (c.get("email") or "").lower() == TEST_EMAIL.lower()), None
        )
        if searchable:
            print(f"  resolvable by the note path's lookup after {attempt} attempt(s)")
            break
        print(f"  not resolvable yet (attempt {attempt}/12) — waiting 5s")
        time.sleep(5)

    if not searchable:
        print("  ⚠️  still not searchable after 60s. Skipping 2b/2c — a "
              "contact_not_found here would be the index, not the note path.")
        print("  The guardrail probes below do not depend on it. Re-run in a minute for 2b.")

    # ------------------------------------------------------------ STEP 2b
    if searchable:
        banner("STEP 2b — live meeting note against the test contact")
        out = hs.stage_meeting_note(
            email=TEST_EMAIL,
            note="Live write test from Lexi. Confirms HUBSPOT_CREATE_NOTE lands on the "
                 "right record with the right body. Safe to delete.",
            meeting_subject="LEXI WRITE TEST",
            approved=True,
        )
        print("  RESULT:",
              json.dumps({k: v for k, v in out.items() if k != "kory_message"}, default=str))
        print("  wrote_for_real:", out.get("ok") and not out.get("dry_run"))

    # ------------------------------------------------------------ STEP 2c
    if searchable:
        banner("STEP 2c — read the note back off the contact")
        # The contact gets archived at the end, so verify here rather than promising
        # to go look in the UI later. A create call returning ok is not proof the
        # body landed on the right record.
        try:
            notes = execute_tool(
                "HUBSPOT_LIST_CONTACT_NOTES", {"contact_id": CONTACT_ID, "limit": 10},
                role="hubspot",
            )
            raw = json.dumps(notes.get("data"), default=str)
            print(f"  notes payload ({len(raw)} chars):", raw[:600])
            print("  body found on the record:", "HUBSPOT_CREATE_NOTE lands on the" in raw)
        except Exception as exc:
            print(f"  read-back failed: {type(exc).__name__}: {exc}")

    # ---------------------------------------------------------------- STEP 4
    banner("STEP 4a — GUARDRAIL: nonexistent contact must refuse, write nothing")
    out = hs.stage_meeting_note(
        email="definitely-not-a-real-contact@nowhere.invalid", note="should never be written",
        approved=True,
    )
    print("  ok:", out.get("ok"), "| error_code:", out.get("error_code"))
    print("  ", (out.get("kory_message") or "")[:200])

    banner("STEP 4b — GUARDRAIL: approval required (approved=False)")
    try:
        hs.stage_meeting_note(email=TEST_EMAIL, note="unapproved — must not be written", approved=False)
        print("  ❌ NO REFUSAL — approval gate did not fire")
    except PermissionError as exc:
        print(f"  ✅ refused: {exc}")

    banner("STEP 4c — GUARDRAIL: fuzzy match must not be written to (the 5df79e9 fix)")
    # A name-shaped query that HubSpot's loose search will answer with SOMEBODY,
    # but which is nobody's actual address.
    out = hs.stage_meeting_note(
        email="kory@thisisnotarealdomainforlexi.invalid", note="should never be written", approved=True
    )
    print("  ok:", out.get("ok"), "| error_code:", out.get("error_code"))
    print("  near_matches:", json.dumps(out.get("near_matches"), default=str))

    banner("STEP 4d — GUARDRAIL: contact owned by another IFG employee")
    other = None
    try:
        scan = hs.search_contacts(limit=200, properties=["email", "firstname", "lastname",
                                                         "hubspot_owner_id", "hs_lead_status"])
        for c in scan.get("contacts") or []:
            owner = str(c.get("hubspot_owner_id") or "")
            if owner and owner != hs.kory_owner_id() and (c.get("email") or ""):
                other = c
                break
    except Exception as exc:
        print("  scan failed:", exc)

    if not other:
        print("  (no non-Kory-owned contact found in the sample — skipped)")
    else:
        print(f"  candidate: {other.get('email')} owned by {other.get('hubspot_owner_id')}")

        # Two-phase, so this probe can never be the thing that writes to a
        # colleague's record. Phase 1 reproduces exactly what stage_meeting_note
        # does internally — same lookup, same exact-email match, same guard — but
        # stops before the write. assert_contact_writable is a pure function over
        # the contact dict, so this costs nothing but a read. Only if it already
        # refuses do we exercise the full path, at which point the refusal is the
        # proven outcome rather than the hoped-for one. Testing a guard by
        # trusting the guard is circular; this breaks the circle.
        #
        # It resolves the record the same way rather than reusing the scan's dict:
        # a guard verdict is only meaningful on the dict the write path will
        # actually hold, and the two lookups are separate calls.
        probe_email = str(other.get("email"))
        resolved = None
        for cand in hs.search_contacts(limit=5, query=probe_email).get("contacts") or []:
            if (cand.get("email") or "").strip().lower() == probe_email.strip().lower():
                resolved = cand
                break

        if resolved is None:
            # The write path would refuse on contact_not_found before ever
            # reaching the ownership guard, so there is nothing here to prove.
            print("  the write path would not resolve this address — skipped, nothing attempted")
        else:
            precheck = hs.assert_contact_writable(resolved, owner_ack=False)
            print(f"  phase 1 (read-only): owner={resolved.get('hubspot_owner_id')} "
                  f"→ guard says {precheck.get('error_code') if precheck else 'WRITABLE'}")

            if not precheck or precheck.get("error_code") != "owner_confirmation_required":
                print("  ❌ OWNERSHIP GUARD FAILED at the read-only check.")
                print("     Skipping the live call — no note attempted on a real record.")
                print(f"     contact={probe_email} owner={resolved.get('hubspot_owner_id')} "
                      f"kory={hs.kory_owner_id()}")
                raise AssertionError("ownership guard did not refuse a non-Kory-owned contact")

            print("  phase 2: guard already refused this record, so the live call is safe to make")
            out = hs.stage_meeting_note(
                email=probe_email,
                note=(
                    "Lexi ownership-guard test. If you are reading this on a real contact, the "
                    "guard that blocks writes to another owner's record failed — please tell "
                    "Anjana and delete this note."
                ),
                approved=True,
            )
            print("  ok:", out.get("ok"), "| error_code:", out.get("error_code"))
            print("  ", (out.get("error") or "")[:200])
            assert out.get("error_code") == "owner_confirmation_required", "OWNERSHIP GUARD FAILED"

    banner("STEP 4e — GUARDRAIL: Do Not Contact record (note path — ruling pending)")
    dnc = None
    try:
        found = hs.search_contacts(
            limit=5,
            filters=[
                {"propertyName": "hubspot_owner_id", "operator": "EQ", "value": hs.kory_owner_id()},
                {"propertyName": "hs_lead_status", "operator": "EQ", "value": "Do Not Contact"},
            ],
        )
        dnc = (found.get("contacts") or [None])[0]
    except Exception as exc:
        print("  DNC lookup failed:", exc)

    if not dnc:
        print("  (no DNC contact found — skipped)")
    else:
        print(f"  A DNC record exists: id={dnc.get('id')} status={dnc.get('hs_lead_status')!r}")
        print("  NOT probing it with a write — notes on DNC records are an open ruling for Kory.")
        print("  Outreach exclusion is already proven; this line is informational only.")

finally:
    cleanup()
    banner("DONE")
    print("The lexi-hermes / lexi-api services never had writes enabled.")
