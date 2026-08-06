"""HubSpot BCC end-to-end — step 3 of the write-test plan.

Sends ONE real email from the Lexi mailbox to an outside address with the HubSpot
logging BCC on, then proves HubSpot actually logged it, then cleans up.

Run with the BCC flag overridden FOR THIS PROCESS ONLY:

    LEXI_HUBSPOT_BCC_ENABLED=true .venv/bin/python scripts/hs_bcc_test.py

The running lexi-hermes / lexi-api services keep the BCC off throughout, so no
other outbound mail gets logged to the CRM while this runs.

What this actually does, in the open:
  - Sends a real email. It leaves lexi@iconicfounders.com and arrives in a real
    inbox. Kory's mailbox is not used and, by default, he is not CC'd.
  - HubSpot's BCC address auto-creates a contact for an address it does not
    recognise. So this creates a contact AND an email activity in the portal that
    all of IFG shares. Both are removed at the end.
"""

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TO_EMAIL = os.getenv("BCC_TEST_TO", "anjanakummetha@gmail.com")
# A copy in a mailbox you control. Nothing here reads Lexi's inbox — the script
# only sends from it — but without a CC the only record you can open is the
# received copy, and the CC also proves the send from a second angle.
CC_EMAIL = os.getenv("BCC_TEST_CC", "anjana.kummetha@iconicfounders.com").strip()
KEEP = "--keep" in sys.argv
CC_KORY = "--cc-kory" in sys.argv
# Diagnostic: HubSpot's BCC address only logs mail whose SENDER is a HubSpot user
# in the portal. lexi@iconicfounders.com is not one of the eight; Kory is. Sending
# the identical message from his mailbox isolates sender identity as the variable.
# This sends a real email as Kory, so it is opt-in and never the default.
FROM_KORY = "--from-kory" in sys.argv

if os.getenv("LEXI_HUBSPOT_BCC_ENABLED", "").lower() not in {"1", "true", "yes"}:
    sys.exit("refusing to run: LEXI_HUBSPOT_BCC_ENABLED is not true for this process")

# Off unless explicitly asked for: a test message should not land in the CEO's
# inbox, and the CC is not what this test is proving.
if not CC_KORY:
    os.environ["LEXI_CC_KORY_ENABLED"] = "false"

from app.config import settings  # noqa: E402
from app.integrations import hubspot_manager as hs  # noqa: E402
from app.integrations.composio_client import execute_tool  # noqa: E402
from app.integrations.outlook_email import (  # noqa: E402
    hubspot_bcc_addresses,
    send_outbound_email,
)

CHANNEL = "kory" if FROM_KORY else "lexi"
STAMP = time.strftime("%Y%m%d-%H%M%S")
SUBJECT = f"Lexi BCC logging test {STAMP}"
BODY = (
    "This is an automated test of Lexi's HubSpot logging BCC, run by Anjana.\n\n"
    f"Reference: {STAMP}\n\n"
    "It confirms that mail sent to people outside IFG is recorded on the right "
    "HubSpot contact. Nothing here needs a reply, and the test records are "
    "removed automatically."
)
if FROM_KORY:
    BODY += (
        "\n\nSent from Kory's mailbox deliberately: the same test from Lexi's "
        "mailbox logged nothing, and Lexi is not a HubSpot user. This isolates "
        "whether sender identity is the reason."
    )


def banner(title):
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


CONTACT_ID = ""
EMAIL_IDS: list[str] = []


def cleanup():
    """Remove whatever HubSpot auto-created. Always runs — the portal is shared."""
    banner("CLEANUP — remove the auto-created activity and contact")
    if KEEP:
        print(f"  --keep given: leaving contact {CONTACT_ID} and emails {EMAIL_IDS} in place.")
        return

    for email_id in EMAIL_IDS:
        try:
            execute_tool("HUBSPOT_ARCHIVE_EMAIL", {"emailId": email_id}, role="hubspot")
            print(f"  ✅ archived logged email {email_id}")
        except Exception as exc:
            print(f"  ⚠️  could not archive email {email_id}: {type(exc).__name__}: {exc}")

    if not CONTACT_ID:
        print("  no contact was auto-created — nothing else to remove.")
        return
    try:
        # ARCHIVE, never the GDPR delete: GDPR erases permanently AND blacklists
        # the address from ever being added to this portal again.
        execute_tool("HUBSPOT_ARCHIVE_CONTACT", {"contactId": CONTACT_ID}, role="hubspot")
        print(f"  ✅ archived contact {CONTACT_ID}")
    except Exception as exc:
        print(f"  ⚠️  ARCHIVE FAILED: {type(exc).__name__}: {exc}")
        print(f"  DELETE CONTACT {CONTACT_ID} ({TO_EMAIL}) BY HAND in HubSpot.")


def find_contact():
    hits = hs.search_contacts(
        limit=5, filters=[{"propertyName": "email", "operator": "EQ", "value": TO_EMAIL}]
    ).get("contacts") or []
    return next((c for c in hits if (c.get("email") or "").lower() == TO_EMAIL.lower()), None)


banner("PRE-FLIGHT")
print(f"  bcc_enabled (this process): {settings.hubspot_bcc_enabled}")
print(f"  bcc_address              : {settings.hubspot_bcc_address}")
print(f"  cc_kory_enabled          : {settings.cc_kory_enabled}")
print(f"  send channel             : {CHANNEL}"
      + ("  ← REAL EMAIL FROM KORY'S MAILBOX" if FROM_KORY else ""))
