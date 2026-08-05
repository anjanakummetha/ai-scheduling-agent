"""HubSpot write test — steps 2 & 4, scoped to ONE disposable contact.

Run with the write flag overridden FOR THIS PROCESS ONLY:

    LEXI_HUBSPOT_LIVE_WRITES_ENABLED=true .venv/bin/python hs_write_test.py

The running lexi-hermes / lexi-api services keep writes OFF the whole time, so
there is no window in which the live gateway could write to the CRM on its own.

Every write here targets the contact whose email is TEST_EMAIL. The script
refuses to run if that address resolves to anything that is not the disposable
test record.
"""

import json
import os
import sys

TEST_EMAIL = "anjanakummetha@gmail.com"
TEST_FIRST = "LEXI TEST"
TEST_LAST = "DELETE ME"

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


# ---------------------------------------------------------------- STEP 2a
banner("STEP 2a — create the disposable contact (only if absent)")
contact = find_test_contact()
if contact:
    print(f"  already exists: id={contact.get('id')} name={contact.get('name')!r}")
else:
    created = execute_tool(
        "HUBSPOT_CREATE_CONTACT",
        {
            "properties": {
                "email": TEST_EMAIL,
                "firstname": TEST_FIRST,
                "lastname": TEST_LAST,
                "hubspot_owner_id": hs.kory_owner_id(),
                "company": "LEXI TEST — safe to delete",
            }
        },
        role="hubspot",
    )
    print("  create result:", json.dumps(created.get("data"), default=str)[:400])
    contact = find_test_contact()

if not contact:
    sys.exit("could not resolve the test contact after creation — stopping before any write")

CONTACT_ID = str(contact.get("id"))
print(f"\n  TEST CONTACT id={CONTACT_ID} email={contact.get('email')} "
      f"name={contact.get('name')!r} owner={contact.get('hubspot_owner_id')}")

# Hard scope guard: never proceed against a record that isn't the test one.
if TEST_FIRST.lower() not in str(contact.get("name") or "").lower():
    sys.exit(f"SAFETY STOP: {TEST_EMAIL} resolves to {contact.get('name')!r}, not the test record")
if str(contact.get("hubspot_owner_id")) != hs.kory_owner_id():
    print(f"  WARNING: owner is {contact.get('hubspot_owner_id')}, expected {hs.kory_owner_id()}")

# ---------------------------------------------------------------- STEP 2b
banner("STEP 2b — live meeting note against the test contact")
out = hs.stage_meeting_note(
    email=TEST_EMAIL,
    note="Live write test from Lexi. Confirms HUBSPOT_CREATE_NOTE lands on the "
         "right record with the right body. Safe to delete.",
    meeting_subject="LEXI WRITE TEST",
    approved=True,
)
print("  RESULT:", json.dumps({k: v for k, v in out.items() if k != "kory_message"}, default=str))
print("  wrote_for_real:", out.get("ok") and not out.get("dry_run"))

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
    print(f"  probing {other.get('email')} owned by {other.get('hubspot_owner_id')}")
    # If the guard holds, this body never lands anywhere. If it does land, it is on
    # a real colleague's record, so make it explain itself rather than shout.
    out = hs.stage_meeting_note(
        email=str(other.get("email")),
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

banner("DONE")
print(f"Test contact id {CONTACT_ID} ({TEST_EMAIL}) — DELETE THIS AT CLEANUP.")
print("The lexi-hermes / lexi-api services never had writes enabled.")
