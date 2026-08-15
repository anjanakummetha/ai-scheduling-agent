"""HubSpot CRM — read/stage proposals; live writes blocked until explicit env flag."""

from __future__ import annotations

import json
import re
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import Any

from app.config import settings
from app.integrations.composio_client import ComposioNotConfiguredError, execute_hubspot_tool
from app.safety.approval_gate import assert_kory_approved_write

HUBSPOT_SEARCH_CONTACTS = "HUBSPOT_SEARCH_CONTACTS_BY_CRITERIA"
HUBSPOT_LIST_CONTACTS = "HUBSPOT_LIST_CONTACTS"
HUBSPOT_UPDATE_CONTACT = "HUBSPOT_UPDATE_CONTACT"
HUBSPOT_MERGE_CONTACTS = "HUBSPOT_MERGE_CONTACTS"
HUBSPOT_CREATE_NOTE = "HUBSPOT_CREATE_NOTE"
HUBSPOT_LIST_DEALS = "HUBSPOT_LIST_DEALS"
HUBSPOT_SEARCH_DEALS = "HUBSPOT_SEARCH_DEALS"
HUBSPOT_READ_PROPERTY = "HUBSPOT_READ_A_CRM_PROPERTY_BY_NAME"
HUBSPOT_LIST_PIPELINES = "HUBSPOT_RETRIEVE_ALL_PIPELINES_FOR_SPECIFIED_OBJECT_TYPE"
HUBSPOT_LIST_OWNERS = "HUBSPOT_RETRIEVE_OWNERS"

# HubSpot returns only a small default projection unless the caller names the
# fields it wants. Omitting this is what made every status field read as empty.
CONTACT_PROPERTIES = [
    "email",
    "firstname",
    "lastname",
    "company",
    "jobtitle",
    "phone",
    "hubspot_owner_id",
    "lifecyclestage",
    "hs_lead_status",
    "hs_analytics_source",
    "hs_linkedin_url",
    "notes_last_contacted",
    "lastmodifieddate",
    "createdate",
]
DEAL_PROPERTIES = [
    "dealname",
    "dealstage",
    "pipeline",
    "amount",
    "closedate",
    "createdate",
    "hs_lastmodifieddate",
]

# Exact hs_lead_status value IFG uses for opt-outs. Never email these.
DO_NOT_CONTACT = "do not contact"

# Placeholder junk that is technically "populated" but carries no information.
_PLACEHOLDER_VALUES = {"n/a", "na", "none", "unknown", "-", "--", ".", "?", "tbd", "null"}

# Values a LinkedIn/Sales Navigator sync writes when the member has hidden the
# field. They are not blank, so every NOT_HAS_PROPERTY scan skips them and every
# blank-only guard treats them as real data — which is why ~30 of Kory's
# contacts have never been offered for enrichment despite carrying no company at
# all. Measured live 2026-08-14: "Prefer No Connection to Company" appears 12
# times in a 400-contact sample of his book.
#
# Deliberately narrow. Marking a real value as a placeholder makes enrichment
# overwrite genuine data, which is far worse than leaving junk in place, so
# these match the whole field after normalisation — never a substring.
_PLACEHOLDER_PATTERNS = (
    r"prefer no connection to company",
    r"prefer not to (?:say|share|disclose|answer)",
    r"(?:no|not) (?:company|employer|title|available|provided|listed|specified|disclosed)",
    r"unknown company",
    r"n\.?/?a\.?",
    r"(?:none|null|nil|tbd|tba|undefined|blank|empty|test)",
    r"[-–—_.*#]+",
    r"x{3,}",
    r"\?+",
)
_PLACEHOLDER_RE = re.compile(r"^(?:%s)$" % "|".join(_PLACEHOLDER_PATTERNS), re.I)

_MAX_PAGE = 100


def hubspot_configured() -> bool:
    return bool(settings.hubspot_composio_connection_id and settings.composio_api_key)


def _truthy(raw: Any) -> bool:
    """HubSpot returns booleans as the STRINGS "true"/"false" — and "false" is truthy."""
    return str(raw).strip().lower() in {"true", "1", "yes"}


# Short but legitimate. Two-letter job titles are normal, and so are two-letter
# company names — WM, GE, 3M, EY, BP. An earlier version of this file asserted
# the opposite in a comment; Kory has a contact at WM, whose company field was
# being offered for overwrite as though it were junk.
_REAL_SHORT_TITLES = {"vp", "pm", "gm", "md", "cf", "ce", "hr", "it", "pa", "ea"}


def is_placeholder(value: Any, *, field: str = "") -> bool:
    """Is this field populated with something that carries no information?

    Matches the whole value, never a substring: "No Limits Consulting" is a real
    company and "Not Available" is not, and only anchoring tells them apart.
    """
    text = str(value or "").strip()
    if not text:
        return False
    if text.lower() in _PLACEHOLDER_VALUES:
        return True
    # Normalise punctuation and spacing before matching so "N / A", "n.a." and
    # "Prefer No Connection To Company." all reduce to the same shape.
    normalized = re.sub(r"\s+", " ", re.sub(r"[^\w\s/?.*#—–-]", " ", text)).strip(" .,")
    if _PLACEHOLDER_RE.match(normalized):
        return True
    if len(text) <= 1:
        return True
    if len(text) == 2:
        if field == "jobtitle":
            return text.lower() not in _REAL_SHORT_TITLES
        # Letters or digits are a plausible short company name; anything else at
        # this length is punctuation, and the patterns above already caught the
        # ones that spell something.
        return not text.isalnum()
    return False


def kory_owner_id() -> str:
    return (settings.hubspot_kory_owner_id or "").strip()


# --- cached reference data -------------------------------------------------
# Pipelines, stage labels and owners change rarely; fetch once per process so
# every report can speak in names instead of opaque numeric ids.

_pipeline_cache: dict[str, dict[str, Any]] | None = None
_lifecycle_cache: dict[str, str] | None = None
_owner_cache: dict[str, str] | None = None


def deal_stage_map(*, refresh: bool = False) -> dict[str, dict[str, Any]]:
    """Deal stage id -> {pipeline, stage, is_closed}."""
    global _pipeline_cache
    if _pipeline_cache is not None and not refresh:
        return _pipeline_cache
    out: dict[str, dict[str, Any]] = {}
    try:
        result = execute_hubspot_tool(HUBSPOT_LIST_PIPELINES, {"objectType": "deals"})
        for pipeline in (result.get("data") or {}).get("results") or []:
            pipeline_label = pipeline.get("label") or ""
            for stage in pipeline.get("stages") or []:
                metadata = stage.get("metadata") or {}
                out[str(stage.get("id"))] = {
                    "pipeline": pipeline_label,
                    "stage": stage.get("label") or "",
                    # Trust the pipeline's own closed flag, then fall back to
                    # the pipeline name for the Closed/Won + Closed/Lost boards.
                    "is_closed": _truthy(metadata.get("isClosed"))
                    or pipeline_label in {"Closed/Won", "Closed/Lost"},
                }
    except Exception:
        out = {}
    _pipeline_cache = out
    return out


def lifecycle_label_map(*, refresh: bool = False) -> dict[str, str]:
    """Lifecycle stage id -> label. IFG uses a custom pipeline with numeric ids."""
    global _lifecycle_cache
    if _lifecycle_cache is not None and not refresh:
        return _lifecycle_cache
    out: dict[str, str] = {}
    try:
        result = execute_hubspot_tool(
            HUBSPOT_READ_PROPERTY,
            {"objectType": "contacts", "propertyName": "lifecyclestage"},
        )
        for option in (result.get("data") or {}).get("options") or []:
            out[str(option.get("value"))] = option.get("label") or ""
    except Exception:
        out = {}
    _lifecycle_cache = out
    return out


def owner_map(*, refresh: bool = False) -> dict[str, str]:
    """Owner id -> display name, for both active and archived owners."""
    global _owner_cache
    if _owner_cache is not None and not refresh:
        return _owner_cache
    out: dict[str, str] = {}
    for archived in (False, True):
        try:
            result = execute_hubspot_tool(
                HUBSPOT_LIST_OWNERS, {"limit": 100, "archived": archived}
            )
            for owner in (result.get("data") or {}).get("results") or []:
                name = f"{owner.get('firstName') or ''} {owner.get('lastName') or ''}".strip()
                out[str(owner.get("id"))] = name or owner.get("email") or str(owner.get("id"))
        except Exception:
            continue
    if not out:
        # Never cache a failed lookup. The cache has no TTL, so one transient
        # error at the wrong moment would make every owner unresolvable for the
        # life of the process — and "I can't tell whose record this is" now
        # changes what Lexi says to Kory about every contact he doesn't own.
        return {}
    _owner_cache = out
    return out


def owner_is_known(owner_id: Any) -> bool:
    """Can this owner id be resolved to a person?

    `owner_map` asks for archived owners too, but Composio ignores `archived` on
    HUBSPOT_RETRIEVE_OWNERS (verified live: `True` and `"true"` both return the
    same 8 active owners), so a deactivated user's id resolves to nothing. That
    is not hypothetical — one such id owns 6 of Kory's 27 duplicate pairs.

    Callers need this separately from `owner_name` because "owned by Natalie"
    and "owned by an id nobody can resolve" call for different advice: the first
    is a person he can ask, the second is a record he has to open HubSpot to
    identify.
    """
    key = str(owner_id or "").strip()
    return bool(key) and key in owner_map()


def owner_name(owner_id: Any) -> str:
    """Human name for an owner id, or an explicit 'unassigned/unlisted' marker.

    The fallback names the id rather than calling it "unknown owner": Kory's
    only route to identifying it is to look the id up in HubSpot, so the id is
    the actionable part of the message.
    """
    key = str(owner_id or "").strip()
    if not key:
        return "unassigned"
    return owner_map().get(key) or f"an unlisted owner (id {key})"


def lifecycle_label(contact: dict[str, Any]) -> str:
    raw = str(contact.get("lifecyclestage") or "").strip()
    if not raw:
        return "unknown"
    return lifecycle_label_map().get(raw) or raw


def is_do_not_contact(contact: dict[str, Any]) -> bool:
    return str(contact.get("hs_lead_status") or "").strip().lower() == DO_NOT_CONTACT


def kory_owns(contact: dict[str, Any]) -> bool:
    return str(contact.get("hubspot_owner_id") or "").strip() == kory_owner_id()


def hubspot_writes_blocked() -> bool:
    return settings.lexi_dry_run or not settings.hubspot_live_writes_enabled


def hubspot_status_brief() -> dict[str, Any]:
    if not hubspot_configured():
        return {
            "ok": False,
            "kory_message": (
                "**HubSpot:** not connected in Lexi yet. "
                "Set `HUBSPOT_COMPOSIO_CONNECTION_ID=ca_jdY18Wb0L46M` "
                "(reads ok; writes stay blocked until you enable live HubSpot writes)."
            ),
        }
    try:
        sample = search_contacts(limit=5)
        total = count_contacts()
        kory_total = count_contacts([_owner_filter(kory_owner_id())])
        dnc = count_contacts(
            [
                _owner_filter(kory_owner_id()),
                {"propertyName": "hs_lead_status", "operator": "EQ", "value": "Do Not Contact"},
            ]
        )
        write_note = (
            "Live writes **blocked**."
            if hubspot_writes_blocked()
            else "Live writes **enabled**."
        )
        lines = [
            "**HubSpot:** connected.",
            f"{total} contacts across IFG · **{kory_total} owned by you**",
        ]
        if dnc:
            lines.append(f"{dnc} of yours are marked **Do Not Contact** and are excluded from outreach.")
        lines.append(f"\n{write_note} Reads span all of IFG; writes are scoped to your contacts.")
        return {
            "ok": True,
            "kory_message": "\n".join(lines),
            "total_contacts": total,
            "kory_contacts": kory_total,
            "do_not_contact": dnc,
            "sample": sample.get("contacts", [])[:5],
            "writes_blocked": hubspot_writes_blocked(),
        }
    except Exception as exc:
        return {
            "ok": False,
            "kory_message": f"**HubSpot:** read failed ({type(exc).__name__}).",
            "error": str(exc),
        }


def _owner_filter(owner_id: str) -> dict[str, Any]:
    return {"propertyName": "hubspot_owner_id", "operator": "EQ", "value": str(owner_id)}


def count_contacts(filters: list[dict[str, Any]] | None = None) -> int | None:
    """Exact portal-wide count for a filter set, so reports never guess at scope."""
    if not hubspot_configured():
        return None
    try:
        result = execute_hubspot_tool(
            HUBSPOT_SEARCH_CONTACTS,
            {
                "limit": 1,
                "properties": ["email"],
                "filterGroups": [
                    {
                        "filters": filters
                        or [{"propertyName": "hs_object_id", "operator": "HAS_PROPERTY"}]
                    }
                ],
            },
        )
        total = (result.get("data") or {}).get("total")
        return int(total) if total is not None else None
    except Exception:
        return None


def search_contacts(
    *,
    limit: int = 25,
    query: str = "",
    owner_id: str | None = None,
    filters: list[dict[str, Any]] | None = None,
    properties: list[str] | None = None,
) -> dict[str, Any]:
    """Read contacts with the fields actually named, paging until `limit` is met.

    Callers get `total` (the portal-wide match count) alongside `count` (what was
    read) so no report can imply it saw more than it did.
    """
    if not hubspot_configured():
        raise ComposioNotConfiguredError("HUBSPOT_COMPOSIO_CONNECTION_ID is missing.")

    wanted = properties or CONTACT_PROPERTIES
    active_filters = list(filters or [])
    if owner_id:
        active_filters.append(_owner_filter(owner_id))

    use_search = bool(query.strip() or active_filters)
    contacts: list[dict[str, Any]] = []
    after: str | None = None
    log_id = None
    dry_run = False
    total: int | None = None

    while len(contacts) < limit:
        page = min(_MAX_PAGE, limit - len(contacts))
        arguments: dict[str, Any] = {"limit": page, "properties": wanted}
        if after:
            arguments["after"] = after
        if use_search:
            if query.strip():
                arguments["query"] = query.strip()
            if active_filters:
                arguments["filterGroups"] = [{"filters": active_filters}]
            tool = HUBSPOT_SEARCH_CONTACTS
        else:
            tool = HUBSPOT_LIST_CONTACTS

        # No silent fallback: a failed search used to fall back to an unfiltered
        # list, so a lookup that errored returned arbitrary contacts as matches.
        result = execute_hubspot_tool(tool, arguments)
        data = result.get("data")
        log_id = result.get("log_id") or log_id
        dry_run = bool(result.get("dry_run")) or dry_run

        batch = _normalize_contacts(data)
        contacts.extend(batch)
        if isinstance(data, dict):
            if total is None and data.get("total") is not None:
                total = int(data["total"])
            after = ((data.get("paging") or {}).get("next") or {}).get("after")
        else:
            after = None
        if not after or not batch:
            break

    return {
        "ok": True,
        "count": len(contacts),
        # None means "HubSpot didn't tell us". Falling back to len(contacts) made
        # an unknown total indistinguishable from a known one, so `scanned >=
        # total` was trivially true and a capped scan reported full coverage.
        # Callers must treat None as unknown — count_contacts() is the exact count.
        "total": total,
        "truncated": bool(total is not None and total > len(contacts)),
        "contacts": contacts,
        "composio_log_id": log_id,
        "dry_run": dry_run,
    }