print(f"  sender                   : "
      f"{settings.kory_cc_email if FROM_KORY else settings.lexi_mailbox_email}")
print(f"  recipient (To)           : {TO_EMAIL}")
# send_outbound_email only applies cc_emails on the lexi channel.
print(f"  cc                       : "
      f"{(CC_EMAIL or '(none)') if CHANNEL == 'lexi' else '(dropped — kory channel ignores cc)'}")

resolved_bcc = hubspot_bcc_addresses([TO_EMAIL])
print(f"  BCC that will be applied : {resolved_bcc or '(NONE)'}")
if not resolved_bcc:
    sys.exit(
        f"{TO_EMAIL} is not an external address, or the BCC is not configured — "
        "the BCC only fires for recipients outside IFG. Nothing sent."
    )

# A pre-existing contact would make "HubSpot created this" unprovable.
existing = find_contact()
if existing:
    sys.exit(
        f"a contact already exists for {TO_EMAIL} (id={existing.get('id')}). "
        "This test proves BCC logging by watching HubSpot create one, so it needs "
        "a clean slate. Archive that contact first, or set BCC_TEST_TO to another "
        "outside address."
    )
print("  no existing contact for the recipient — clean baseline")

try:
    # ---------------------------------------------------------------- SEND
    banner("STEP 3a — send one real email with the HubSpot BCC applied")
    message_id, log_id = send_outbound_email(
        to_email=TO_EMAIL,
        subject=SUBJECT,
        body=BODY,
        approved_send=True,
        send_channel=CHANNEL,
        cc_emails=[CC_EMAIL] if CC_EMAIL else None,
    )
    print(f"  sent. message_id={message_id} composio_log_id={log_id}")
    print(f"  subject: {SUBJECT}")

    # ---------------------------------------------------------------- LOGGED?
    banner("STEP 3b — wait for HubSpot to log it")
    # BCC logging is asynchronous: HubSpot receives the copy, matches or creates
    # the contact, then attaches the email. Minutes, not seconds, is normal.
    contact = None
    for attempt in range(1, 25):
        contact = find_contact()
        if contact:
            print(f"  contact appeared after {attempt} poll(s) (~{attempt * 10}s)")
            break
        print(f"  no contact yet (poll {attempt}/24) — waiting 10s")
        time.sleep(10)

    if not contact:
        print("  ⚠️  no contact after ~4 minutes. Either BCC logging is not working, "
              "or HubSpot is slower than usual.")
        print("  The email WAS sent — check the recipient inbox. Re-check HubSpot by hand.")
    else:
        CONTACT_ID = str(contact.get("id"))
        print(f"  contact id={CONTACT_ID} email={contact.get('email')} "
              f"name={contact.get('name')!r} owner={contact.get('hubspot_owner_id')}")

        # ------------------------------------------------------------ VERIFY
        banner("STEP 3c — confirm the email is attached, with the right subject")
        found_subject = False
        for attempt in range(1, 13):
            try:
                assoc = execute_tool(
                    "HUBSPOT_LIST_OBJECT_ASSOCIATIONS",
                    {"objectType": "contacts", "objectId": CONTACT_ID,
                     "toObjectType": "emails", "limit": 20},
                    role="hubspot",
                )
                rows = (assoc.get("data") or {}).get("results") or []
                EMAIL_IDS[:] = [str(r.get("toObjectId") or r.get("id")) for r in rows
                                if (r.get("toObjectId") or r.get("id"))]
            except Exception as exc:
                print(f"  association read failed: {type(exc).__name__}: {exc}")
                rows = []

            if EMAIL_IDS:
                print(f"  attached email objects: {EMAIL_IDS}")
                for email_id in EMAIL_IDS:
                    try:
                        obj = execute_tool(
                            "HUBSPOT_READ_CRM_OBJECT_BY_ID",
                            {"objectType": "emails", "objectId": email_id,
                             "properties": ["hs_email_subject", "hs_email_text",
                                            "hs_email_direction", "hs_timestamp"]},
                            role="hubspot",
                        )
                        props = (obj.get("data") or {}).get("properties") or {}
                        subj = props.get("hs_email_subject") or ""
                        print(f"  email {email_id}: direction={props.get('hs_email_direction')} "
                              f"ts={props.get('hs_timestamp')}")
                        print(f"    subject: {subj!r}")
                        if STAMP in subj or STAMP in (props.get("hs_email_text") or ""):
                            found_subject = True
                            print("    ✅ this is our test email — BCC logging works end to end")
                    except Exception as exc:
                        print(f"    read failed: {type(exc).__name__}: {exc}")
                break
            print(f"  no email attached yet (poll {attempt}/12) — waiting 10s")
            time.sleep(10)

        if not found_subject:
            print("  ⚠️  the contact exists but our email was not confirmed on it. "
                  "Check the contact's timeline in HubSpot before concluding.")

finally:
    cleanup()
    banner("DONE")
    print("The lexi-hermes / lexi-api services never had the HubSpot BCC enabled.")
    print(f"One real email was sent to {TO_EMAIL} — subject {SUBJECT!r}.")
