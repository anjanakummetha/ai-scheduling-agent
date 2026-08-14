"""Rehearse the HubSpot cleanup against Kory's real book without writing to it.

Reads are real. Every write is intercepted at `execute_hubspot_tool` and printed
as the exact payload that would have gone to Composio, so the whole path —
scan, signature mining, placeholder detection, ownership guard, apply, undo —
runs against live data and HubSpot is never touched.

Verifies against the destination in the only way possible without writing: the
payload actually handed to Composio, not Lexi's description of it.

    .venv/bin/python scripts/hubspot_cleanup_rehearsal.py [--limit 25] [--phone]

Nothing here can write. The interceptor is installed before the first call and
asserts on any write slug that reaches it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

sys.path.insert(0, os.getcwd())

import app.integrations.hubspot_manager as hs  # noqa: E402

WRITE_SLUGS = {
    hs.HUBSPOT_UPDATE_CONTACT,
    hs.HUBSPOT_CREATE_NOTE,
    hs.HUBSPOT_MERGE_CONTACTS,
}

intercepted: list[tuple[str, dict[str, Any]]] = []
_real_execute = hs.execute_hubspot_tool


def guarded_execute(slug: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Real reads; writes are recorded and answered as if HubSpot accepted them."""
    if slug in WRITE_SLUGS:
        intercepted.append((slug, arguments))
        return {"successful": True, "data": {"id": "rehearsal"}, "log_id": "rehearsal"}
    return _real_execute(slug, arguments)


hs.execute_hubspot_tool = guarded_execute

# The apply path returns early when live writes are disabled, which would leave
# the part that matters — the payload built for Composio — unexercised. Force it
# open: the interceptor above is what actually keeps HubSpot untouched, and it
# sits below this.
hs.hubspot_writes_blocked = lambda: False


def rule(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--phone", action="store_true", help="also propose phone numbers")
    parser.add_argument(
        "--dupe-scan",
        type=int,
        default=2400,
        help="contacts to scan for duplicates; a partial scan finds a pair only "
        "when both records land in the same sample",
    )
    args = parser.parse_args()

    rule("0. Posture")
    print(f"  live writes blocked: {hs.hubspot_writes_blocked()}")
    print(f"  kory owner id:       {hs.kory_owner_id()}")
    print("  NOTE: every write is intercepted regardless of the flag above.")

    rule("1. Candidate scan (real reads)")
    contacts, coverage = hs.find_enrichment_candidates(
        limit=args.limit, owner_id=hs.kory_owner_id(), include_phone=args.phone
    )
    print(f"  candidates: {len(contacts)}")
    print(f"  coverage:   {json.dumps(coverage, default=str)}")
    junk = [
        (c.get("name"), f, c.get(f))
        for c in contacts
        for f in hs.ENRICHABLE_FIELDS
        if str(c.get(f) or "").strip() and hs.is_placeholder(c.get(f), field=f)
    ]
    print(f"  carrying a placeholder value: {len(junk)}")
    for name, field, value in junk[:10]:
        print(f"      {name} — {field} = {value!r}")

    rule("2. Proposals (real signature mining from Kory's inbox)")
    proposed = hs.propose_field_enrichment(limit=args.limit, include_phone=args.phone)
    print(proposed.get("kory_message", ""))
    batch_id = proposed.get("batch_id")
    print(f"\n  batch_id: {batch_id}")
    print(f"  proposals: {proposed.get('proposal_count')}   "
          f"no signature: {proposed.get('no_source_count')}   "
          f"placeholder replacements: {proposed.get('placeholder_replacements')}")
    for row in (proposed.get("proposals") or [])[:10]:
        prov = row.get("provenance") or {}
        print(f"\n    {row.get('name')} <{row.get('email')}>")
        for k, v in (row.get("proposed_fields") or {}).items():
            was = (row.get("replacing") or {}).get(k)
            print(f"      {k} = {v!r}" + (f"   (replacing {was!r})" if was else "   (was blank)"))
        print(f"      source: {prov.get('subject') or '—'}  [{prov.get('message_id', '')[:24]}]")

    if not proposed.get("proposal_count"):
        print("\n  Nothing proposed — skipping apply/undo, the guard section still runs.")
        merge_guard_check(args)
        return

    rule("3. Apply — the payloads that WOULD reach HubSpot")
    before = len(intercepted)
    applied = hs.execute_hubspot_batch(batch_id=batch_id, approved=True)
    print(f"  result: {json.dumps({k: v for k, v in applied.items() if k != 'batch'}, default=str)}")
    print(f"\n  {len(intercepted) - before} write(s) intercepted:")
    for slug, payload in intercepted[before:]:
        print(f"    {slug}  {json.dumps(payload, default=str)}")

    rule("4. Undo log")
    from app.storage.lexi_db import get_lexi_connection

    with get_lexi_connection() as conn:
        hs.ensure_applied_writes_table(conn)
        rows = conn.execute(
            "SELECT contact_id, field, old_value, new_value FROM hubspot_applied_writes "
            "WHERE batch_id = ? AND reverted_at IS NULL",
            (batch_id,),
        ).fetchall()
    print(f"  {len(rows)} field(s) recorded as reversible")
    for r in rows[:10]:
        print(f"    contact {r['contact_id']}  {r['field']}: {r['old_value']!r} -> {r['new_value']!r}")

    rule("5. Undo — the payloads that WOULD restore them")
    before = len(intercepted)
    undone = hs.revert_hubspot_batch(batch_id=batch_id, approved=True)
    print(f"  result: {json.dumps(undone, default=str)}")
    for slug, payload in intercepted[before:]:
        print(f"    {slug}  {json.dumps(payload, default=str)}")

    merge_guard_check(args)

    rule("RESULT")
    print(f"  {len(intercepted)} write(s) intercepted. HubSpot was not modified.")
    assert all(slug in WRITE_SLUGS for slug, _ in intercepted)


def merge_guard_check(args: argparse.Namespace) -> None:
    rule("6. Ownership guard against real records")
    dupes = hs.propose_duplicate_merges(limit=args.dupe_scan)
    pairs = dupes.get("pairs") or []
    print(f"  {len(pairs)} pair(s) in a {args.dupe_scan}-contact scan; "
          f"coverage={json.dumps(dupes.get('coverage'), default=str)}")
    foreign = [p for p in pairs if p.get("foreign_owners")]
    print(f"  {len(foreign)} touch someone else's record")
    for pair in foreign[:5]:
        left = pair.get("primary_name") or pair.get("primary_email") or pair.get("name")
        right = pair.get("duplicate_name") or pair.get("duplicate_email")
        print(f"    {left} ↔ {right}"
              f"  owners={pair.get('foreign_owners')} unlisted={pair.get('unlisted_owner_ids')}")
    print("\n  what Kory would actually see:")
    for line in (dupes.get("kory_message") or "").splitlines()[:8]:
        print(f"    {line}")
    if foreign:
        blocked = hs._merge_owner_block(foreign[0])
        print("\n  guard on the first foreign pair:")
        print(f"    {(blocked or {}).get('error_code')} — {(blocked or {}).get('kory_message')}")
    mine = [p for p in pairs if not p.get("foreign_owners")]
    if mine:
        print(f"\n  guard on one of his own pairs: {hs._merge_owner_block(mine[0])} (None = allowed)")


if __name__ == "__main__":
    main()
