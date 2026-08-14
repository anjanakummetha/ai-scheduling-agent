"""Resolve a contact's employer from facts HubSpot already holds.

None of this infers anything. Every answer is a value a human already entered
into the CRM, retrieved by an exact match — the point is to fill gaps without
introducing a single thing that could be wrong.

Four sources, in descending order of directness:

* **association** — the contact is already linked to a Company object. HubSpot
  has literally recorded where this person works; the contact-level `company`
  property just happens to be blank or junk.
* **domain object** — a Company object whose `domain` equals the contact's email
  domain. Exact string match on a field a human filled in, and measured
  unambiguous on Kory's portal (one Company object per domain, never two).
* **book consensus** — every other contact of Kory's at that domain agrees on
  one spelling of the employer. Evidence from his own data.
* **the company's own website** (opt-in) — the business stating its own name on
  its own homepage, accepted only when that name corroborates the domain the
  address came from. Two facts that already agree, not a search result about a
  person.

Explicitly *not* a source: any web search about the **individual**. A wrong
title on a real human in a shared CRM reads as authoritative and nobody
re-checks it. Those go to Kory as a list to look at, never as a staged write.

Explicitly *not* a source: turning the domain string into a name.
`acme-holdings.com` → "Acme Holdings" guesses at legal name, capitalisation and
punctuation, and a guess written into the shared CRM reads as authoritative.

Free-mail domains are excluded everywhere. `gmail.com` says nothing about an
employer, and a Company object that happens to carry it would be catastrophic —
it would name one employer for every personal address in the book.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from html import unescape
from typing import Any

from app.integrations.composio_client import execute_hubspot_tool

HUBSPOT_SEARCH_COMPANIES = "HUBSPOT_SEARCH_COMPANIES"
HUBSPOT_LIST_ASSOCIATIONS = "HUBSPOT_LIST_OBJECT_ASSOCIATIONS"
HUBSPOT_BATCH_READ_COMPANIES = "HUBSPOT_BATCH_READ_COMPANIES_BY_PROPERTIES"

# Consumer mailboxes. A shared domain here is not a shared employer.
FREE_MAIL_DOMAINS = frozenset(
    {
        "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "outlook.com",
        "hotmail.com", "live.com", "msn.com", "icloud.com", "me.com", "mac.com",
        "aol.com", "comcast.net", "verizon.net", "att.net", "sbcglobal.net",
        "protonmail.com", "proton.me", "gmx.com", "mail.com", "zoho.com",
        "cox.net", "charter.net", "earthlink.net", "bellsouth.net", "pm.me",
    }
)

# How many of Kory's own contacts must agree before a domain mapping counts as
# evidence rather than coincidence. Two people at the same domain writing the
# same employer is a pattern; one is a single data point that may itself be the
# typo we are trying to avoid propagating.
MIN_CONSENSUS = 2

_domain_company_cache: dict[str, str | None] = {}


def reset_cache() -> None:
    _domain_company_cache.clear()


def domain_of(email: Any) -> str:
    domain = str(email or "").strip().lower().rpartition("@")[2]
    return domain if "." in domain else ""


def is_corporate_domain(email: Any) -> bool:
    domain = domain_of(email)
    return bool(domain) and domain not in FREE_MAIL_DOMAINS


def _normalize(name: str) -> str:
    """Compare company names ignoring punctuation and case, never for display."""
    return re.sub(r"[^a-z0-9]+", " ", str(name or "").lower()).strip()


def build_domain_consensus(
    contacts: list[dict[str, Any]], *, is_placeholder=None, min_agreeing: int = MIN_CONSENSUS
) -> dict[str, str]:
    """Map corporate domain -> the one employer Kory's own contacts agree on.

    Disagreement disqualifies the domain outright. If half his contacts at a
    domain say "Alvarez & Marsal" and half say "A&M Capital", we do not get to
    pick; both are plausible and writing either would be inventing a fact.
    """
    by_domain: dict[str, Counter] = defaultdict(Counter)
    for contact in contacts:
        domain = domain_of(contact.get("email"))
        company = str(contact.get("company") or "").strip()
        if not domain or domain in FREE_MAIL_DOMAINS or not company:
            continue
        if is_placeholder and is_placeholder(company, field="company"):
            continue
        by_domain[domain][company] += 1

    consensus: dict[str, str] = {}
    for domain, counter in by_domain.items():
        if len({_normalize(v) for v in counter}) != 1:
            continue  # they disagree — not evidence
        # Count agreement on the *normalised* name, not the exact string:
        # "BOK Financial" and "BOK Financial." are two contacts agreeing, and
        # tallying raw spellings would score them one apiece and discard both.
        total = sum(counter.values())
        if total >= min_agreeing:
            consensus[domain] = counter.most_common(1)[0][0]
    return consensus


def company_from_association(contact_id: str) -> dict[str, Any] | None:
    """The Company object HubSpot has already linked this contact to."""
    if not str(contact_id or "").strip():
        return None
    try:
        assoc = execute_hubspot_tool(
            HUBSPOT_LIST_ASSOCIATIONS,
            {"objectType": "contacts", "objectId": str(contact_id), "toObjectType": "companies"},
        )
        ids = [
            str(row.get("toObjectId"))
            for row in (assoc.get("data") or {}).get("results") or []
            if row.get("toObjectId")
        ]
    except Exception:
        return None
    if len(ids) != 1:
        # Two associated companies is a question for Kory, not a coin toss.
        return None
    try:
        read = execute_hubspot_tool(
            HUBSPOT_BATCH_READ_COMPANIES,
            {"inputs": [{"id": ids[0]}], "properties": ["name", "domain"]},
        )
        rows = (read.get("data") or {}).get("results") or []
    except Exception:
        return None
    if not rows:
        return None
    name = str((rows[0].get("properties") or {}).get("name") or "").strip()
    if not name:
        # Measured on Kory's portal: several Company objects carry a domain and
        # no name. An unnamed company is not an answer.
        return None
    return {
        "value": name,
        "source": "hubspot_company_association",
        "evidence": f"HubSpot already links this contact to company #{ids[0]} ({name}).",
        "company_id": ids[0],
    }


def company_from_domain_object(email: Any) -> dict[str, Any] | None:
    """A Company object whose domain is exactly this contact's email domain."""
    domain = domain_of(email)
    if not domain or domain in FREE_MAIL_DOMAINS:
        return None
    if domain in _domain_company_cache:
        cached = _domain_company_cache[domain]
        if not cached:
            return None
        return {
            "value": cached,
            "source": "hubspot_company_domain",
            "evidence": f"HubSpot has a company record for {domain} named {cached}.",
        }
    try:
        result = execute_hubspot_tool(
            HUBSPOT_SEARCH_COMPANIES,
            {
                "limit": 3,
                "properties": ["name", "domain"],
                "filterGroups": [
                    {"filters": [{"propertyName": "domain", "operator": "EQ", "value": domain}]}
                ],
            },
        )
        rows = (result.get("data") or {}).get("results") or []
    except Exception:
        return None
    names = sorted({
        str((row.get("properties") or {}).get("name") or "").strip()
        for row in rows
        if str((row.get("properties") or {}).get("name") or "").strip()
    })
    # Exactly one, or we do not know which.
    resolved = names[0] if len(names) == 1 else None
    _domain_company_cache[domain] = resolved
    if not resolved:
        return None
    return {
        "value": resolved,
        "source": "hubspot_company_domain",
        "evidence": f"HubSpot has a company record for {domain} named {resolved}.",
    }


