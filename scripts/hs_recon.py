"""READ-ONLY recon for the HubSpot write test. Makes no writes of any kind."""

import json

from app.config import settings
from app.integrations import hubspot_manager as hs

print("kory_owner_id      :", hs.kory_owner_id())
print("writes_blocked     :", hs.hubspot_writes_blocked())
print("bcc_enabled        :", settings.hubspot_bcc_enabled)
print("bcc_address        :", settings.hubspot_bcc_address or "(unset)")
print()


def show(label, contacts):
    print(f"--- {label}: {len(contacts)} ---")
    for c in contacts:
        owner = c.get("hubspot_owner_id") or ""
        mine = "KORY" if owner == hs.kory_owner_id() else (hs.owner_name(owner) if owner else "unassigned")
        print(
            f"  id={c.get('id')} email={c.get('email')} name={c.get('name')!r} "
            f"owner={mine} status={c.get('hs_lead_status')!r} created={c.get('createdate')}"
        )
    if not contacts:
        print("  (none)")
    print()


for query in ("anjanakummetha@gmail.com", "anjana.kummetha@iconicfounders.com", "LEXI TEST"):
    try:
        found = hs.search_contacts(limit=10, query=query)
        show(f"query {query!r}", found.get("contacts") or [])
    except Exception as exc:
        print(f"--- query {query!r}: FAILED {type(exc).__name__}: {exc} ---\n")

# Exact-address filter, which is what the note path now requires.
try:
    found = hs.search_contacts(
        limit=10,
        filters=[{"propertyName": "email", "operator": "EQ", "value": "anjanakummetha@gmail.com"}],
    )
    show("EXACT email = anjanakummetha@gmail.com", found.get("contacts") or [])
except Exception as exc:
    print(f"exact-filter lookup FAILED: {type(exc).__name__}: {exc}")

print("recon complete — no writes issued")