# HubSpot's built-in association type linking a note to a contact.
NOTE_TO_CONTACT_ASSOCIATION_TYPE_ID = 202


def note_payload(*, contact_id: str, body: str) -> dict[str, Any]:
    """Build the arguments for HUBSPOT_CREATE_NOTE.

    The tool's schema is flat and uses HubSpot's own field names: the text goes in
    `hs_note_body`, `hs_timestamp` is REQUIRED, and the link to the contact is an
    `associations` entry — there is no `contactId` parameter at all.

    This previously sent {"contactId": ..., "body": ...}. Neither key exists in
    the schema, so both were dropped and the required timestamp was never sent:
    every meeting note was either rejected outright or filed as an empty note
    attached to nobody. Nothing in the dry-run harness could catch it, because
    stubbing execute_hubspot_tool captures what we MEANT to send and never
    compares it against what the tool actually accepts.
    """
    return {
        "hs_note_body": body,
        "hs_timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "hubspot_owner_id": kory_owner_id(),
        "associations": [
            {
                "to": {"id": str(contact_id)},
                "types": [
                    {
                        "associationCategory": "HUBSPOT_DEFINED",
                        "associationTypeId": NOTE_TO_CONTACT_ASSOCIATION_TYPE_ID,
                    }
                ],
            }
        ],
    }


def assert_contact_writable(
    contact: dict[str, Any], *, owner_ack: bool = False
) -> dict[str, Any] | None:
    """Guard writes to a contact Kory does not own.

    Mirrors the Asana ownership guard: reads span the whole company, but
    changing a colleague's record takes an explicit acknowledgement that names
    the owner, so it can never happen by accident.
    """
    if kory_owns(contact) or owner_ack:
        return None
    owner_id = str(contact.get("hubspot_owner_id") or "").strip()
    if not owner_id:
        # Unassigned records belong to nobody, so there is no one to confirm with.
        return None
    who = owner_name(owner_id)
    known = owner_is_known(owner_id)
    label = contact.get("name") or contact.get("email") or "This contact"
    if known:
        message = (
            f"'{label}' is owned by {who}, not you. "
            "Confirm you want to change someone else's contact."
        )
    else:
        # Saying "owned by unknown owner (159291600)" reads like a name and
        # tells him nothing he can act on. Say plainly that the id doesn't
        # resolve, and point at the one place that can answer it.
        message = (
            f"'{label}' is owned by someone else — owner id {owner_id} isn't in "
            "HubSpot's owner list, most likely a deactivated user, so I can't tell "
            "you whose it is. Look it up in HubSpot, or confirm you want to change "
            "it anyway."
        )
    return {
        "ok": False,
        "error_code": "owner_confirmation_required",
        "error": message,
        "owner": who,
        "owner_id": owner_id,
        "owner_unknown": not known,
        "contact_id": contact.get("id"),
    }


def _owner_annotation(*contacts: dict[str, Any]) -> dict[str, Any]:
    """Whose records a proposed pair touches.

    Duplicate scanning is portal-wide on purpose: a duplicate of Kory's contact
    is very often the copy a colleague filed. But the proposal has to say so.
    He approves merges one at a time off this list, and "Angelo ↔ Angelo" reads
    identical whether both records are his or one is Heidi's — and a HubSpot
    merge is permanent.
    """
    foreign_ids = {
        str(c.get("hubspot_owner_id") or "").strip()
        for c in contacts
        if str(c.get("hubspot_owner_id") or "").strip() and not kory_owns(c)
    }
    foreign = sorted(owner_name(oid) for oid in foreign_ids)
    return {
        "foreign_owners": foreign,
        # Kept apart from the names so the proposal can flag the pairs whose
        # owner cannot be resolved at all — those need a look in HubSpot, not a
        # word with a colleague.
        "unlisted_owner_ids": sorted(oid for oid in foreign_ids if not owner_is_known(oid)),
        "kory_owns_all": not foreign,
    }


def _merge_owner_block(
    row: dict[str, Any], *, owner_ack: bool = False
) -> dict[str, Any] | None:
    """Refuse a merge that would consume a record Kory does not own.

    Read live rather than trusting the staged row: a batch can sit for days, and
    the staged copy is whatever the scan happened to see.

    This guard fails CLOSED, unlike the Asana owner check, which lets a read
    failure through so an Asana outage cannot lock Kory out of his own tasks.
    The trade is different here — an Asana mistake is reopened in one call, and
    a HubSpot merge is not undone at all. If ownership cannot be established,
    the merge does not happen.
    """
    if owner_ack:
        return None
    ids = [
        str(row.get("primary_id") or "").strip(),
        str(row.get("duplicate_id") or "").strip(),
    ]
    ids = [i for i in ids if i]
    live = contacts_by_ids(ids)
    if len(live) < len(ids):
        return {
            "ok": False,
            "error_code": "owner_check_unavailable",
            "error": (
                "Could not read both contacts to check who owns them — "
                "not merging. A HubSpot merge cannot be undone."
            ),
            "kory_message": (
                "I couldn't confirm who owns both of these records, so I haven't "
                "merged them. Merges are permanent — worth a look in HubSpot first."
            ),
        }
    for contact in live:
        blocked = assert_contact_writable(contact)
        if blocked:
            label = contact.get("name") or contact.get("email") or "one of these records"
            whose = (
                f"owner id {blocked['owner_id']}, which isn't in HubSpot's owner list "
                "(most likely a deactivated user)"
                if blocked.get("owner_unknown")
                else blocked["owner"]
            )
            return {
                **blocked,
                "kory_message": (
                    f"**{label}** is owned by {whose}, not you — and a "
                    "HubSpot merge is permanent, so I've stopped.\n\n"
                    "Say you want it merged anyway and I'll go ahead."
                ),
            }
    return None


def contacts_by_ids(contact_ids: list[str]) -> list[dict[str, Any]]:
    """Fetch full records for specific contact ids (so opt-out status is known)."""
    ids = [str(cid).strip() for cid in contact_ids if str(cid).strip()]
    if not ids:
        return []
    found = search_contacts(
        limit=len(ids),
        filters=[{"propertyName": "hs_object_id", "operator": "IN", "values": ids}],
    )
    return found.get("contacts") or []


def propose_inactive_cleanup(*, inactive_days: int = 180, limit: int = 50) -> dict[str, Any]:
    """CRM health report — read-only. Nothing is proposed for archiving.

    The previous version read 50 of 2,000+ contacts with every status field
    blank, so its guards never matched and it recommended archiving the whole
    sample, active customers included. Kory's book is ~95% populated and
    actively maintained: the useful output here is a view, not a mutation.
    """
    return crm_health_report(owner_id=kory_owner_id(), inactive_days=inactive_days)


def crm_health_report(
    *,
    owner_id: str | None = None,
    inactive_days: int = 180,
    sample: int = 400,
) -> dict[str, Any]:
    """Where the book is thin, counted portal-wide rather than from a sample."""
    if not hubspot_configured():
        return {"ok": False, "kory_message": "**HubSpot:** not connected."}

    scope_owner = owner_id if owner_id is not None else kory_owner_id()
    base = [_owner_filter(scope_owner)] if scope_owner else []
    whose = "Kory's contacts" if scope_owner == kory_owner_id() else (
        owner_name(scope_owner) + "'s contacts" if scope_owner else "all IFG contacts"
    )

    def missing(prop: str) -> int | None:
        return count_contacts(base + [{"propertyName": prop, "operator": "NOT_HAS_PROPERTY"}])

    total = count_contacts(base or None)
    no_title = missing("jobtitle")
    no_company = missing("company")
    no_phone = missing("phone")
    never = missing("notes_last_contacted")
    dnc = count_contacts(
        base + [{"propertyName": "hs_lead_status", "operator": "EQ", "value": "Do Not Contact"}]
    )
    # The set actually worth fixing: a blank title on someone he has emailed.
    fixable = count_contacts(
        base
        + [
            {"propertyName": "jobtitle", "operator": "NOT_HAS_PROPERTY"},
            {"propertyName": "notes_last_contacted", "operator": "HAS_PROPERTY"},
        ]
    )

    # Placeholder junk needs values, not counts, so scan a bounded sample.
    placeholders: list[dict[str, Any]] = []
    try:
        scan = search_contacts(limit=sample, owner_id=scope_owner or None)
        for contact in scan.get("contacts") or []:
            for field in ("company", "jobtitle"):
                if is_placeholder(contact.get(field), field=field):
                    placeholders.append(
                        {
                            "id": contact.get("id"),
                            "name": contact.get("name") or contact.get("email"),
                            "field": field,
                            "value": contact.get(field),
                        }
                    )
        scanned = scan.get("count") or 0
    except Exception:
        scanned = 0

    def pct(part: int | None) -> str:
        if not part or not total:
            return ""
        return f" ({part / total:.0%})"

    lines = [f"**CRM health — {whose}**\n", f"Total: **{total}** contacts\n"]
    lines.append(f"• Missing job title: {no_title}{pct(no_title)}")
    lines.append(f"• Missing company: {no_company}{pct(no_company)}")
    lines.append(f"• Missing phone: {no_phone}{pct(no_phone)}")
    lines.append(f"• Never contacted: {never}{pct(never)}")
    lines.append(f"• Marked Do Not Contact: {dnc}{pct(dnc)}")
    lines.append(
        f"\n**Worth fixing: {fixable}** — missing a title but you've actually corresponded with them."
    )
    if placeholders:
        lines.append(
            f"\n**Placeholder values: {len(placeholders)}** found in {scanned} scanned "
            f"(e.g. {', '.join(sorted({str(p['value']) for p in placeholders})[:4])})"
        )
    lines.append("\n_Read-only. Nothing in HubSpot was changed._")
    return {
        "ok": True,
        "owner_scope": scope_owner,
        "total": total,
        "missing_jobtitle": no_title,
        "missing_company": no_company,
        "missing_phone": no_phone,
        "never_contacted": never,
        "do_not_contact": dnc,
        "worth_fixing": fixable,
        "placeholders": placeholders,
        "scanned": scanned,
        "writes_blocked": hubspot_writes_blocked(),
        "kory_message": "\n".join(lines),
    }


# A duplicate is only detectable when BOTH records are in the sample, so a
# partial scan misses almost everything: sampling 50 of ~1,000 finds a given
# pair roughly 0.2% of the time, then reports "no duplicates". Scan the book.
# Portal held 2,207 contacts when this was set, and a 2,000 cap read as PARTIAL.
# 50 pages at _MAX_PAGE=100 leaves room to grow; ~23 calls at today's size,
# against a 200k monthly budget sitting near 12%.
DUPLICATE_SCAN_LIMIT = 5000


