"""Does Composio's OUTLOOK_SEND_EMAIL actually deliver bcc_emails?

Two HubSpot BCC tests logged nothing — once from Lexi's mailbox, once from
Kory's, who IS a HubSpot user. That rules out sender identity and puts the
question one layer down: is the BCC reaching the wire at all?

The parameter exists in the tool's schema, but this session already found a
tool whose schema and behaviour diverged (HUBSPOT_CREATE_NOTE), so the schema
is not evidence.

This sends ONE internal email from Lexi to Anjana's work address, BCC'd to
Lexi's own mailbox, then polls that mailbox for it. Nothing leaves IFG and no
CRM record is touched.

  arrives  -> bcc_emails works; the HubSpot side is what is broken
  does not -> Composio drops the BCC, and no amount of HubSpot config would help

    .venv/bin/python scripts/bcc_delivery_check.py
"""

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.integrations.composio_client import execute_tool  # noqa: E402

TO = "anjana.kummetha@iconicfounders.com"
STAMP = time.strftime("%Y%m%d-%H%M%S")
SUBJECT = f"BCC delivery check {STAMP}"


def banner(title):
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


LEXI = (settings.lexi_mailbox_email or "").strip().lower()
if not LEXI:
    sys.exit("LEXI_MAILBOX_EMAIL is not set — nothing to BCC and poll.")

banner("SEND — internal only, BCC'd to Lexi's own mailbox")
print(f"  from : {LEXI}")
print(f"  to   : {TO}")
print(f"  bcc  : {LEXI}   (the thing being tested)")
print(f"  subj : {SUBJECT}")

result = execute_tool(
    "OUTLOOK_SEND_EMAIL",
    {
        "user_id": "me",
        "to": TO,
        "subject": SUBJECT,
        "body": (
            "Internal test of whether the BCC field is delivered at all. "
            "No reply needed, nothing to action.\n\n"
            f"Reference: {STAMP}"
        ),
        "is_html": False,
        "save_to_sent_items": True,
        "bcc_emails": [LEXI],
    },
    role="lexi",
)
print("  send result:", json.dumps(result.get("data"), default=str)[:200])

banner("POLL — did the BCC copy land in Lexi's mailbox?")
# Delivery to a mailbox in the same tenant is usually seconds, but Graph's index
# can trail; give it a couple of minutes before calling it.
found = None
for attempt in range(1, 13):
    try:
        listing = execute_tool(
            "OUTLOOK_LIST_MESSAGES",
            {"user_id": "me", "top": 25,
             "select": ["id", "subject", "from", "toRecipients", "receivedDateTime"]},
            role="lexi",
        )
        data = listing.get("data") or {}
        rows = data.get("value") or data.get("messages") or []
        if isinstance(data, dict) and not rows:
            for key in ("response_data", "results", "items"):
                nested = data.get(key)
                if isinstance(nested, dict):
                    rows = nested.get("value") or []
                elif isinstance(nested, list):
                    rows = nested
                if rows:
                    break
        # OUTLOOK_LIST_MESSAGES ignores server-side filters here, so scope locally.
        found = next((m for m in rows if STAMP in str(m.get("subject") or "")), None)
    except Exception as exc:
        print(f"  list failed: {type(exc).__name__}: {exc}")
        rows = []

    if found:
        print(f"  ✅ BCC copy ARRIVED after {attempt} poll(s) (~{attempt * 10}s)")
        print("     id:", found.get("id"))
        print("     to:", json.dumps(found.get("toRecipients"), default=str)[:200])
        break
    print(f"  not yet (poll {attempt}/12, {len(rows)} recent messages scanned) — waiting 10s")
    time.sleep(10)

banner("VERDICT")
if found:
    print("  bcc_emails IS delivered. The send path is fine, so the HubSpot side")
    print("  is what is not working — the logging address, or the portal's email")
    print("  logging setup. Not a code bug in Lexi.")
else:
    print("  bcc_emails did NOT arrive. Composio's OUTLOOK_SEND_EMAIL accepts the")
    print("  parameter and appears to drop it — same class as the CREATE_NOTE bug.")
    print("  The HubSpot BCC feature cannot work until the send path really sends a BCC.")
    print(f"  Confirm by checking {TO} received the To: copy but Lexi's inbox has no BCC copy.")