_GENERIC_SITE_TITLES = frozenset(
    {
        "home", "homepage", "welcome", "index", "untitled", "site", "website",
        "coming soon", "under construction", "default", "landing page", "main",
        "about", "about us", "contact", "contact us", "login", "sign in",
    }
)


def _company_name_from_title(title: str, domain: str) -> str:
    """A company name out of a page title, or "" when it cannot be trusted.

    Site titles are usually "Sky Group | Commercial Roofing" — the name, then a
    tagline. Taking the first segment gets the name; the corroboration check
    below is what makes it a fact rather than a guess.
    """
    raw = re.sub(r"\s+", " ", str(title or "")).strip()
    if not raw:
        return ""
    # A literal escape sequence means something upstream serialised the text
    # instead of reading it. Whatever this is, it is not a company name — and
    # this is the belt to the extraction fix's braces.
    if re.search(r"\\[uUxN]", raw):
        return ""
    # First segment before a separator, which is conventionally the name.
    candidate = re.split(r"\s*[|–—•·>»:]\s*|\s+-\s+", raw, maxsplit=1)[0].strip(" .,-")
    if not candidate or not (2 < len(candidate) <= 60):
        return ""
    if candidate.lower() in _GENERIC_SITE_TITLES:
        return ""
    if _normalize(candidate) in _GENERIC_SITE_TITLES:
        return ""

    # Corroboration: the name and the domain must actually be about each other.
    # "Sky Group" vs skygroup-co.com agrees; a title lifted from an ad network or
    # a parked-domain page does not, and that is exactly what must not be written.
    name_key = re.sub(r"[^a-z0-9]", "", candidate.lower())
    domain_key = re.sub(r"[^a-z0-9]", "", domain.rpartition(".")[0].lower())
    if not name_key or not domain_key:
        return ""
    if name_key in domain_key or domain_key in name_key:
        return candidate
    # Or the domain is an acronym/abbreviation of the words in the name.
    words = [w for w in re.split(r"[^a-z0-9]+", candidate.lower()) if w]
    if words and (domain_key.startswith(words[0]) or "".join(w[0] for w in words) == domain_key):
        return candidate
    return ""