def propose_duplicate_merges(*, limit: int = DUPLICATE_SCAN_LIMIT) -> dict[str, Any]:
    """Find likely duplicate contacts by email/name — stage merge proposals only."""
    raw = search_contacts(limit=limit)
    contacts = raw.get("contacts") or []
    by_email: dict[str, list[dict[str, Any]]] = {}
    by_name: dict[str, list[dict[str, Any]]] = {}
    for contact in contacts:
        email = (contact.get("email") or "").strip().lower()
        name = re.sub(r"\s+", " ", (contact.get("name") or "").strip().lower())
        if email:
            by_email.setdefault(email, []).append(contact)
        if name and " " in name:
            by_name.setdefault(name, []).append(contact)

    pairs: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for email, group in by_email.items():
        if len(group) < 2:
            continue
        primary, *dupes = group
        for dupe in dupes:
            key = f"{primary.get('id')}:{dupe.get('id')}"
            if key in seen_ids:
                continue
            seen_ids.add(key)
            pairs.append(
                {
                    "reason": "same_email",
                    "email": email,
                    "primary_id": primary.get("id"),
                    "primary_name": primary.get("name"),
                    "duplicate_id": dupe.get("id"),
                    "duplicate_name": dupe.get("name"),
                    "suggested_action": "merge",
                    **_owner_annotation(primary, dupe),
                }
            )
    for name, group in by_name.items():
        if len(group) < 2:
            continue
        emails = {(c.get("email") or "").lower() for c in group if c.get("email")}
        if len(emails) <= 1:
            continue
        primary, *dupes = group
        for dupe in dupes:
            key = f"{primary.get('id')}:{dupe.get('id')}"
            if key in seen_ids:
                continue
            seen_ids.add(key)
            pairs.append(
                {
                    "reason": "same_name_different_email",
                    "name": name,
                    "primary_id": primary.get("id"),
                    "primary_email": primary.get("email"),
                    "duplicate_id": dupe.get("id"),
                    "duplicate_email": dupe.get("email"),
                    "suggested_action": "review_or_merge",
                    **_owner_annotation(primary, dupe),
                }
            )

    batch_id = _stage_hubspot_batch(
        batch_type="duplicate_merge",
        payload={"pairs": pairs},
    )
    scanned = raw.get("count") or len(contacts)
    # Not raw["total"]: search_contacts falls back to len(contacts) when HubSpot
    # returns no total, so an UNKNOWN total is indistinguishable from a known one
    # and "scanned >= total" is trivially true. That reported a capped scan as
    # complete. count_contacts is an exact portal-wide count (one extra call).
    portal = count_contacts()
    # Unknown coverage is not complete. Never claim the book is clean on a guess.
    complete = portal is not None and scanned >= portal
    scope = f"{scanned} contact(s)" + (f" of {portal}" if portal and portal > scanned else "")
    coverage_line = (
        f"scanned all {scanned} contact(s)"
        if complete
        else (
            f"scanned {scanned} of {portal} contact(s) — PARTIAL"
            if portal is not None
            else f"scanned {scanned} contact(s); portal total UNKNOWN — coverage unverified"
        )
    )
    lines = [f"**HubSpot duplicate proposals** ({len(pairs)}) — {coverage_line}\n"]
    for row in pairs[:12]:
        # Say whose records these are. He approves merges one at a time off this
        # list, and it is the only place he sees the pair before it is permanent.
        whose = row.get("foreign_owners") or []
        owner_note = f" — ⚠️ owned by {', '.join(whose)}, not you" if whose else ""
        # Lead with the person. A same-name pair carries the name once and the
        # two addresses separately, and rendering only the addresses made the
        # list read as twenty-seven strangers — he approves merges off this.
        left = row.get("primary_name") or row.get("primary_email") or ""
        right = row.get("duplicate_name") or row.get("duplicate_email") or ""
        shared = str(row.get("name") or "").strip()
        if shared and left != right:
            who = f"**{shared.title()}** ({left} ↔ {right})"
        else:
            who = f"{left} ↔ {right}"
        lines.append(f"• {who} — **{row['suggested_action']}** ({row['reason']}){owner_note}")
    if not pairs:
        lines.append(
            f"_No duplicates found in the full book ({scanned} contact(s) scanned)._"
            if complete
            else (
                f"_No duplicates among the {scanned} checked"
                + (f" of {portal}" if portal is not None else "; portal total unknown")
                + ". This is a PARTIAL scan — it is not evidence the book is clean._"
            )
        )
    # An id that resolves to nobody is the one case he cannot settle by asking a
    # colleague, and it is common enough here to warrant its own line: a single
    # deactivated owner accounts for 6 of his duplicate pairs.
    unlisted = sorted({oid for row in pairs for oid in (row.get("unlisted_owner_ids") or [])})
    if unlisted:
        affected = sum(1 for row in pairs if row.get("unlisted_owner_ids"))
        lines.append(
            f"\n_{affected} pair(s) belong to owner id(s) {', '.join(unlisted)}, which aren't "
            "in HubSpot's owner list — most likely a deactivated user. I can't resolve them to "
            "a name, so check those in HubSpot before merging._"
        )
    lines.append(
        "\n_Merges are staged only — no HubSpot changes until live writes + approval. "
        "HubSpot merges cannot be undone, so each one needs its own approval._"
    )
    return {
        "ok": True,
        "batch_id": batch_id,
        "pair_count": len(pairs),
        "pairs": pairs,
        "unlisted_owner_ids": unlisted,
        # Structured, so coverage survives being summarised. The prose version of
        # this got dropped in paraphrase once and "checked 50 of 1016" reached
        # Kory as "your book looks clean".
        "coverage": {
            "scanned": scanned,
            "portal_total": portal,
            "complete": complete,
        },
        "writes_blocked": hubspot_writes_blocked(),
        "kory_message": "\n".join(lines),
    }


_TITLE_HINTS = (
    "ceo", "cfo", "coo", "cto", "cmo", "president", "vp ", "vice president",
    "director", "manager", "partner", "principal", "founder", "owner",
    "head of", "chief", "advisor", "adviser", "consultant", "analyst",
    "associate", "controller", "supervisor", "lead ", "specialist",
)


# A signature phone: 10+ digits, optional country code, the usual separators.
# Anchored on a word boundary so it cannot pick up the tail of a longer number.
_PHONE_RE = re.compile(
    r"(?<![\d-])(\+?\d{1,3}[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}(?![\d-])"
)
# Lines whose number is not the person's own reachable line.
_PHONE_EXCLUDE = ("fax", "f:", "efax", "toll", "support", "office hours", "suite", "zip")
# Toll-free prefixes. A company switchboard is never a person's contact number,
# and writing one onto a contact record is worse than leaving the field blank —
# it looks like a direct line. Caught by the live rehearsal: a signature offered
# (800) 962-0418 as its owner's phone.
_TOLL_FREE = {"800", "833", "844", "855", "866", "877", "888"}
# Signature labels that mark a direct or mobile line — preferred over a
# switchboard when a block lists several.
_PHONE_PREFERRED = ("mobile", "cell", "m:", "c:", "direct", "d:")


_QUOTE_MARKERS = (
    "-----original message-----",
    "________________________________",
    "from:",
    "sent from my",
    "begin forwarded message",
)


def _strip_quoted_history(text: str) -> str:
    """Keep only the newest part of a message.

    Everything below a quote marker is somebody else's writing — usually Kory's
    own signature bouncing back in a reply chain.
    """
    lines = text.splitlines()
    kept: list[str] = []
    for line in lines:
        low = line.strip().lower()
        if low.startswith(">"):
            break
        if any(low.startswith(marker) for marker in _QUOTE_MARKERS):
            break
        if re.match(r"^on .{5,80}\bwrote:\s*$", low):
            break
        kept.append(line)
    return "\n".join(kept)


def _clean_body(body: str) -> str:
    text = re.sub(r"(?is)<(script|style|blockquote).*?</\1>", "\n", body or "")
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    text = text.replace(" ", " ")
    return text


def _extract_signature_fields(
    body: str, *, known_company: str = "", contact_name: str = ""
) -> dict[str, str]:
    """Pull a job title (and company) out of a sender's own signature block.

    Deliberately strict. A wrong title written into the CRM is worse than a
    blank one, so a candidate is only accepted when it sits within a few lines
    of the contact's own name — otherwise a reply chain yields the *recipient's*
    signature, which is how this first read "Kory Mitchell - CEO" onto someone
    else's record.
    """
    fields: dict[str, str] = {}
    text = _clean_body(_strip_quoted_history(_clean_body(body or "")))
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln and len(ln) <= 90]

    name_parts = [p.lower() for p in re.split(r"\s+", contact_name.strip()) if p]

    def _is_name_line(line: str) -> bool:
        """True when the line is the person's own name, not merely mentioning it."""
        if not name_parts:
            return False
        stripped = re.sub(r"[^a-z ]", " ", line.lower())
        words = [w for w in stripped.split() if w]
        if not words or len(words) > 5:
            return False
        return all(part in words for part in name_parts)

    name_at = -99
    title_at = -99
    for index, line in enumerate(lines):
        low = line.lower()
        if _is_name_line(line):
            name_at = index
            continue
        if low.startswith(("from:", "sent:", "to:", "subject:", "http", ">")):
            continue
        if "@" in line or "unsubscribe" in low:
            continue
        # When the contact's name is known, the title must sit just beneath it —
        # otherwise a reply chain hands back the *recipient's* signature.
        if name_parts and not (0 < index - name_at <= 3):
            continue
        # "Title at Company" / "Title | Company"
        split = re.split(r"\s+(?:at|@)\s+|\s*[|]\s*", line, maxsplit=1)
        candidate = split[0].strip(" ,-–—")
        if not candidate or len(candidate) < 3 or len(candidate) > 60:
            continue
        if not any(hint in candidate.lower() for hint in _TITLE_HINTS):
            continue
        fields["jobtitle"] = candidate
        if len(split) > 1 and not known_company:
            company = split[1].strip(" ,-–—")
            if 2 < len(company) <= 60:
                fields["company"] = company
        title_at = index
        break

    phone = _phone_from_signature(lines, start_at=max(name_at, title_at))
    if phone:
        fields["phone"] = phone
    return fields


def _phone_from_signature(lines: list[str], *, start_at: int) -> str:
    """The contact's own phone number out of their signature block.

    Phone is the biggest single gap in Kory's book — 306 of 1,022 contacts —
    and LinkedIn structurally cannot supply it, so his own inbox is the best
    source available.

    Scoped to the lines just below the name/title for the same reason the title
    is: further down a message is a reply chain, and the number there belongs to
    whoever wrote that part. A switchboard number written onto a contact is not
    a disaster, but a *wrong* one is, so a labelled mobile or direct line wins
    over an unlabelled number and anything marked fax is skipped.
    """
    if start_at < -1:
        return ""
    window = lines[max(0, start_at) : max(0, start_at) + 9]
    fallback = ""
    for line in window:
        low = line.lower()
        if any(marker in low for marker in _PHONE_EXCLUDE):
            continue
        match = _PHONE_RE.search(line)
        if not match:
            continue
        raw = match.group(0).strip()
        digits = re.sub(r"\D", "", raw)
        if len(digits) < 10:
            continue
        # Drop a leading country code before reading the area code.
        national = digits[1:] if len(digits) == 11 and digits[0] == "1" else digits
        if national[:3] in _TOLL_FREE:
            continue
        if any(marker in low for marker in _PHONE_PREFERRED):
            return raw
        fallback = fallback or raw
    return fallback


ENRICHABLE_FIELDS = ("jobtitle", "company", "phone")

# Mining one contact's signature costs an inbox search plus a body fetch per
# candidate message — measured at ~3s. Eighty candidates is four minutes, which
# is how this timed out in Teams on its first real run. The work is bounded two
# ways: a hard cap on contacts per call, and a wall clock that returns whatever
# has been found so far rather than dying with nothing.
ENRICH_BATCH_DEFAULT = 12
ENRICH_BATCH_MAX = 25
ENRICH_TIME_BUDGET_SEC = 70
# A contact is remembered once it has been looked at, whether or not anything
# was found. Remembering only the empty ones looks right and is not: a contact
# that yielded a fill still has the gap until Kory applies the batch, so it comes
# straight back in the next scan. A server-side sweep spun on the same 19
# contacts for nine rounds that way, and "keep going" in Teams would have stalled
# the same. `clear_enrichment_check_cache()` is the way back.
ENRICH_MISS_TTL_DAYS = 14

# How many contacts per batch may have a LinkedIn profile *searched* for. A
# contact that already carries a URL is exempt: that path is one fetch, and it
# serves the placeholder records nothing else can touch. Measured live — an
# unrationed batch spent its whole 70-second budget on four contacts.
PERSON_SEARCH_MAX_PER_BATCH = 6

# Share of the batch budget the company tiers may spend before the inbox and
# profile tiers get their turn. They are the most certain sources, so they go
# first — but "first" must not mean "all of it".
PHASE1_BUDGET_SHARE = 0.55


def _ensure_enrichment_checks_table(conn: Any) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS hubspot_enrichment_checks (
            contact_id TEXT PRIMARY KEY,
            checked_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )


def _recently_checked_contacts() -> set[str]:
    """Contacts already looked at recently — hit or miss — so a scan can move on."""
    from app.storage.lexi_db import get_lexi_connection

    try:
        with get_lexi_connection() as conn:
            _ensure_enrichment_checks_table(conn)
            rows = conn.execute(
                "SELECT contact_id FROM hubspot_enrichment_checks "
                "WHERE checked_at > datetime('now', ?)",
                (f"-{ENRICH_MISS_TTL_DAYS} days",),
            ).fetchall()
        return {str(r["contact_id"]) for r in rows}
    except Exception:
        # A cache miss must never stop the scan — worst case it redoes the work.
        return set()


def _record_contacts_checked(contact_ids: list[str]) -> None:
    if not contact_ids:
        return
    from app.storage.lexi_db import get_lexi_connection

    try:
        with get_lexi_connection() as conn:
            _ensure_enrichment_checks_table(conn)
            conn.executemany(
                "INSERT INTO hubspot_enrichment_checks (contact_id, checked_at) "
                "VALUES (?, datetime('now')) "
                "ON CONFLICT(contact_id) DO UPDATE SET checked_at = datetime('now')",
                [(str(cid),) for cid in contact_ids],
            )
            conn.commit()
    except Exception:
        pass


def clear_enrichment_check_cache() -> int:
    """Forget which contacts had no signature, so the next scan re-checks them."""
    from app.storage.lexi_db import get_lexi_connection

    with get_lexi_connection() as conn:
        _ensure_enrichment_checks_table(conn)
        removed = conn.execute("DELETE FROM hubspot_enrichment_checks").rowcount
        conn.commit()
    return removed


def _needs_filling(contact: dict[str, Any], field: str) -> bool:
    """Blank, or populated with something that carries no information."""
    return not str(contact.get(field) or "").strip() or is_placeholder(
        contact.get(field), field=field
    )


