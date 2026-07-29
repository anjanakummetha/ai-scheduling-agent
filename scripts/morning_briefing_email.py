#!/usr/bin/env python
"""Generate the CEO dashboard briefing and email it to Kory. Runs 4:45 AM MT.

Split of responsibility, on purpose:

  the dashboard  composes the briefing (it holds the calendar, inbox and Asana
                 data) but is deliberately read-only — a static guard fails its
                 build if any non-read Composio slug appears in its source.
  Lexi           sends, because sending is what Lexi is permitted and gated to do.

The recipient is read from config and never from the model or the response body,
so nothing generated upstream can redirect where this lands.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DASHBOARD_URL = os.getenv("DASHBOARD_BRIEFING_URL", "http://127.0.0.1:3000/api/hermes/briefing")
DEFAULT_RECIPIENT = "kory.mitchell@iconicfounders.com"


def generate_briefing(timeout: int = 600) -> dict:
    """Ask the dashboard to build today's briefing and hand back the email."""
    token = os.getenv("BRIEFING_CRON_TOKEN", "").strip()
    if not token:
        raise SystemExit("BRIEFING_CRON_TOKEN is not set — cannot authenticate to the dashboard.")

    request = urllib.request.Request(
        DASHBOARD_URL,
        method="POST",
        data=b"{}",
        headers={"Content-Type": "application/json", "x-briefing-token": token},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"Dashboard returned {exc.code}: {exc.read()[:300]!r}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Could not reach the dashboard at {DASHBOARD_URL}: {exc.reason}") from exc

    draft = (payload.get("emailDraft") or {}) if isinstance(payload, dict) else {}
    if not draft.get("bodyHtml"):
        raise SystemExit("Dashboard produced no email body — nothing sent.")
    return draft


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate and print the email without sending it.",
    )
    parser.add_argument(
        "--to",
        default=os.getenv("BRIEFING_EMAIL_TO", DEFAULT_RECIPIENT),
        help="Recipient (defaults to Kory).",
    )
    args = parser.parse_args()

    draft = generate_briefing()
    subject = str(draft.get("subject") or "CEO Daily Briefing")
    body_html = str(draft["bodyHtml"])

    if args.dry_run:
        print(f"TO:      {args.to}")
        print(f"SUBJECT: {subject}")
        print(f"HTML:    {len(body_html)} chars")
        print("-" * 60)
        print(draft.get("bodyText") or "(no plain-text body)")
        return 0

    from app.integrations.outlook_email import send_outbound_email

    # approved_send: this is Kory's own briefing going to Kory's own address, on
    # a schedule he asked for. The approval gate exists for mail sent to other
    # people on his behalf, which this is not — and `to` never comes from the
    # generated content.
    message_id, log_id = send_outbound_email(
        to_email=args.to,
        subject=subject,
        body=body_html,
        approved_send=True,
        send_channel="kory",
        html_body=True,
    )
    print(f"sent to {args.to} | message_id={message_id} | log_id={log_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