def company_from_website(email: Any) -> dict[str, Any] | None:
    """Read the company's own site for its own name.

    A business stating its name on its own homepage is not a guess about it, and
    the domain came from the contact's address — so this answers "who do they
    work for" with two facts that already agree. The name is only accepted when
    it corroborates the domain, so a parked page or an unrelated title yields
    nothing rather than something plausible.

    Deliberately not a general web search about the *person*: that is where a
    wrong answer becomes a wrong fact about a real human on a shared CRM.
    """
    domain = domain_of(email)
    if not domain or domain in FREE_MAIL_DOMAINS:
        return None
    cache_key = f"web::{domain}"
    if cache_key in _domain_company_cache:
        cached = _domain_company_cache[cache_key]
        if not cached:
            return None
        return {
            "value": cached,
            "source": "company_website",
            "evidence": f"{domain} identifies itself as {cached} on its own site.",
        }

    name = ""
    try:
        from app.integrations.composio_search import fetch_url_content, search_enabled

        if not search_enabled():
            return None
        result = fetch_url_content([f"https://{domain}"], max_characters=3000)
        payload = result.get("data")

        # Read the field, don't grep the serialisation. json.dumps escapes
        # non-ASCII, so an em-dash in a site title arrived as the six literal
        # characters of its escape sequence. The separator never matched, the
        # tagline was never split off, and "<Name> <escape> Growth is a system"
        # was one approval away from being written as somebody's employer.
        titles: list[str] = []
        if isinstance(payload, dict):
            for row in payload.get("results") or []:
                if not isinstance(row, dict):
                    continue
                for key in ("title", "siteName", "site_name", "og:site_name"):
                    value = row.get(key)
                    if isinstance(value, str) and value.strip():
                        titles.append(value)
        for title in titles:
            name = _company_name_from_title(unescape(title), domain)
            if name:
                break

        # Fall back to scanning the payload, with escaping turned off so the
        # text matched is the text that was served.
        blob = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        for pattern in () if name else (
            r'"(?:og:site_name|site_name|siteName)"\s*[:=]\s*"([^"]{2,80})"',
            r"<title[^>]*>([^<]{2,120})</title>",
            r'"title"\s*:\s*"([^"]{2,120})"',
        ):
            match = re.search(pattern, blob, re.I)
            if not match:
                continue
            name = _company_name_from_title(unescape(match.group(1)), domain)
            if name:
                break
    except Exception:
        name = ""

    _domain_company_cache[cache_key] = name or None
    if not name:
        return None
    return {
        "value": name,
        "source": "company_website",
        "evidence": f"{domain} identifies itself as {name} on its own site.",
    }


def resolve_company(
    contact: dict[str, Any],
    *,
    consensus: dict[str, str] | None = None,
    use_website: bool = False,
) -> dict[str, Any] | None:
    """Best available employer for this contact, or None. Never a guess.

    Association first: it is about *this person*, not about their domain, so it
    survives the case where someone uses a company address but works elsewhere.
    """
    found = company_from_association(str(contact.get("id") or ""))
    if found:
        return found

    found = company_from_domain_object(contact.get("email"))
    if found:
        return found

    domain = domain_of(contact.get("email"))
    if consensus and domain in consensus:
        return {
            "value": consensus[domain],
            "source": "kory_book_consensus",
            "evidence": (
                f"Every other contact of yours at {domain} is filed under "
                f"{consensus[domain]}."
            ),
        }

    # Last, because it is the only one that leaves the building.
    if use_website:
        return company_from_website(contact.get("email"))
    return None