def find_enrichment_candidates(
    *,
    limit: int = 25,
    owner_id: str | None = None,
    include_phone: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Contacts with a gap a signature could close, plus what was scanned.

    Three passes, because one HubSpot filter group cannot express them:

    1. No job title — the original scan.
    2. No phone, when asked. The biggest single gap in the book (306 of 1,022)
       and the one LinkedIn structurally cannot fill.
    3. A *sweep* for placeholder values. "Prefer No Connection to Company" is
       not blank, so no NOT_HAS_PROPERTY filter will ever surface it, and every
       contact carrying one was invisible to enrichment. There is no HubSpot
       operator for "is junk", so this reads his book and decides client-side.

    Every pass is scoped to people he has actually corresponded with: the rest
    have no signature to read and no relationship to justify the work.
    """
    contacted = {"propertyName": "notes_last_contacted", "operator": "HAS_PROPERTY"}
    by_id: dict[str, dict[str, Any]] = {}
    scanned = 0
    totals: dict[str, int | None] = {}

    passes: list[tuple[str, list[dict[str, Any]]]] = [
        # Company first: it is the gap HubSpot's own records can close outright,
        # with no inference and no inbox reading. Title-first ordering meant a
        # batch spent its whole budget on signature mining and never reached a
        # contact whose employer was already sitting in the CRM.
        ("missing_company", [{"propertyName": "company", "operator": "NOT_HAS_PROPERTY"}, contacted]),
        ("missing_jobtitle", [{"propertyName": "jobtitle", "operator": "NOT_HAS_PROPERTY"}, contacted]),
    ]
    if include_phone:
        passes.append(
            ("missing_phone", [{"propertyName": "phone", "operator": "NOT_HAS_PROPERTY"}, contacted])
        )

    for label, filters in passes:
        found = search_contacts(limit=limit, owner_id=owner_id or None, filters=filters)
        totals[label] = found.get("total")
        scanned += found.get("count") or 0
        for contact in found.get("contacts") or []:
            cid = str(contact.get("id") or "")
            if cid:
                by_id.setdefault(cid, contact)

    # The placeholder sweep. Reads a wider slice because the junk is scattered
    # and no filter narrows it.
    sweep = search_contacts(limit=max(limit, 200), owner_id=owner_id or None, filters=[contacted])
    totals["placeholder_sweep"] = sweep.get("total")
    scanned += sweep.get("count") or 0
    placeholders = 0
    for contact in sweep.get("contacts") or []:
        junk = [f for f in ENRICHABLE_FIELDS if str(contact.get(f) or "").strip() and is_placeholder(contact.get(f), field=f)]
        if not junk:
            continue
        if not include_phone and junk == ["phone"]:
            continue
        placeholders += 1
        cid = str(contact.get("id") or "")
        if cid:
            by_id.setdefault(cid, contact)

    return list(by_id.values()), {
        "scanned": scanned,
        "totals": totals,
        "placeholder_hits": placeholders,
    }


def propose_field_enrichment(
    *,
    limit: int = ENRICH_BATCH_DEFAULT,
    owner_id: str | None = None,
    include_phone: bool = False,
    use_website: bool = True,
    use_person_lookup: bool = True,
    time_budget_seconds: int = ENRICH_TIME_BUDGET_SEC,
) -> dict[str, Any]:
    """Propose title/company/phone fills sourced from Kory's own email signatures.

    Read-only: proposals are staged for approval and never applied while writes
    are blocked. Only fields that are blank *or hold a placeholder* are ever
    proposed — a real existing value is never overwritten.

    Works in batches, and says so. Reading one signature costs ~3 seconds, so a
    scan of every candidate cannot finish inside a tool call; this returns what
    it found within the budget along with how many are left, and remembers the
    contacts that had no mail so the next call starts where this one stopped.
    """
    if not hubspot_configured():
        return {"ok": False, "kory_message": "**HubSpot:** not connected."}

    batch = max(1, min(ENRICH_BATCH_MAX, limit))
    scope_owner = owner_id if owner_id is not None else kory_owner_id()
    try:
        contacts, coverage = find_enrichment_candidates(
            # Scan wider than we mine: the candidate list is cheap, the
            # signatures are not.
            limit=max(batch * 4, 100),
            owner_id=scope_owner,
            include_phone=include_phone,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc), "kory_message": "HubSpot lookup failed."}

    already_checked = _recently_checked_contacts()
    fresh = [c for c in contacts if str(c.get("id") or "") not in already_checked]
    skipped_known = len(contacts) - len(fresh)
    # Contacts that already carry a LinkedIn URL go first. Resolving one of those
    # is a single fetch instead of a search plus two, and they are also the group
    # nothing else can reach — the "Prefer No Connection to Company" records came
    # from LinkedIn, so they have a URL and no employer. Cheapest and highest
    # yield in the same sort key.
    fresh.sort(key=lambda c: 0 if str(c.get("hs_linkedin_url") or "").strip() else 1)
    queue = fresh[:batch]
    remaining = len(fresh) - len(queue)

    from app.integrations import hubspot_company_lookup as company_lookup
    from app.integrations import hubspot_person_lookup as person_lookup

    # Built from the contacts already read — no extra calls.
    consensus = company_lookup.build_domain_consensus(contacts, is_placeholder=is_placeholder)

    proposals: list[dict[str, Any]] = []
    no_source: list[dict[str, Any]] = []
    not_people: list[dict[str, Any]] = []
    may_have_moved: list[dict[str, Any]] = []
    needs_your_call: list[dict[str, Any]] = []
    checked_ids: list[str] = []
    ran_out_of_time = False
    started = time.monotonic()

    # Phase 1 — the certain source, for every contact in the batch.
    #
    # Resolving a company out of HubSpot's own records is fast and its domain
    # lookups are cached, so this runs to completion before any time is spent on
    # the inbox. Doing it per-contact inside the expensive loop meant the time
    # limit cut off contacts whose employer HubSpot already knew.
    #
    # Bounded to part of the budget. Its last tier fetches a company website,
    # which is as slow as anything downstream, and left unbounded it consumed
    # the whole batch — measured live, twenty candidates spent seventy seconds
    # here and three contacts reached the inbox. The tiers below have to be
    # reachable or they may as well not exist.
    found_company: dict[str, dict[str, Any]] = {}
    phase1_deadline = time_budget_seconds * PHASE1_BUDGET_SHARE
    for contact in queue:
        if not _needs_filling(contact, "company"):
            continue
        if time.monotonic() - started > phase1_deadline:
            ran_out_of_time = True
            break
        try:
            resolved = company_lookup.resolve_company(
                contact, consensus=consensus, use_website=use_website
            )
        except Exception:
            resolved = None
        if resolved:
            found_company[str(contact.get("id") or "")] = resolved

    # Phase 2 — the inbox, for whatever is still missing.
    checked = 0
    searches_done = 0
    for contact in queue:
        cid = str(contact.get("id") or "")
        fields: dict[str, str] = {}
        evidence: dict[str, dict[str, Any]] = {}

        resolved = found_company.get(cid)
        if resolved:
            fields["company"] = resolved["value"]
            evidence["company"] = {
                "source": resolved["source"],
                "detail": resolved["evidence"],
                "confidence": "certain",
            }

        email = (contact.get("email") or "").strip()
        known_company = fields.get("company") or str(contact.get("company") or "")
        if is_placeholder(known_company, field="company"):
            # Junk in the field must not act as a known company, or the miner
            # keeps the placeholder and proposes nothing.
            known_company = ""

        still_missing = [
            f
            for f in ENRICHABLE_FIELDS
            if (include_phone or f != "phone") and f not in fields and _needs_filling(contact, f)
        ]
        out_of_time = time.monotonic() - started > time_budget_seconds
        if email and still_missing and not out_of_time:
            checked += 1
            mined, provenance = _signature_fields_for(
                email,
                known_company=known_company,
                contact_name=str(contact.get("name") or ""),
            )
            for key, value in mined.items():
                if key in fields or key not in still_missing:
                    continue
                fields[key] = value
                evidence[key] = {
                    "source": "outlook_signature",
                    "detail": (
                        f"From {provenance.get('sender') or email}'s own signature in "
                        f"\"{provenance.get('subject') or 'a message to you'}\"."
                    ),
                    "confidence": "certain",
                    "message_id": provenance.get("message_id"),
                }
        elif still_missing and out_of_time:
            # Not searched, so not a miss — it must not be remembered as one or
            # the next batch will skip it.
            ran_out_of_time = True
            remaining += 1
            if fields:
                # The certain part still stands; propose it and move on.
                pass
            else:
                continue

        # Phase 3 — the guarded person lookup, for what neither HubSpot's own
        # records nor the inbox could close.
        #
        # The only tier that looks up an *individual*, and so the only one that
        # can be wrong about which human it found. It never asks what this
        # person's title is; it asks whether a candidate profile shows a role at
        # the employer already on file. A stranger who shares the name does not
        # also share the employer, which is what makes the answer checkable.
        known_company = fields.get("company") or str(contact.get("company") or "")
        if is_placeholder(known_company, field="company"):
            known_company = ""
        unresolved = [f for f in ("company", "jobtitle") if f not in fields and _needs_filling(contact, f)]
        not_a_person = False
        # A profile already on the record costs one fetch; finding one costs a
        # search and up to two more. Only the expensive path is rationed, or a
        # single slow batch starves the contacts this tier exists to serve.
        on_record = bool(str(contact.get("hs_linkedin_url") or "").strip())
        affordable = on_record or searches_done < PERSON_SEARCH_MAX_PER_BATCH
        if not on_record:
            searches_done += 1
        if (
            use_person_lookup
            and unresolved
            and affordable
            and time.monotonic() - started <= time_budget_seconds
        ):
            try:
                person = person_lookup.resolve_person(contact, known_company=known_company)
            except Exception:
                person = None
            skip = (person or {}).get("skip")
            if skip == "not_a_person":
                # A finding, not a failed lookup. Both real cases — "Dunes Point
                # Capital Team" and "Exuma Funds General Mailbox" — resolved to
                # a genuine executive at the right company, so every check other
                # than the name would have passed them.
                not_a_person = True
                not_people.append(
                    {
                        "contact_id": contact.get("id"),
                        "name": contact.get("name"),
                        "email": email,
                        "reason": person.get("reason"),
                    }
                )
            elif skip in {"may_have_moved", "several_current_roles"}:
                (may_have_moved if skip == "may_have_moved" else needs_your_call).append(
                    {
                        "contact_id": contact.get("id"),
                        "name": contact.get("name"),
                        "email": email,
                        "reason": person.get("reason"),
                        "profile_url": person.get("profile_url"),
                        "options": person.get("options") or [],
                    }
                )
            elif person:
                for key, value in (person.get("fields") or {}).items():
                    # The LinkedIn URL rides along on a proven match; every other
                    # field still has to be an actual gap.
                    if key in fields or (key != "hs_linkedin_url" and key not in unresolved):
                        continue
                    fields[key] = value
                    evidence[key] = (person.get("evidence") or {}).get(key) or {}

        if not fields:
            if not_a_person:
                # Saying "I could not establish anything" about a distribution
                # address invites someone to go and find a title for it.
                checked_ids.append(str(contact.get("id") or ""))
                continue
            no_source.append(
                {
                    "contact_id": contact.get("id"),
                    "email": email,
                    "name": contact.get("name"),
                    "missing": [f for f in ENRICHABLE_FIELDS
                                if (include_phone or f != "phone") and _needs_filling(contact, f)],
                    "domain": company_lookup.domain_of(email),
                    "corporate_domain": company_lookup.is_corporate_domain(email),
                }
            )
            checked_ids.append(str(contact.get("id") or ""))
            continue
        proposals.append(
            {
                "contact_id": contact.get("id"),
                "email": email,
                "name": contact.get("name"),
                "proposed_fields": fields,
                # What each value is replacing, so a review can tell "was blank"
                # from "was junk" without opening HubSpot.
                "replacing": {
                    key: str(contact.get(key) or "") for key in fields if str(contact.get(key) or "").strip()
                },
                # One record per field: what it is, where it came from, and how
                # sure we are. Values from different sources land on the same
                # contact, so provenance cannot be a single row-level fact.
                "evidence": evidence,
                "source": ",".join(sorted({e["source"] for e in evidence.values()})),
                "suggested_action": "fill_blank_fields",
            }
        )
        # Remembered too. The gap is still open until Kory applies the batch, so
        # without this the very contacts we just answered come back first in the
        # next scan and the sweep never advances.
        checked_ids.append(str(contact.get("id") or ""))

    _record_contacts_checked(checked_ids)

    batch_id = _stage_hubspot_batch(
        batch_type="field_enrichment",
        payload={"proposals": proposals, "needs_research": no_source},
    )
    junk_fixes = sum(1 for row in proposals if row.get("replacing"))
    by_source: Counter[str] = Counter(
        ev["source"] for row in proposals for ev in (row.get("evidence") or {}).values()
    )

    lines = [
        f"**Contact enrichment — {len(proposals)} fill(s) proposed** "
        f"from {checked} contact(s) checked this batch\n"
    ]
    for row in proposals[:12]:
        parts = []
        for key, value in (row.get("proposed_fields") or {}).items():
            was = (row.get("replacing") or {}).get(key)
            parts.append(f"**{key}** = {value}" + (f" _(was '{was}')_" if was else ""))
        lines.append(f"• {row.get('name') or row['email']} — {', '.join(parts)}")
        for key, ev in (row.get("evidence") or {}).items():
            lines.append(f"    ↳ {key}: {ev['detail']}")
    if len(proposals) > 12:
        # One approval applies the whole batch, so never imply the list is all of it.
        lines.append(
            f"\n_Showing 12 of {len(proposals)} proposed fills — approving applies all "
            f"{len(proposals)}._"
        )
    if not proposals:
        lines.append("_Nothing could be established for this batch._")

    if by_source:
        lines.append(
            "\n**Where these came from:** "
            + " · ".join(f"{_SOURCE_LABELS.get(s, s)} {n}" for s, n in by_source.most_common())
        )
    if junk_fixes:
        lines.append(
            f"_{junk_fixes} replace a placeholder value such as "
            "'Prefer No Connection to Company' rather than filling a blank._"
        )

    # Findings that are not fills. A distribution address and a person who has
    # changed jobs are both worth knowing about, and neither is a gap to close.
    if not_people:
        lines.append(
            f"\n**{len(not_people)} of these are not people.** Shared mailboxes and "
            "group addresses — I won't put a job title on one, because it makes the "
            "record look like a human being:"
        )
        for row in not_people[:5]:
            lines.append(f"• {row.get('name') or row.get('email')} — {row.get('reason')}")

    if may_have_moved:
        lines.append(
            f"\n**{len(may_have_moved)} appear to have moved on.** Their profile shows "
            "the role at that employer has ended, so filling in the old title would make "
            "a stale record look freshly confirmed:"
        )
        for row in may_have_moved[:5]:
            lines.append(f"• {row.get('reason')}")

    if needs_your_call:
        lines.append(
            f"\n**{len(needs_your_call)} hold more than one role right now** and nothing on "
            "the record says which one you deal with them in. Tell me which and I'll fill it:"
        )
        for row in needs_your_call[:5]:
            lines.append(f"• {row.get('reason')}")

    # Say plainly what could not be established. This must stay accurate about
    # what Lexi can actually reach: she *does* look people up now, but only
    # where a candidate profile can be corroborated against the employer already
    # on file. Overstating it invites blind trust; understating it sends Kory
    # chasing work she has already done.
    if no_source:
        researchable = [r for r in no_source if r.get("corporate_domain")]
        lines.append(
            f"\n**{len(no_source)} I could not establish anything for.** Nothing in your inbox, "
            "nothing in HubSpot's own company records, nothing usable on their company's "
            "website, and no LinkedIn profile I could tie to them with certainty. Where I "
            "find a profile but can't confirm it's the same person — same name, different "
            "employer — I leave it alone rather than guess."
        )
        if researchable:
            lines.append(
                f"_{len(researchable)} are on a company domain "
                f"({', '.join(sorted({r['domain'] for r in researchable})[:5])}…) — those are "
                "the ones worth a manual look._"
            )

    if remaining or ran_out_of_time:
        lines.append(
            f"\n**{remaining} contact(s) still unchecked.** "
            + (
                "I stopped at the time limit for this batch. "
                if ran_out_of_time
                else "I work through these a batch at a time. "
            )
            + "Say _keep going_ and I'll pick up where I left off."
        )
    if skipped_known:
        lines.append(f"_Skipped {skipped_known} I already went through recently._")

    lines.append(
        f"\n_Staged only — HubSpot was not modified. Apply with batch `{batch_id}`; "
        "every value records where it came from and can be undone._"
    )
    return {
        "ok": True,
        "batch_id": batch_id,
        "proposal_count": len(proposals),
        "proposals": proposals,
        "needs_research": no_source,
        "no_source_count": len(no_source),
        "not_people": not_people,
        "may_have_moved": may_have_moved,
        "needs_your_call": needs_your_call,
        "placeholder_replacements": junk_fixes,
        "sources": dict(by_source),
        "checked": checked,
        "remaining": remaining,
        "skipped_recently_checked": skipped_known,
        "stopped_at_time_limit": ran_out_of_time,
        "coverage": coverage,
        "writes_blocked": hubspot_writes_blocked(),
        "kory_message": "\n".join(lines),
    }


_SOURCE_LABELS = {
    "hubspot_company_association": "HubSpot's own company link",
    "hubspot_company_domain": "HubSpot company record for the domain",
    "kory_book_consensus": "your other contacts at that domain",
    "outlook_signature": "their email signature",
    "linkedin_profile_corroborated": "their LinkedIn profile, matched on the employer you already had",
    "linkedin_profile_on_record": "the LinkedIn profile already on their contact record",
}


def _signature_fields_for(
    email: str, *, known_company: str = "", contact_name: str = ""
) -> tuple[dict[str, str], dict[str, Any]]:
    """Read recent mail *from* this person and mine the signature (read-only).

    ``search_inbox`` returns a normalised summary whose ``preview`` is only the
    opening lines — signatures live at the foot of a message, so the full body
    has to be fetched per message.

    Returns the fields *and* the message they came from. A value written into
    the shared CRM has to be traceable to its source: without that, a wrong
    title is indistinguishable from a right one six months later, and there is
    no way to judge whether a whole batch should be rolled back.
    """
    target = email.strip().lower()
    if not target:
        return {}, {}
    try:
        from app.integrations.outlook_inbox import search_inbox

        messages, _ = search_inbox(query=email, top=5)
    except Exception:
        return {}, {}

    for summary in messages or []:
        # Only trust a signature in mail the contact actually sent.
        sender = str(summary.get("sender") or "").strip().lower()
        if sender and sender != target:
            continue
        message_id = summary.get("message_id")
        if not message_id:
            continue
        try:
            from app.integrations.outlook_email import get_message

            message, _ = get_message(str(message_id), role="read")
        except Exception:
            continue
        body = ""
        raw_body = message.get("body")
        if isinstance(raw_body, dict):
            body = raw_body.get("content") or ""
        body = body or message.get("bodyPreview") or ""
        fields = _extract_signature_fields(
            body, known_company=known_company, contact_name=contact_name
        )
        if fields:
            return fields, {
                "source": "outlook_signature",
                "message_id": str(message_id),
                "subject": str(summary.get("subject") or "")[:120],
                "received": str(summary.get("received") or summary.get("received_at") or ""),
                "sender": sender or target,
            }
    return {}, {}


# Retired: `hs_analytics_source` is OFFLINE for every contact in this portal, so
# "missing lead source" was never a real condition. Kept as a thin alias so any
# stored shortcut still resolves to the report that replaced it.
def propose_lead_source_fills(*, limit: int = 25) -> dict[str, Any]:
    return propose_field_enrichment(limit=limit)


def contact_deals(contact_id: str) -> list[dict[str, Any]]:
    """Deals associated with a contact, with real stage labels."""
    if not contact_id:
        return []
    try:
        assoc = execute_hubspot_tool(
            "HUBSPOT_LIST_OBJECT_ASSOCIATIONS",
            {"objectType": "contacts", "objectId": str(contact_id), "toObjectType": "deals"},
        )
        deal_ids = [
            str(row.get("toObjectId"))
            for row in (assoc.get("data") or {}).get("results") or []
            if row.get("toObjectId")
        ]
    except Exception:
        return []
    if not deal_ids:
        return []
    try:
        found = execute_hubspot_tool(
            HUBSPOT_SEARCH_DEALS,
            {
                "limit": min(len(deal_ids), _MAX_PAGE),
                "properties": DEAL_PROPERTIES,
                "filterGroups": [
                    {"filters": [{"propertyName": "hs_object_id", "operator": "IN", "values": deal_ids}]}
                ],
            },
        )
    except Exception:
        return []
    return _describe_deals(_normalize_deals(found.get("data")))


def _describe_deals(deals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach pipeline/stage labels and an open/closed verdict to raw deals."""
    stages = deal_stage_map()
    out: list[dict[str, Any]] = []
    for deal in deals:
        info = stages.get(str(deal.get("dealstage") or "")) or {}
        enriched = dict(deal)
        enriched["pipeline_label"] = info.get("pipeline") or ""
        enriched["stage_label"] = info.get("stage") or str(deal.get("dealstage") or "unknown")
        enriched["is_open"] = not info.get("is_closed", False)
        out.append(enriched)
    return out


_ORG_SUFFIXES = frozenset({"inc", "llc", "ltd", "co", "corp", "group", "partners", "capital"})


def _split_name_and_org(text: str) -> tuple[str, str]:
    """Separate a person from the company they were described with.

    "Angelo (Morgan Stanley)" -> ("Angelo", "Morgan Stanley")
    "Angelo Amitsis"          -> ("Angelo Amitsis", "")
    """
    raw = (text or "").strip()
    parenthetical = re.search(r"\(([^)]+)\)", raw)
    if parenthetical:
        org = parenthetical.group(1).strip()
        person = re.sub(r"\([^)]*\)", " ", raw)
        return re.sub(r"\s+", " ", person).strip(" ,-–—"), org
    for separator in (" at ", " from ", " of ", " — ", " - ", ", "):
        if separator in raw:
            person, _, org = raw.partition(separator)
            if person.strip() and org.strip():
                return person.strip(), org.strip()
    return raw, ""


def _org_matches(hint: str, contact: dict[str, Any]) -> bool:
    """Does a stated company plausibly describe this contact?"""
    def tokens(value: str) -> set[str]:
        return {
            t for t in re.split(r"[^a-z0-9]+", (value or "").lower())
            if t and t not in _ORG_SUFFIXES
        }

    wanted = tokens(hint)
    if not wanted:
        return False
    haystack = tokens(
        " ".join(
            str(contact.get(field) or "")
            for field in ("company", "jobtitle", "email", "website")
        )
    )
    return bool(wanted & haystack)


def enrich_prebrief_from_hubspot(*, email: str = "", name: str = "") -> dict[str, Any]:
    """Read-only HubSpot context for a person Kory is about to meet."""
    if not hubspot_configured():
        return {
            "ok": False,
            "kory_message": "HubSpot not connected — prebrief enrichment skipped.",
        }
    query = email.strip() or name.strip()
    if not query:
        return {"ok": False, "error": "email or name required"}

    # People arrive described, not named: the briefing says "Angelo (Morgan
    # Stanley)". Searching that whole string returned "No HubSpot contact" — the
    # name filter below required every word, including "(morgan" and "stanley)",
    # to appear in the contact's *name*. Lexi reported no CRM record and drafted
    # to a placeholder address. The company belongs against the company field, as
    # a way to tell two people apart, not as part of the name.
    search_name, company_hint = (name.strip(), "") if not email.strip() else (name.strip(), "")
    if search_name:
        search_name, company_hint = _split_name_and_org(search_name)
    lookup_query = email.strip() or search_name or query

    try:
        found = search_contacts(limit=10, query=lookup_query)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "kory_message": f"HubSpot lookup failed for {query}."}

    contacts = found.get("contacts") or []
    match = None
    email_l = email.strip().lower()
    for contact in contacts:
        if email_l and (contact.get("email") or "").lower() == email_l:
            match = contact
            break

    if match is None and contacts:
        # HubSpot's free-text search is loose — querying "Mark" also returns
        # "Karen Brown" via andrew.brown@markel.com. Only accept a candidate
        # whose *name* actually contains every word asked for, and never guess
        # between several people: showing the wrong person's Do Not Contact
        # status is worse than asking which one Kory meant.
        wanted = [w for w in re.split(r"\s+", (search_name or query).strip().lower()) if w]
        plausible = [
            contact
            for contact in contacts
            if wanted and all(word in str(contact.get("name") or "").lower() for word in wanted)
        ]
        # A stated company is evidence, not a guess: "Angelo (Morgan Stanley)"
        # picks Angelo Amitsis over Chris Angelo without having to ask.
        if len(plausible) > 1 and company_hint:
            by_org = [c for c in plausible if _org_matches(company_hint, c)]
            if len(by_org) == 1:
                plausible = by_org
        if len(plausible) == 1:
            match = plausible[0]
        elif len(plausible) > 1:
            lines = [f"**{len(plausible)} contacts match '{query}'** — which one?\n"]
            for contact in plausible[:8]:
                role = " · ".join(
                    x for x in (contact.get("jobtitle"), contact.get("company")) if x
                )
                lines.append(
                    f"• **{contact.get('name')}** — {contact.get('email')}"
                    + (f" ({role})" if role else "")
                )
            return {
                "ok": True,
                "found": False,
                "ambiguous": True,
                "candidates": plausible,
                "kory_message": "\n".join(lines),
            }

    if not match:
        return {
            "ok": True,
            "found": False,
            "kory_message": f"No HubSpot contact for {query}.",
        }

    title = match.get("jobtitle")
    company = match.get("company")
    headline = match.get("name") or match.get("email")
    role = " · ".join(
        value
        for value, field in ((title, "jobtitle"), (company, "company"))
        if value and not is_placeholder(value, field=field)
    )

    lines = [f"**{headline}**" + (f" — {role}" if role else "")]
    lines.append(
        f"Stage: {lifecycle_label(match)} · Status: {match.get('hs_lead_status') or 'unknown'}"
    )
    last = match.get("notes_last_contacted")
    days = _days_since(str(last)) if last else None
    if days is not None:
        lines.append(f"Last contact: {days} days ago")
    elif not last:
        lines.append("Last contact: never recorded")
    owner_id = str(match.get("hubspot_owner_id") or "").strip()
    if owner_id and owner_id != kory_owner_id():
        lines.append(f"Owner: {owner_name(owner_id)} (not yours)")

    deals = contact_deals(str(match.get("id") or ""))
    open_deals = [d for d in deals if d.get("is_open")]
    if open_deals:
        lines.append("")
        for deal in open_deals[:3]:
            amount = _format_amount(deal.get("amount"))
            lines.append(
                f"Open deal: **{deal.get('dealname') or 'Untitled'}** — "
                f"{deal.get('pipeline_label')} / {deal.get('stage_label')}{amount}"
            )
    elif deals:
        lines.append(f"\n{len(deals)} closed deal(s) on record, none open.")

    if is_do_not_contact(match):
        lines.append("\n⚠️ **Marked Do Not Contact** — do not include in outreach.")

    return {
        "ok": True,
        "found": True,
        "contact": match,
        "deals": deals,
        "open_deal_count": len(open_deals),
        "do_not_contact": is_do_not_contact(match),
        "kory_message": "\n".join(lines),
    }


