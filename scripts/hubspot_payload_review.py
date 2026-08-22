"""HubSpot write-path payload review (step 1 of the HubSpot write-test plan).

Captures the EXACT Composio payload every HubSpot write path would emit with
live writes ENABLED, without any network call: execute_hubspot_tool is stubbed.
Also exercises the guardrails (DNC, non-owned, missing contact) and the BCC
address logic.

Run:  .venv/bin/python <this file>
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

REPO = Path("/Users/anjanakummetha/Downloads/IFG 2026 Summer Internship/AI_Scheduling_Agent")
sys.path.insert(0, str(REPO))

# Hermetic DB + non-prod env before anything imports config.
_tmp = tempfile.mkdtemp(prefix="hs-review-")
os.environ["LEXI_DATABASE_PATH"] = str(Path(_tmp) / "lexi.db")
os.environ["LEXI_ENV"] = "test"
os.environ["LEXI_DRY_RUN"] = "false"
os.environ["LEXI_HUBSPOT_LIVE_WRITES_ENABLED"] = "true"
os.environ["HUBSPOT_KORY_OWNER_ID"] = "KORY_OWNER"
os.environ["LEXI_HUBSPOT_BCC_ADDRESS"] = "242757246@bcc.na2.hubspot.com"

from app.config import settings  # noqa: E402
from scripts.init_lexi_db import init_lexi_db  # noqa: E402

init_lexi_db(Path(os.environ["LEXI_DATABASE_PATH"]))

import app.integrations.hubspot_manager as hs  # noqa: E402


def set_setting(name, value):
    """settings is a frozen dataclass — assign through object.__setattr__."""
    object.__setattr__(settings, name, value)


set_setting("hubspot_kory_owner_id", "KORY_OWNER")

CALLS: list[tuple[str, dict]] = []


def fake_execute(tool, arguments, **kwargs):
    CALLS.append((tool, arguments))
    return {"data": {"id": "STUB"}, "log_id": "stub-log"}


KORY_CONTACT = {
    "id": "C-TEST",
    "email": "anjanakummetha@gmail.com",
    "name": "LEXI TEST",
    "hubspot_owner_id": "KORY_OWNER",
    "hs_lead_status": "Active",
    "jobtitle": "",
    "company": "",
}
OTHER_OWNER_CONTACT = dict(KORY_CONTACT, id="C-OTHER", hubspot_owner_id="SOMEONE_ELSE")
DNC_CONTACT = dict(KORY_CONTACT, id="C-DNC", hs_lead_status="Do Not Contact")
UNASSIGNED_CONTACT = dict(KORY_CONTACT, id="C-NONE", hubspot_owner_id="")


def show(title):
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def dump_calls(label):
    if not CALLS:
        print(f"  [{label}] NO Composio call emitted")
    for tool, args in CALLS:
        print(f"  [{label}] TOOL: {tool}")
        print("           ARGS: " + json.dumps(args, indent=2, default=str).replace("\n", "\n                 "))
    CALLS.clear()


# ---------------------------------------------------------------- write paths

show("W1. stage_meeting_note — Kory-owned contact, approved, writes ON")
with patch.object(hs, "execute_hubspot_tool", side_effect=fake_execute), patch.object(
    hs, "search_contacts", return_value={"contacts": [KORY_CONTACT]}
):
    out = hs.stage_meeting_note(
        email="anjanakummetha@gmail.com",
        note="Discussed the Q3 raise; sending the deck Monday.",
        meeting_subject="Intro call",
        approved=True,
    )
print("  RESULT:", json.dumps({k: v for k, v in out.items() if k != "kory_message"}, default=str))
dump_calls("W1")

show("W2. execute_hubspot_batch — meeting_note batch")
with patch.object(hs, "execute_hubspot_tool", side_effect=fake_execute), patch.object(
    hs, "search_contacts", return_value={"contacts": [KORY_CONTACT]}
):
    staged = hs.stage_meeting_note(
        email="anjanakummetha@gmail.com", note="Batch note body", approved=True
    )
    CALLS.clear()
    out = hs.execute_hubspot_batch(batch_id=staged["batch_id"], approved=True)
print("  RESULT:", json.dumps(out, default=str))
dump_calls("W2")

show("W3. execute_hubspot_batch — field_enrichment (blank-only re-check at apply)")
with patch.object(hs, "execute_hubspot_tool", side_effect=fake_execute), patch.object(
    hs, "hubspot_configured", return_value=True
), patch.object(
    hs, "search_contacts", return_value={"contacts": [KORY_CONTACT], "total": 1, "count": 1}
), patch.object(
    hs, "_signature_fields_for", return_value={"jobtitle": "VP Finance", "company": "Acme"}
):
    proposed = hs.propose_field_enrichment(limit=5)
    print("  proposals:", json.dumps(proposed["proposals"], default=str))
    CALLS.clear()
    # apply-time re-check reads the live record; still blank -> write proceeds
    with patch.object(hs, "contacts_by_ids", return_value=[KORY_CONTACT]):
        out = hs.execute_hubspot_batch(batch_id=proposed["batch_id"], approved=True)
print("  RESULT:", json.dumps(out, default=str))
dump_calls("W3")

show("W3b. field_enrichment apply when the field was filled in meanwhile (must NOT write)")
with patch.object(hs, "execute_hubspot_tool", side_effect=fake_execute), patch.object(
    hs, "hubspot_configured", return_value=True
), patch.object(
    hs, "search_contacts", return_value={"contacts": [KORY_CONTACT], "total": 1, "count": 1}
), patch.object(
    hs, "_signature_fields_for", return_value={"jobtitle": "VP Finance"}
):
    proposed = hs.propose_field_enrichment(limit=5)
    CALLS.clear()
    filled = dict(KORY_CONTACT, jobtitle="Chief Financial Officer")
    with patch.object(hs, "contacts_by_ids", return_value=[filled]):
        out = hs.execute_hubspot_batch(batch_id=proposed["batch_id"], approved=True)
print("  RESULT:", json.dumps(out, default=str))
dump_calls("W3b")

show("W4. execute_hubspot_batch — duplicate_merge (IRREVERSIBLE in HubSpot)")
with patch.object(hs, "execute_hubspot_tool", side_effect=fake_execute), patch.object(
    hs,
    "search_contacts",
    return_value={
        "contacts": [
            {"id": "1", "email": "dup@x.com", "name": "Ann Dup"},
            {"id": "2", "email": "dup@x.com", "name": "Ann Dup"},
        ],
        "count": 2,
        "total": 2,
    },
):
    proposed = hs.propose_duplicate_merges(limit=10)
    print("  pairs:", json.dumps(proposed["pairs"], default=str))
    CALLS.clear()
    out = hs.execute_hubspot_batch(batch_id=proposed["batch_id"], approved=True)
print("  RESULT:", json.dumps(out, default=str))
dump_calls("W4")

# ----------------------------------------------------------------- guardrails

show("G1. GUARDRAIL — approval: approved=False must raise (writes ON)")
try:
    with patch.object(hs, "execute_hubspot_tool", side_effect=fake_execute), patch.object(
        hs, "search_contacts", return_value={"contacts": [KORY_CONTACT]}
    ):
        hs.stage_meeting_note(email="anjanakummetha@gmail.com", note="x", approved=False)
    print("  ❌ NO REFUSAL")
except PermissionError as exc:
    print(f"  ✅ refused: {exc}")
dump_calls("G1")

show("G2. GUARDRAIL — non-owned contact, no owner_ack")
with patch.object(hs, "execute_hubspot_tool", side_effect=fake_execute), patch.object(
    hs, "search_contacts", return_value={"contacts": [OTHER_OWNER_CONTACT]}
), patch.object(hs, "owner_map", return_value={"SOMEONE_ELSE": "Heidi Colleague"}):
    out = hs.stage_meeting_note(email="anjanakummetha@gmail.com", note="x", approved=True)
print("  RESULT:", json.dumps(out, default=str))
dump_calls("G2")

show("G2b. non-owned contact WITH owner_ack=True (should proceed)")
with patch.object(hs, "execute_hubspot_tool", side_effect=fake_execute), patch.object(
    hs, "search_contacts", return_value={"contacts": [OTHER_OWNER_CONTACT]}
), patch.object(hs, "owner_map", return_value={"SOMEONE_ELSE": "Heidi Colleague"}):
    out = hs.stage_meeting_note(
        email="anjanakummetha@gmail.com", note="x", approved=True, owner_ack=True
    )
print("  RESULT:", json.dumps({k: v for k, v in out.items() if k != "kory_message"}, default=str))
dump_calls("G2b")

show("G2c. UNASSIGNED contact (owner id blank) — current code ALLOWS the write")
with patch.object(hs, "execute_hubspot_tool", side_effect=fake_execute), patch.object(
    hs, "search_contacts", return_value={"contacts": [UNASSIGNED_CONTACT]}
):
    out = hs.stage_meeting_note(email="anjanakummetha@gmail.com", note="x", approved=True)
print("  RESULT:", json.dumps({k: v for k, v in out.items() if k != "kory_message"}, default=str))
dump_calls("G2c")

show("G3. GUARDRAIL — nonexistent contact (no match) must refuse, stage nothing")
with patch.object(hs, "execute_hubspot_tool", side_effect=fake_execute), patch.object(
    hs, "search_contacts", return_value={"contacts": []}
):
    out = hs.stage_meeting_note(email="nobody@nowhere.invalid", note="x", approved=True)
print("  RESULT:", json.dumps(out, default=str))
dump_calls("G3")

show("G3b. FUZZY MATCH — search returns a DIFFERENT person than the email asked for")
with patch.object(hs, "execute_hubspot_tool", side_effect=fake_execute), patch.object(
    hs,
    "search_contacts",
    return_value={"contacts": [dict(KORY_CONTACT, id="C-WRONG", email="someone.else@corp.com",
                                    name="Someone Else")]},
):
    out = hs.stage_meeting_note(email="anjanakummetha@gmail.com", note="x", approved=True)
print("  RESULT:", json.dumps({k: v for k, v in out.items() if k != 'kory_message'}, default=str))
dump_calls("G3b")

show("G4b. DNC contact — does the meeting-note path check DNC at all?")
with patch.object(hs, "execute_hubspot_tool", side_effect=fake_execute), patch.object(
    hs, "search_contacts", return_value={"contacts": [DNC_CONTACT]}
):
    out = hs.stage_meeting_note(email="anjanakummetha@gmail.com", note="x", approved=True)
print("  RESULT:", json.dumps({k: v for k, v in out.items() if k != "kory_message"}, default=str))
dump_calls("G4b")

# ------------------------------------------------------------------ BCC logic

show("B1. hubspot_bcc_addresses — who gets BCC'd")
from app.integrations import outlook_email as oe  # noqa: E402

for enabled in (False, True):
    set_setting("hubspot_bcc_enabled", enabled)
    set_setting("hubspot_bcc_address", "242757246@bcc.na2.hubspot.com")
    for who in (["anjanakummetha@gmail.com"], ["kory.mitchell@iconicfounders.com"], []):
        print(f"  bcc_enabled={enabled!s:<5} to={who} -> {oe.hubspot_bcc_addresses(who)}")

show("B2. writes_blocked flag as reported to Kory")
for dry, live in ((True, True), (False, False), (False, True)):
    set_setting("lexi_dry_run", dry)
    set_setting("hubspot_live_writes_enabled", live)
    print(f"  lexi_dry_run={dry!s:<5} hubspot_live_writes_enabled={live!s:<5} -> blocked={hs.hubspot_writes_blocked()}")

print("\nDone. No network calls were made — execute_hubspot_tool was stubbed throughout.")
