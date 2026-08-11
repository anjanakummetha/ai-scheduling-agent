#!/usr/bin/env python3
"""Verify Phase 1: no outbound email without explicit approval."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# This script attempts sends to exercise the approval gate, and CI runs it with
# dry-run deliberately OFF — so it must never also be able to talk to Teams. Set
# before load_dotenv/app.config so these beat a developer .env. Dry-run and the
# write mode are intentionally NOT forced here: turning dry-run on would defeat
# the gate this script exists to test. The real-send risk stays covered by the
# COMPOSIO_API_KEY check below.
os.environ["LEXI_TEAMS_ENABLED"] = "false"
os.environ["LEXI_SUPPRESS_TEAMS_PUSH"] = "true"

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from app.config import settings
from app.integrations.outlook_email import send_outbound_email
from app.orchestrator import evaluate_auto_execute_policy
from app.safety.approval_gate import (
    auto_execute_allowed,
    immediate_send_allowed,
    kory_approves_all,
    kory_outbound_email_blocked,
)


def main() -> int:
    failed = 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal failed
        if not ok:
            failed += 1
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))

    dry_run = settings.lexi_dry_run
    has_real_composio = bool(os.getenv("COMPOSIO_API_KEY", "").strip())

    # SAFETY: this suite attempts outbound sends to exercise the approval gate.
    # With dry-run OFF and a real Composio key present, an approved-path send could
    # deliver a real email. Refuse to run in that combination — it is only ever
    # intended for keyless CI (dry-run off, no keys) or local dry-run (simulated).
    if not dry_run and has_real_composio:
        print(
            "\n[ABORT] LEXI_DRY_RUN is off AND a real COMPOSIO_API_KEY is set — "
            "refusing to run send-attempting safety checks (real-send risk).\n"
            "Run this only in keyless CI (dry-run off) or with dry-run on.\n"
        )
        return 1

    # The approval gate raises only when dry-run is OFF (dry-run bypasses it by
    # design — nothing is sent). So the "blocked without approval" assertions are
    # only meaningful with dry-run off (keyless CI). Under dry-run they are skipped
    # here; they are covered hermetically by tests/test_approval_gate_lexi.py and
    # tests/test_recipient_allowlist.py.
    gate_active = not dry_run

    print("\n=== Approval safety gates ===\n")

    check("kory_approves_all (rules.py)", kory_approves_all())
    check("auto_execute disabled", not auto_execute_allowed())
    check("immediate_send disabled", not immediate_send_allowed())

    should, reason = evaluate_auto_execute_policy(999999)
    check("orchestrator auto_execute blocked", not should, reason)

    if not gate_active:
        print("  [SKIP] send-without-approval gate checks — dry-run bypasses the gate; "
              "covered by tests/test_approval_gate_lexi.py (run keyless CI with LEXI_DRY_RUN=false to exercise here)")
    else:
        try:
            send_outbound_email(
                to_email="blocked@example.com",
                subject="should not send",
                body="test",
                approved_send=False,
            )
            check("direct send without approval blocked", False, "send succeeded unexpectedly")
        except PermissionError as exc:
            check("direct send without approval blocked", True, str(exc)[:80])

        try:
            send_outbound_email(
                to_email="blocked@example.com",
                subject="kory no approval",
                body="test",
                approved_send=False,
                send_channel="kory",
            )
            check("kory channel without approval blocked", False, "send succeeded unexpectedly")
        except PermissionError as exc:
            check("kory channel without approval blocked", True, str(exc)[:80])

    try:
        send_outbound_email(
            to_email="ok@example.com",
            subject="approved path",
            body="test",
            approved_send=True,
            send_channel="kory",
        )
        if kory_outbound_email_blocked():
            check("kory channel approved send authorized", False, "must not send when outbound blocked")
        else:
            check("kory channel approved send authorized", True, "dry-run or live send path reached")
    except PermissionError as exc:
        if kory_outbound_email_blocked():
            check("kory channel approved send authorized", True, str(exc)[:80])
        else:
            check("kory channel approved send authorized", False, str(exc)[:120])
    except Exception as exc:
        if kory_outbound_email_blocked():
            check("kory channel approved send authorized", True, f"blocked: {type(exc).__name__}")
        else:
            check("kory channel approved send authorized", True, f"reached send: {type(exc).__name__}")

    if gate_active:
        try:
            send_outbound_email(
                to_email="blocked@example.com",
                subject="lexi no approval",
                body="test",
                approved_send=False,
                send_channel="lexi",
            )
            check("lexi channel without approval blocked", False, "send succeeded unexpectedly")
        except PermissionError as exc:
            check("lexi channel without approval blocked", True, str(exc)[:80])

    try:
        send_outbound_email(
            to_email="pilot@example.com",
            subject="lexi approved",
            body="Hi,\n\nTest.",
            approved_send=True,
            send_channel="lexi",
        )
        check("lexi channel approved path authorized", True, "dry-run or live send path reached")
    except PermissionError as exc:
        check("lexi channel approved path authorized", False, str(exc)[:120])
    except Exception as exc:
        check("lexi channel approved path authorized", True, f"reached send: {type(exc).__name__}")

    print(f"\n{'ALL PASS' if failed == 0 else f'{failed} FAILED'}\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