def find_contacts(
    *,
    company: str = "",
    quiet_days: int = 0,
    lifecycle: str = "",
    limit: int = 25,
    include_all_owners: bool = False,
) -> dict[str, Any]:
    """Search Kory's contacts by company, silence, or relationship stage.

    Answers "show me everyone at Bertram Capital" and "who haven't I spoken to
    in a year" — read-only, and opt-outs are labelled rather than hidden so a
    follow-up suggestion can't quietly include someone who asked not to be
    contacted.
    """
    if not hubspot_configured():
        return {"ok": False, "kory_message": "**HubSpot:** not connected."}

    scope_owner = "" if include_all_owners else kory_owner_id()
    filters: list[dict[str, Any]] = []
    described: list[str] = []
    if company.strip():
        filters.append(
            {"propertyName": "company", "operator": "CONTAINS_TOKEN", "value": company.strip()}
        )
        described.append(f"at **{company.strip()}**")
    if quiet_days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=quiet_days)
        filters.append(
            {
                "propertyName": "notes_last_contacted",
                "operator": "LT",
                "value": str(int(cutoff.timestamp() * 1000)),
            }
        )
        described.append(f"not contacted in **{quiet_days}+ days**")
    if not filters:
        return {
            "ok": False,
            "error": "company or quiet_days required",
            "kory_message": "Tell me a company name or how long since you last spoke to them.",
        }

    try:
        found = search_contacts(limit=limit, owner_id=scope_owner or None, filters=filters)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "kory_message": "HubSpot lookup failed."}

    contacts = found.get("contacts") or []
    if lifecycle.strip():
        wanted = lifecycle.strip().lower()
        contacts = [c for c in contacts if wanted in lifecycle_label(c).lower()]

    total = count_contacts(
        ([_owner_filter(scope_owner)] if scope_owner else []) + filters
    )
    whose = "your contacts" if scope_owner else "all IFG contacts"
    header = f"**{total} of {whose}** " + " and ".join(described)
    lines = [header + "\n"]
    for contact in contacts[:15]:
        role = " · ".join(
            value
            for value, field in ((contact.get("jobtitle"), "jobtitle"), (contact.get("company"), "company"))
            if value and not is_placeholder(value, field=field)
        )
        days = _days_since(str(contact.get("notes_last_contacted") or ""))
        age = f" — {days}d ago" if days is not None else ""
        flag = " · ⚠️ Do Not Contact" if is_do_not_contact(contact) else ""
        lines.append(f"• **{contact.get('name') or contact.get('email')}**"
                     + (f" ({role})" if role else "") + age + flag)
    if not contacts:
        lines.append("_No matches._")
    elif total and total > len(contacts[:15]):
        lines.append(f"\n_Showing {min(len(contacts), 15)} of {total}._")
    return {
        "ok": True,
        "count": len(contacts),
        "total": total,
        "contacts": contacts,
        "kory_message": "\n".join(lines),
    }


def compare_books(*, limit_owners: int = 6) -> dict[str, Any]:
    """Compare Kory's book against the rest of IFG, owner by owner.

    Reads span the whole portal on purpose — Kory should see the company
    picture even though writes stay scoped to his own contacts.
    """
    if not hubspot_configured():
        return {"ok": False, "kory_message": "**HubSpot:** not connected."}

    # Discover which owners actually hold contacts, rather than assuming.
    seen: dict[str, int] = {}
    try:
        scan = search_contacts(limit=400, properties=["email", "hubspot_owner_id"])
        for contact in scan.get("contacts") or []:
            key = str(contact.get("hubspot_owner_id") or "").strip() or "unassigned"
            seen[key] = seen.get(key, 0) + 1
    except Exception as exc:
        return {"ok": False, "error": str(exc), "kory_message": "HubSpot read failed."}

    known = owner_map()
    rows: list[dict[str, Any]] = []
    for owner_id in sorted(seen, key=lambda k: seen[k], reverse=True)[:limit_owners]:
        filters = [] if owner_id == "unassigned" else [_owner_filter(owner_id)]
        if not filters:
            continue
        total = count_contacts(filters)
        if not total:
            continue

        def missing(prop: str) -> int | None:
            return count_contacts(filters + [{"propertyName": prop, "operator": "NOT_HAS_PROPERTY"}])

        rows.append(
            {
                "owner_id": owner_id,
                "owner": known.get(owner_id) or f"unknown owner ({owner_id})",
                "known_owner": owner_id in known,
                "total": total,
                "missing_jobtitle": missing("jobtitle"),
                "missing_company": missing("company"),
                "missing_phone": missing("phone"),
                "never_contacted": missing("notes_last_contacted"),
                "is_kory": owner_id == kory_owner_id(),
            }
        )

    def pct(part: int | None, whole: int | None) -> str:
        if not part or not whole:
            return "0%"
        return f"{part / whole:.0%}"

    lines = ["**Contact books across IFG**\n"]
    for row in rows:
        marker = " ← yours" if row["is_kory"] else ""
        lines.append(f"**{row['owner']}** — {row['total']} contacts{marker}")
        lines.append(
            f"  no title {pct(row['missing_jobtitle'], row['total'])} · "
            f"no company {pct(row['missing_company'], row['total'])} · "
            f"no phone {pct(row['missing_phone'], row['total'])} · "
            f"never contacted {pct(row['never_contacted'], row['total'])}"
        )
        lines.append("")

    orphaned = [r for r in rows if not r["known_owner"]]
    if orphaned:
        lines.append("**⚠️ Unassigned records**")
        for row in orphaned:
            lines.append(
                f"• {row['total']} contacts belong to owner id `{row['owner_id']}`, "
                "who is not an active or archived HubSpot user — most likely a "
                "departed employee whose records were never reassigned."
            )
        lines.append("")
    # A high never-contacted rate marks a prospecting list, not a bad book;
    # saying so stops the comparison reading as a judgement on a colleague.
    lines.append(
        "_Books serve different purposes — a high never-contacted share usually "
        "means a prospecting list rather than a relationship book._"
    )
    return {
        "ok": True,
        "books": rows,
        "orphaned_owners": orphaned,
        "kory_message": "\n".join(lines).rstrip(),
    }


def recent_changes(*, days: int = 7) -> dict[str, Any]:
    """What actually changed lately — new contacts and deal stage movements.

    Deliberately ignores ``lastmodifieddate``: syncs touch ~92% of contacts and
    100% of deals every month, so "modified" reports everything and means
    nothing. New records and real stage transitions are the honest signals.
    """
    if not hubspot_configured():
        return {"ok": False, "kory_message": "**HubSpot:** not connected."}

    window_start = datetime.now(timezone.utc) - timedelta(days=max(1, days))
    stamp = str(int(window_start.timestamp() * 1000))
    created_filter = [{"propertyName": "createdate", "operator": "GT", "value": stamp}]

    new_total = count_contacts(created_filter)
    new_kory = count_contacts(created_filter + [_owner_filter(kory_owner_id())])
    history = deal_stage_movements(days=days)
    movements = history.get("moves") or []

    lines = [f"**Last {days} days**\n"]
    lines.append(f"• New contacts: **{new_total}** across IFG ({new_kory} yours)")
    if not history.get("ok"):
        # Never present a failed lookup as "nothing happened".
        lines.append(f"\n_Deal movement unavailable — {history.get('error')}_")
    elif movements:
        lines.append(f"\n**Deal movement ({len(movements)}):**")
        for move in movements[:10]:
            arrow = (
                f"{move['from_stage']} → **{move['to_stage']}**"
                if move.get("from_stage")
                else f"→ **{move['to_stage']}**"
            )
            lines.append(f"• {move['dealname']} — {arrow}")
    else:
        lines.append(
            f"\n_No deal stage changes in this window ({history.get('scanned')} deals checked)._"
        )
    return {
        "ok": True,
        "days": days,
        "new_contacts": new_total,
        "new_contacts_kory": new_kory,
        "deal_movements": movements,
        "movements_ok": bool(history.get("ok")),
        "movements_error": history.get("error"),
        "kory_message": "\n".join(lines),
    }


# HubSpot refuses history requests for more than 50 objects at a time.
_HISTORY_PAGE = 50


def deal_stage_movements(*, days: int = 7, scan: int = 200) -> dict[str, Any]:
    """Deal stage transitions inside the window, from HubSpot's property history.

    Returns ``{"ok", "moves", "scanned", "error"}``. A failure is reported rather
    than swallowed: an empty list and a failed request look identical to the
    caller otherwise, and "no deals moved" is a very believable wrong answer.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, days))
    stages = deal_stage_map()
    moves: list[dict[str, Any]] = []
    scanned = 0
    after: str | None = None

    while scanned < scan:
        arguments: dict[str, Any] = {
            "limit": min(_HISTORY_PAGE, scan - scanned),
            "properties": ["dealname", "dealstage"],
            "propertiesWithHistory": ["dealstage"],
        }
        if after:
            arguments["after"] = after
        try:
            result = execute_hubspot_tool(HUBSPOT_LIST_DEALS, arguments)
        except Exception as exc:
            return {
                "ok": False,
                "moves": moves,
                "scanned": scanned,
                "error": f"{type(exc).__name__}: {exc}",
            }
        data = result.get("data") or {}
        rows = data.get("results") or []
        scanned += len(rows)

        for row in rows:
            history = (row.get("propertiesWithHistory") or {}).get("dealstage") or []
            stamped = [
                (when, str(entry.get("value")))
                for entry in history
                if (when := _parse_ts(entry.get("timestamp")))
            ]
            stamped.sort()
            recent = [(when, value) for when, value in stamped if when >= cutoff]
            if not recent:
                continue
            older = [(when, value) for when, value in stamped if when < cutoff]
            prior = older[-1][1] if older else (recent[0][1] if len(recent) > 1 else None)
            newest = recent[-1][1]
            if prior == newest:
                continue
            props = row.get("properties") or {}
            moves.append(
                {
                    "deal_id": row.get("id"),
                    "dealname": props.get("dealname") or "Untitled",
                    "from_stage": (stages.get(str(prior)) or {}).get("stage") if prior else None,
                    "to_stage": (stages.get(newest) or {}).get("stage") or newest,
                    "at": recent[-1][0].isoformat(),
                }
            )

        after = ((data.get("paging") or {}).get("next") or {}).get("after")
        if not after or not rows:
            break

    moves.sort(key=lambda m: m["at"], reverse=True)
    return {"ok": True, "moves": moves, "scanned": scanned, "error": None}


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _format_amount(raw: Any) -> str:
    """Format a deal amount, hiding the $1 placeholders that pollute the pipeline."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return ""
    if value <= 1:
        return ""
    return f" · ${value:,.0f}"


def stage_meeting_note(
    *,
    email: str,
    note: str,
    meeting_subject: str = "",
    approved: bool = False,
    owner_ack: bool = False,
) -> dict[str, Any]:
    """Stage a HubSpot note after a meeting — no live write while blocked.

    The approval gate sits *after* the contact lookup on purpose. It used to be
    the first line, which meant an unconfirmed call returned
    `confirmation_required` in 0.01s and everything below — the address lookup,
    the "did you mean one of these five people" list, the ownership check — never
    ran. Kory asked for a note, was asked to confirm, confirmed, and only then
    heard that nobody in HubSpot has that address or that the record is
    Natalie's. Two round trips to learn something knowable in the first.

    Finding the contact is a read. It needs no approval, and nothing below it
    touches HubSpot until the gate passes.
    """
    text = note.strip()
    if not text:
        return {"ok": False, "error": "note is required"}
    body = text
    if meeting_subject.strip():
        body = f"Meeting: {meeting_subject.strip()}\n\n{text}"

    # The address decides the record, so ask by address: an exact EQ filter on
    # email. Free-text query was doing this job, which was wrong twice over — it
    # is loose (a query for one person routinely returns others, which is why the
    # exact-match loop below exists), and it is backed by a different, slower
    # index, so a contact that is definitely there reads as contact_not_found for
    # a while after it is created. The filter index is consistent immediately.
    #
    # The loose query is still worth running, but only when the address matched
    # nothing: then its hits become the "did you mean?" list for Kory.
    try:
        found = search_contacts(
            limit=5,
            filters=[{"propertyName": "email", "operator": "EQ", "value": email.strip()}],
        )
        if not (found.get("contacts") or []):
            found = search_contacts(limit=5, query=email)
    except Exception as exc:
        # A failed lookup must never read as "that person isn't in HubSpot".
        return {
            "ok": False,
            "error_code": "lookup_failed",
            "error": f"HubSpot lookup failed for {email}: {type(exc).__name__}: {exc}",
            "kory_message": (
                f"I couldn't reach HubSpot to look up **{email}** ({type(exc).__name__}), "
                "so I haven't staged anything. Worth retrying in a moment."
            ),
        }

    candidates = found.get("contacts") or []
    matched: dict[str, Any] | None = None
    for contact in candidates:
        if (contact.get("email") or "").strip().lower() == email.strip().lower():
            matched = contact
            break

    if matched is None:
        near = [c for c in candidates if (c.get("email") or "").strip()]
        lines = [
            f"No HubSpot contact has the address **{email}**, so there's nothing to attach a note to."
        ]
        if near:
            lines.append("\nThe closest matches were:")
            for contact in near[:5]:
                lines.append(f"• {contact.get('name') or 'Unnamed'} — {contact.get('email')}")
            lines.append(
                "\nIf one of those is the right person, give me the address on their record "
                "and I'll file it there. I won't guess between them."
            )
        else:
            lines.append(" Add them in HubSpot first, or give me the address on their record.")
        return {
            "ok": False,
            "error_code": "contact_not_found",
            "error": f"No HubSpot contact for {email} — nothing staged.",
            "near_matches": [
                {"id": c.get("id"), "name": c.get("name"), "email": c.get("email")} for c in near[:5]
            ],
            "kory_message": "\n".join(lines),
        }

    blocked = assert_contact_writable(matched, owner_ack=owner_ack)
    if blocked:
        return blocked
    contact_id = matched.get("id")

    if not contact_id:
        # A matched record with no id would stage an un-appliable note.
        return {
            "ok": False,
            "error_code": "contact_not_found",
            "error": f"HubSpot returned a match for {email} with no record id — nothing staged.",
            "kory_message": (
                f"HubSpot matched **{email}** but returned no record id, so I can't attach a note. "
                "Nothing was staged."
            ),
        }

    # Everything above is a read. This is the last point before anything is
    # staged or written, and the action names the record so the confirmation
    # Kory sees says which contact he is agreeing to.
    assert_kory_approved_write(
        approved=approved,
        action=f"HubSpot meeting note on {matched.get('name') or email}",
    )

    batch_id = _stage_hubspot_batch(
        batch_type="meeting_note",
        payload={
            "email": email,
            "contact_id": contact_id,
            "note": body,
            "meeting_subject": meeting_subject,
        },
    )
    if hubspot_writes_blocked() or not approved:
        return {
            "ok": True,
            "batch_id": batch_id,
            "dry_run": True,
            "writes_blocked": True,
            "kory_message": (
                f"Staged HubSpot note for {email} (batch `{batch_id}`). "
                "Not written — live HubSpot writes are blocked."
            ),
        }

    result = execute_hubspot_tool(HUBSPOT_CREATE_NOTE, note_payload(contact_id=contact_id, body=body))
    wrote, refusal = _hubspot_write_ok(result)
    return {
        "ok": wrote,
        **({"error": refusal} if not wrote else {}),
        "batch_id": batch_id,
        "dry_run": bool(result.get("dry_run")),
        "composio_log_id": result.get("log_id"),
    }


def find_contacts_for_outreach(
    *,
    goal: str = "",
    lifecycle: str = "",
    query: str = "",
    limit: int = 15,
    owner_id: str | None = None,
    include_all_owners: bool = False,
) -> dict[str, Any]:
    """Outreach candidates (read-only).

    Two rules are absolute here: contacts marked Do Not Contact are never
    returned, and an empty result stays empty. The previous version fell back to
    ``contacts[:limit]`` when its filter matched nothing — which, with every
    status field reading blank, meant it always returned arbitrary contacts and
    silently included opt-outs.
    """
    scope_owner = None if include_all_owners else (owner_id or kory_owner_id())
    raw = search_contacts(limit=max(limit * 4, 60), query=query, owner_id=scope_owner)
    contacts = raw.get("contacts") or []

    lifecycle_l = lifecycle.strip().lower()
    filtered: list[dict[str, Any]] = []
    excluded_dnc = 0
    for contact in contacts:
        if is_do_not_contact(contact):
            excluded_dnc += 1
            continue
        if not (contact.get("email") or "").strip():
            continue
        if lifecycle_l:
            stage = lifecycle_label(contact).lower()
            status = str(contact.get("hs_lead_status") or "").lower()
            if lifecycle_l not in stage and lifecycle_l not in status:
                continue
        filtered.append(contact)
        if len(filtered) >= limit:
            break

    scope = "Kory's contacts" if scope_owner else "all IFG contacts"
    if not filtered:
        note = f"_No outreach candidates matched in {scope}._"
        if lifecycle_l:
            note += f" (filter: `{lifecycle}`)"
        if excluded_dnc:
            note += f"\n\n{excluded_dnc} contact(s) were skipped — marked **Do Not Contact**."
        return {
            "ok": True,
            "contacts": [],
            "count": 0,
            "excluded_do_not_contact": excluded_dnc,
            "kory_message": f"**Outreach candidates** (0)\n\n{note}",
        }

    lines = [f"**Outreach candidates** ({len(filtered)} from {scope})\n"]
    for contact in filtered[:12]:
        status = contact.get("hs_lead_status") or "no status"
        lines.append(
            f"• {contact.get('name') or contact.get('email')} — {lifecycle_label(contact)} · {status}"
        )
    if excluded_dnc:
        lines.append(f"\n_{excluded_dnc} contact(s) excluded — marked **Do Not Contact**._")
    return {
        "ok": True,
        "contacts": filtered,
        "count": len(filtered),
        "excluded_do_not_contact": excluded_dnc,
        "kory_message": "\n".join(lines),
    }


def all_deals(*, limit: int = 200) -> list[dict[str, Any]]:
    """Every deal with pipeline/stage labels resolved."""
    deals: list[dict[str, Any]] = []
    after: str | None = None
    while len(deals) < limit:
        arguments: dict[str, Any] = {
            "limit": min(_MAX_PAGE, limit - len(deals)),
            "properties": DEAL_PROPERTIES,
        }
        if after:
            arguments["after"] = after
        result = execute_hubspot_tool(HUBSPOT_LIST_DEALS, arguments)
        data = result.get("data")
        batch = _normalize_deals(data)
        deals.extend(batch)
        after = (
            ((data.get("paging") or {}).get("next") or {}).get("after")
            if isinstance(data, dict)
            else None
        )
        if not after or not batch:
            break
    return _describe_deals(deals)


def deals_snapshot_for_brief(*, limit: int = 8) -> dict[str, Any]:
    """Read-only open-deal snapshot, pipeline-aware.

    A past close date only means something for a deal that is still open — the
    Closed/Won and Closed/Lost boards are full of them by design.
    """
    if not hubspot_configured():
        return {
            "ok": False,
            "kory_message": "**Deals:** HubSpot not connected.",
        }
    try:
        deals = all_deals(limit=200)
    except Exception as exc:
        return {
            "ok": False,
            "kory_message": f"**Deals:** unavailable ({type(exc).__name__}).",
            "error": str(exc),
        }

    open_deals = [d for d in deals if d.get("is_open")]
    overdue = []
    for deal in open_deals:
        days = _days_since(str(deal.get("closedate") or ""))
        if days is not None and days > 0:
            overdue.append((days, deal))
    overdue.sort(key=lambda pair: pair[0], reverse=True)

    by_stage: dict[str, int] = {}
    for deal in open_deals:
        key = f"{deal.get('pipeline_label')} / {deal.get('stage_label')}"
        by_stage[key] = by_stage.get(key, 0) + 1

    lines = [f"**Open deals: {len(open_deals)}** of {len(deals)} total\n"]
    for key, count in sorted(by_stage.items(), key=lambda kv: kv[1], reverse=True)[:limit]:
        lines.append(f"• {key} — {count}")
    if overdue:
        lines.append(f"\n**Past close date ({len(overdue)}):**")
        for days, deal in overdue[:5]:
            lines.append(
                f"• **{deal.get('dealname') or 'Untitled'}**{_format_amount(deal.get('amount'))} "
                f"— {days}d overdue · {deal.get('stage_label')}"
            )
    else:
        lines.append("\n_No open deal is past its close date._")
    # Deliberately no pipeline total: many amounts are $1 placeholders, so any
    # sum would read as authoritative while being fiction.
    return {
        "ok": True,
        "open_count": len(open_deals),
        "total_count": len(deals),
        "deals": open_deals[:limit],
        "overdue": [d for _, d in overdue],
        "kory_message": "\n".join(lines).rstrip(),
    }


def propose_outreach_batch(
    *,
    goal: str = "",
    contact_ids: list[str] | None = None,
    limit: int = 10,
    lifecycle: str = "",
) -> dict[str, Any]:
    """Draft outreach emails for approval — no send."""
    excluded_dnc = 0
    if contact_ids:
        # Resolve ids to real records: the caller may hand us ids directly, and
        # a bare {"id": ...} dict would carry no opt-out status to check.
        contacts = contacts_by_ids(contact_ids[:limit])
        kept: list[dict[str, Any]] = []
        for contact in contacts:
            if is_do_not_contact(contact):
                excluded_dnc += 1
                continue
            kept.append(contact)
        contacts = kept
    else:
        found = find_contacts_for_outreach(goal=goal, lifecycle=lifecycle, limit=limit)
        contacts = found.get("contacts") or []
        excluded_dnc = found.get("excluded_do_not_contact") or 0

    if not contacts:
        note = "_No contacts to draft for._"
        if excluded_dnc:
            note = f"_All {excluded_dnc} contact(s) are marked **Do Not Contact** — nothing drafted._"
        return {
            "ok": True,
            "batch_id": None,
            "draft_count": 0,
            "drafts": [],
            "excluded_do_not_contact": excluded_dnc,
            "writes_blocked": hubspot_writes_blocked(),
            "kory_message": f"**HubSpot outreach drafts** (0)\n\n{note}",
        }

    drafts: list[dict[str, Any]] = []
    for contact in contacts[:limit]:
        name = contact.get("name") or (contact.get("email") or "there").split("@")[0]
        draft = _draft_outreach_email(name=name, goal=goal)
        drafts.append(
            {
                "contact_id": contact.get("id"),
                "email": contact.get("email"),
                "name": name,
                "subject": draft["subject"],
                "body": draft["body"],
            }
        )

    batch_id = _stage_hubspot_batch(
        batch_type="outreach",
        payload={"goal": goal, "drafts": drafts},
    )
    lines = [f"**HubSpot outreach drafts** ({len(drafts)})\n"]
    for d in drafts[:5]:
        lines.append(f"• **{d['subject']}** → {d['email']}")
    if excluded_dnc:
        lines.append(f"\n_{excluded_dnc} contact(s) excluded — marked **Do Not Contact**._")
    lines.append("\n_Approve in Teams before any email sends. HubSpot writes stay blocked for now._")
    return {
        "ok": True,
        "batch_id": batch_id,
        "draft_count": len(drafts),
        "drafts": drafts,
        "excluded_do_not_contact": excluded_dnc,
        "writes_blocked": hubspot_writes_blocked(),
        "kory_message": "\n".join(lines),
    }


def execute_hubspot_batch(
    *,
    batch_id: str,
    approved: bool = False,
    merge_pair: str = "",
    owner_ack: bool = False,
) -> dict[str, Any]:
    """Apply staged batch only after approval; still blocked when live writes disabled."""
    assert_kory_approved_write(approved=approved, action="HubSpot batch update")
    batch = _load_hubspot_batch(batch_id)
    if not batch:
        return {"ok": False, "error": f"Unknown batch {batch_id}"}

    if hubspot_writes_blocked():
        return {
            "ok": True,
            "dry_run": True,
            "batch_id": batch_id,
            "writes_blocked": True,
            "message": (
                "Blocked — HubSpot live writes disabled "
                "(LEXI_DRY_RUN or LEXI_HUBSPOT_LIVE_WRITES_ENABLED=false)."
            ),
            "batch": batch,
        }

    applied = 0
    errors: list[str] = []
    batch_type = batch.get("batch_type")
    payload = batch.get("payload") or {}

    if batch_type == "cleanup":
        # Cleanup no longer stages mutations. Refuse any batch left over from the
        # version that did: those rows recommended archiving active contacts.
        return {
            "ok": False,
            "batch_id": batch_id,
            "applied": 0,
            "errors": [
                "Cleanup batches are no longer applied — cleanup is a read-only report. "
                "Re-run it to see the current state."
            ],
        }
    skipped = 0
    not_owned = 0
    if batch_type == "duplicate_merge":
        # A HubSpot merge cannot be undone, and the staging message promises Kory
        # that each one gets its own approval. Applying a whole batch on a single
        # "approved" would break that promise irreversibly, so a merge has to name
        # the exact pair it means.
        pairs = payload.get("pairs", [])
        wanted = str(merge_pair or "").strip()
        if not wanted:
            return {
                "ok": False,
                "batch_id": batch_id,
                "applied": 0,
                "errors": [
                    f"This batch holds {len(pairs)} merge pair(s). HubSpot merges are "
                    "permanent, so they are applied one at a time: re-call with "
                    "merge_pair='<primary_id>:<duplicate_id>' naming the pair to merge."
                ],
                "pairs": pairs,
            }
        chosen = [
            row
            for row in pairs
            if f"{row.get('primary_id')}:{row.get('duplicate_id')}" == wanted
        ]
        if not chosen:
            return {
                "ok": False,
                "batch_id": batch_id,
                "applied": 0,
                "errors": [f"No staged merge pair matches '{wanted}' in batch {batch_id}."],
                "pairs": pairs,
            }
        for row in chosen:
            # Ownership is checked here rather than at proposal time: the scan is
            # portal-wide by design, and this is the last point before the merge
            # becomes permanent.
            blocked = _merge_owner_block(row, owner_ack=owner_ack)
            if blocked:
                return {**blocked, "batch_id": batch_id, "applied": 0, "errors": []}
            try:
                _apply_merge_row(row)
                applied += 1
            except Exception as exc:
                errors.append(str(exc))
    elif batch_type in {"field_enrichment", "lead_source_fill"}:
        for row in payload.get("proposals", []):
            try:
                outcome = _apply_field_fill(row, owner_ack=owner_ack, batch_id=batch_id)
                if outcome == "applied":
                    applied += 1
                elif outcome == "not_kory_owned":
                    # Reported separately: telling him 3 were "skipped" reads as
                    # "you'd already done those", not "those are someone else's".
                    not_owned += 1
                else:
                    # Field filled in by hand since staging — his value wins.
                    skipped += 1
            except Exception as exc:
                errors.append(str(exc))
    elif batch_type == "meeting_note":
        try:
            _raise_if_refused(
                execute_hubspot_tool(
                    HUBSPOT_CREATE_NOTE,
                    note_payload(
                        contact_id=str(payload.get("contact_id") or ""),
                        body=str(payload.get("note") or ""),
                    ),
                ),
                "Create meeting note",
            )
            applied = 1
        except Exception as exc:
            errors.append(str(exc))

    result = {
        "ok": not errors,
        "applied": applied,
        "skipped": skipped,
        "errors": errors,
        "batch_id": batch_id,
    }
    message: list[str] = []
    if batch_type in {"field_enrichment", "lead_source_fill"}:
        message.append(f"Filled {applied} field(s) on your contacts.")
        if not_owned:
            result["not_kory_owned"] = not_owned
            # Reported separately from `skipped`: "3 skipped" reads as "you'd
            # already done those", not "those are someone else's".
            message.append(
                f"Left {not_owned} alone — those records belong to other people at IFG."
            )
        if skipped:
            message.append(f"{skipped} already had a value, so I left yours in place.")
        if applied:
            result["undo_batch_id"] = batch_id
            message.append(f"Say the word and I can undo all of it — batch `{batch_id}`.")
    elif batch_type == "duplicate_merge" and applied:
        message.append(
            f"Merged {applied} pair. HubSpot merges are permanent — this one cannot be undone."
        )
    if errors:
        message.append(f"{len(errors)} failed: " + "; ".join(errors[:3]))
    if message:
        result["kory_message"] = " ".join(message)
    return result


def _hubspot_write_ok(result: dict[str, Any]) -> tuple[bool, str]:
    """Did HubSpot actually accept the write?

    execute_hubspot_tool only raises when Composio sets `error`. A vendor refusal
    arrives as successful=false with no error, which every writer here discarded
    in favour of a hardcoded ok:True — the same shape that let a malformed
    CREATE_NOTE payload report success while nothing reached the contact.
    """
    if not isinstance(result, dict):
        return False, f"unexpected HubSpot response: {type(result).__name__}"
    if result.get("dry_run"):
        return True, ""
    if result.get("successful") is False:
        data = result.get("data")
        detail = ""
        if isinstance(data, dict):
            detail = str(data.get("error") or data.get("message") or "").strip()
        return False, detail or "HubSpot rejected the request"
    return True, ""


def _raise_if_refused(result: dict[str, Any], action: str) -> None:
    ok, detail = _hubspot_write_ok(result)
    if not ok:
        raise RuntimeError(f"{action} failed: {detail}")


def _apply_merge_row(row: dict[str, Any]) -> None:
    primary = row.get("primary_id")
    duplicate = row.get("duplicate_id")
    if not primary or not duplicate:
        return
    _raise_if_refused(
        execute_hubspot_tool(
            HUBSPOT_MERGE_CONTACTS,
            {"primaryObjectId": primary, "objectIdToMerge": duplicate},
        ),
        f"Merge {duplicate} into {primary}",
    )


def ensure_applied_writes_table(conn: Any) -> None:
    """One definition of the undo log, used by every reader and writer."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS hubspot_applied_writes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id TEXT NOT NULL,
            contact_id TEXT NOT NULL,
            field TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT,
            source TEXT,
            reverted_at TEXT,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_hs_applied_batch ON hubspot_applied_writes (batch_id)"
    )


def _record_applied_write(
    *,
    batch_id: str,
    contact_id: str,
    field: str,
    old_value: str,
    new_value: str,
    source: str = "",
) -> None:
    """Log what a write replaced, so a batch can be undone.

    A book-wide cleanup writes to hundreds of records in the shared IFG portal.
    Without the prior value there is no undo: "put it back how it was" is
    unanswerable, and the only remedy for a bad batch is opening each contact by
    hand. HubSpot's own property history exists but is per-record and not
    reachable through any tool here.
    """
    from app.storage.lexi_db import get_lexi_connection

    with get_lexi_connection() as conn:
        ensure_applied_writes_table(conn)
        conn.execute(
            """
            INSERT INTO hubspot_applied_writes
                (batch_id, contact_id, field, old_value, new_value, source)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (batch_id, str(contact_id), field, old_value, new_value, source),
        )
        conn.commit()


def revert_hubspot_batch(
    *, batch_id: str, approved: bool = False, force: bool = False
) -> dict[str, Any]:
    """Put every field this batch wrote back to what it was.

    Enrichment only. A merge cannot be undone by anything here — HubSpot itself
    cannot undo one — which is exactly why merges are applied one pair at a time
    and this refuses to pretend otherwise.

    Re-checks before restoring, the same way the apply path does. A batch can sit
    applied for weeks; if someone has since corrected one of these fields by
    hand, an unconditional restore would silently throw their work away and call
    it an undo. Fields that no longer hold what we wrote are left alone and
    reported. ``force`` overrides that, for the case where the batch itself is
    what needs reversing regardless.
    """
    assert_kory_approved_write(approved=approved, action=f"HubSpot rollback of batch {batch_id}")
    from app.storage.lexi_db import get_lexi_connection

    with get_lexi_connection() as conn:
        ensure_applied_writes_table(conn)
        rows = conn.execute(
            "SELECT id, contact_id, field, old_value, new_value FROM hubspot_applied_writes "
            "WHERE batch_id = ? AND reverted_at IS NULL",
            (batch_id,),
        ).fetchall()

    if not rows:
        return {
            "ok": False,
            "batch_id": batch_id,
            "error_code": "nothing_to_revert",
            "kory_message": (
                f"Nothing to undo for batch `{batch_id}` — either it was never applied, "
                "or it has already been rolled back."
            ),
        }

    if hubspot_writes_blocked():
        return {
            "ok": True,
            "dry_run": True,
            "batch_id": batch_id,
            "would_revert": len(rows),
            "kory_message": (
                f"Would put back {len(rows)} field(s) from batch `{batch_id}`. "
                "Nothing written — live HubSpot writes are blocked."
            ),
        }

    # What the records hold right now, so a hand-edit made since the batch was
    # applied is not mistaken for our own write and quietly discarded.
    live_by_id: dict[str, dict[str, Any]] = {}
    if not force:
        try:
            live_by_id = {
                str(c.get("id")): c
                for c in contacts_by_ids(sorted({str(r["contact_id"]) for r in rows}))
            }
        except Exception:
            # Reading the current state is a safety check, not the operation.
            # If HubSpot cannot be read the honest move is to stop, not to
            # restore blind.
            return {
                "ok": False,
                "batch_id": batch_id,
                "error_code": "precheck_failed",
                "kory_message": (
                    f"Couldn't read the current values for batch `{batch_id}`, so I stopped "
                    "rather than restore over the top of whatever is there now."
                ),
            }

    # Group by contact so one record is one API call, not one per field.
    by_contact: dict[str, dict[str, Any]] = {}
    ids_by_contact: dict[str, list[int]] = {}
    changed_since: list[dict[str, Any]] = []
    for row in rows:
        cid = str(row["contact_id"])
        if not force and cid in live_by_id:
            current = str(live_by_id[cid].get(row["field"]) or "").strip()
            written = str(row["new_value"] or "").strip()
            if current != written:
                changed_since.append(
                    {
                        "contact_id": cid,
                        "name": live_by_id[cid].get("name"),
                        "field": row["field"],
                        "now": current,
                        "we_wrote": written,
                    }
                )
                continue
        by_contact.setdefault(cid, {})[row["field"]] = row["old_value"] or ""
        ids_by_contact.setdefault(cid, []).append(row["id"])

    reverted, errors = 0, []
    done_ids: list[int] = []
    for contact_id, props in by_contact.items():
        try:
            _raise_if_refused(
                execute_hubspot_tool(
                    HUBSPOT_UPDATE_CONTACT, {"contactId": contact_id, "properties": props}
                ),
                f"Revert contact {contact_id}",
            )
            reverted += len(props)
            done_ids.extend(ids_by_contact[contact_id])
        except Exception as exc:
            errors.append(f"{contact_id}: {exc}")

    if done_ids:
        with get_lexi_connection() as conn:
            conn.executemany(
                "UPDATE hubspot_applied_writes SET reverted_at = datetime('now') WHERE id = ?",
                [(i,) for i in done_ids],
            )
            conn.commit()

    head = f"Put back {reverted} field(s) from batch `{batch_id}`."
    if errors:
        head += f" {len(errors)} failed."
    lines = [head]
    if changed_since:
        lines.append(
            f"\n**Left {len(changed_since)} alone — edited since I wrote them.** "
            "Undoing would have thrown that work away:"
        )
        for row in changed_since[:5]:
            lines.append(
                f"• {row.get('name') or row['contact_id']} — {row['field']} now reads "
                f"'{row['now']}', I wrote '{row['we_wrote']}'"
            )
        lines.append("_Say 'undo it anyway' and I'll roll those back regardless._")
    return {
        "ok": not errors,
        "batch_id": batch_id,
        "reverted": reverted,
        "skipped_changed_since": changed_since,
        "errors": errors,
        "kory_message": "\n".join(lines),
    }


SETTABLE_FIELDS = ("company", "jobtitle", "phone", "hs_linkedin_url")


def set_contact_field(
    *,
    contact: str,
    field: str,
    value: str,
    approved: bool = False,
    owner_ack: bool = False,
) -> dict[str, Any]:
    """Write one field on one contact because Kory said so.

    Everything else here refuses to write anything it cannot source. This is the
    exception, and the source *is* Kory — which is a better provenance than any
    of the automated tiers, so it is recorded as such rather than dressed up.

    It exists because the enrichment path can now ask questions it could not
    answer. When someone holds three current roles, "tell me which and I'll set
    it" is the right response and was, until this, an empty offer.

    Unlike enrichment this may overwrite a populated field — an explicit
    instruction means the existing value is the thing being corrected. The prior
    value is logged, so it is still one `undo` away.
    """
    key = str(field or "").strip().lower()
    if key not in SETTABLE_FIELDS:
        return {
            "ok": False,
            "error_code": "field_not_settable",
            "kory_message": (
                f"I can set {', '.join(SETTABLE_FIELDS)} — not `{field}`."
            ),
        }
    new_value = str(value or "").strip()
    if not new_value:
        return {
            "ok": False,
            "error_code": "no_value",
            "kory_message": "I need a value to put in that field.",
        }

    needle = str(contact or "").strip()
    if not needle:
        return {"ok": False, "error_code": "no_contact", "kory_message": "Which contact?"}
    matches = contacts_by_ids([needle]) if needle.isdigit() else []
    if not matches:
        found = search_contacts(limit=5, query=needle)
        matches = found.get("contacts") or []
    if not matches:
        return {
            "ok": False,
            "error_code": "not_found",
            "kory_message": f"I couldn't find a contact matching **{needle}**.",
        }
    if len(matches) > 1:
        names = ", ".join(f"{c.get('name')} <{c.get('email')}>" for c in matches[:5])
        return {
            "ok": False,
            "error_code": "ambiguous_contact",
            "matches": matches[:5],
            "kory_message": f"**{needle}** matches more than one contact — {names}. Which one?",
        }

    live = matches[0]
    assert_kory_approved_write(
        approved=approved,
        action=f"Set {key} = '{new_value}' on {live.get('name') or needle} in HubSpot",
    )
    blocked = assert_contact_writable(live, owner_ack=owner_ack)
    if blocked:
        return blocked

    contact_id = str(live.get("id") or "")
    was = str(live.get(key) or "")
    if was.strip() == new_value:
        return {
            "ok": True,
            "changed": False,
            "kory_message": f"**{live.get('name')}** already has {key} set to {new_value}.",
        }

    if hubspot_writes_blocked():
        return {
            "ok": True,
            "dry_run": True,
            "kory_message": (
                f"Would set **{key}** = {new_value} on **{live.get('name')}**"
                + (f" (replacing '{was}')" if was else "")
                + ". Nothing written — live HubSpot writes are blocked."
            ),
        }

    batch_id = f"hs-set-{uuid.uuid4().hex[:12]}"
    _raise_if_refused(
        execute_hubspot_tool(
            HUBSPOT_UPDATE_CONTACT, {"contactId": contact_id, "properties": {key: new_value}}
        ),
        f"Set {key} on contact {contact_id}",
    )
    _record_applied_write(
        batch_id=batch_id,
        contact_id=contact_id,
        field=key,
        old_value=was,
        new_value=new_value,
        source="You told me to set this.",
    )
    return {
        "ok": True,
        "changed": True,
        "batch_id": batch_id,
        "undo_batch_id": batch_id,
        "contact_id": contact_id,
        "field": key,
        "old_value": was,
        "new_value": new_value,
        "kory_message": (
            f"Set **{key}** to {new_value} on **{live.get('name')}**"
            + (f" — it was '{was}'." if was else " — it was blank.")
            + f" Undo with batch `{batch_id}`."
        ),
    }


def _apply_field_fill(
    row: dict[str, Any], *, owner_ack: bool = False, batch_id: str = ""
) -> str:
    """Write proposed fields, re-checking at apply time that they are still blank.

    A batch can sit staged for days; if Kory filled the field himself in the
    meantime, his value wins. Blank-only is enforced here as well as at proposal
    time so a stale batch can never overwrite real data.

    Returns why nothing was written, not just that nothing was — "he already
    filled it in" and "that record is Heidi's" are different facts and the
    caller reports them to him. Only "applied" means HubSpot was written to.
    """
    contact_id = row.get("contact_id")
    props = row.get("proposed_fields") or {}
    if not contact_id or not props:
        return "nothing_proposed"
    live: dict[str, Any] = {}
    current = contacts_by_ids([str(contact_id)])
    if current:
        live = current[0]
        # Enrichment is scoped to Kory's own contacts at proposal time, but the
        # scope is a caller argument and the batch outlives it. Never write to a
        # colleague's record just because a stale row said so.
        if assert_contact_writable(live, owner_ack=owner_ack):
            return "not_kory_owned"
        props = {key: value for key, value in props.items() if _needs_filling(live, key)}
    if not props:
        return "already_filled"
    _raise_if_refused(
        execute_hubspot_tool(
            HUBSPOT_UPDATE_CONTACT,
            {"contactId": contact_id, "properties": props},
        ),
        f"Enrich contact {contact_id}",
    )
    # Recorded only after HubSpot accepted it, so the undo log never claims a
    # write that did not land.
    if batch_id:
        evidence = row.get("evidence") or {}
        for key, value in props.items():
            # Per-field, because a single contact can be filled from HubSpot's
            # own company record and from a signature in the same write.
            per_field = evidence.get(key) or {}
            source = str(
                per_field.get("detail")
                or per_field.get("source")
                or row.get("source")
                or ""
            )[:300]
            _record_applied_write(
                batch_id=batch_id,
                contact_id=str(contact_id),
                field=key,
                old_value=str(live.get(key) or ""),
                new_value=str(value),
                source=source,
            )
    return "applied"


def _draft_outreach_email(*, name: str, goal: str) -> dict[str, str]:
    goal_line = goal.strip() or "catch up and explore whether there is a fit"
    first = name.split()[0] if name else "there"
    subject = f"Quick note — {first}"
    body = (
        f"Hi {first},\n\n"
        f"I wanted to reach out — {goal_line}.\n\n"
        "Would a brief call next week work?\n\n"
        "Best,\nKory"
    )
    return {"subject": subject, "body": body}



def _days_since(raw: str) -> int | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0, (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).days)
    except ValueError:
        return None



def _normalize_contacts(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict) and data.get("dry_run"):
        return []
    rows: list[Any] = []
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        for key in ("results", "contacts", "data", "value"):
            nested = data.get(key)
            if isinstance(nested, list):
                rows = nested
                break
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        props = row.get("properties") if isinstance(row.get("properties"), dict) else row
        email = props.get("email") or row.get("email")
        first = props.get("firstname") or ""
        last = props.get("lastname") or ""
        name = f"{first} {last}".strip() or props.get("name") or ""
        out.append(
            {
                "id": row.get("id") or props.get("hs_object_id"),
                "email": email,
                "name": name,
                "firstname": first,
                "lastname": last,
                "company": props.get("company") or props.get("associatedcompanyid"),
                "jobtitle": props.get("jobtitle"),
                "phone": props.get("phone"),
                "hubspot_owner_id": props.get("hubspot_owner_id"),
                "hs_lead_status": props.get("hs_lead_status"),
                "lifecyclestage": props.get("lifecyclestage"),
                "hs_analytics_source": props.get("hs_analytics_source"),
                "hs_linkedin_url": props.get("hs_linkedin_url"),
                "lead_source": props.get("hs_analytics_source") or props.get("lead_source"),
                "createdate": props.get("createdate"),
                "lastmodifieddate": props.get("lastmodifieddate") or props.get("notes_last_updated"),
                "last_activity": props.get("notes_last_contacted") or props.get("lastmodifieddate"),
                "notes_last_contacted": props.get("notes_last_contacted"),
            }
        )
    return out


def _normalize_deals(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict) and data.get("dry_run"):
        return []
    rows: list[Any] = []
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        for key in ("results", "deals", "data", "value"):
            nested = data.get(key)
            if isinstance(nested, list):
                rows = nested
                break
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        props = row.get("properties") if isinstance(row.get("properties"), dict) else row
        out.append(
            {
                "id": row.get("id"),
                "dealname": props.get("dealname") or props.get("name"),
                "dealstage": props.get("dealstage") or props.get("stage"),
                "amount": props.get("amount"),
                "closedate": props.get("closedate"),
            }
        )
    return out


def _stage_hubspot_batch(*, batch_type: str, payload: dict[str, Any]) -> str:
    from app.storage.lexi_db import get_lexi_connection

    batch_id = f"hs-{uuid.uuid4().hex[:12]}"
    with get_lexi_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS hubspot_batches (
                batch_id TEXT PRIMARY KEY,
                batch_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            INSERT INTO hubspot_batches (batch_id, batch_type, payload)
            VALUES (?, ?, ?)
            """,
            (batch_id, batch_type, json.dumps(payload, default=str)),
        )
        conn.commit()
    return batch_id


def _load_hubspot_batch(batch_id: str) -> dict[str, Any] | None:
    from app.storage.lexi_db import get_lexi_connection

    with get_lexi_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS hubspot_batches (
                batch_id TEXT PRIMARY KEY,
                batch_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        row = conn.execute(
            "SELECT batch_id, batch_type, payload FROM hubspot_batches WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
    if not row:
        return None
    payload = json.loads(row["payload"])
    return {
        "batch_id": row["batch_id"],
        "batch_type": row["batch_type"],
        "payload": payload,
    }
