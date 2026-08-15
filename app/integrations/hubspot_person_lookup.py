"""Resolve a contact's job title from their LinkedIn profile, or refuse.

The other enrichment tiers answer "who does this person work for" from facts
HubSpot already holds. This one is different in kind: it leaves the building and
looks up an individual, which is exactly the thing
:mod:`hubspot_company_lookup` refuses to do for company names.

It is only safe because of an inversion. **It never asks "what is this person's
job title?"** — a wrong answer to that question is plausible, unfalsifiable, and
reads as authoritative once it is sitting in the shared IFG CRM. It asks:

    Does this profile show a role at the employer we *already know* they work
    for, and if so, what is that role called?

A wrong profile cannot pass that check, because a stranger who happens to share
the name does not also happen to work at the same company. The employer is not
the thing being learned — it is the key the answer has to fit.

Measured on Kory's real book (probe, 2026-08-14): a candidate LinkedIn profile
was found for 8 of 8 contacts. Finding profiles is easy. Six of those eight were
the wrong human or not a human at all, which is what this module exists to
catch:

* **Chris Gavora** — profile said CFO at Bockmann Inc., HubSpot said Three
  Shadows. There are genuinely two Chris Gavoras in this database and one of
  them was one approval away from getting the other's job title.
* **Bianca Martins** — matched a tech recruiter in Brazil.
* **"Dunes Point Capital Team"**, **"Exuma Funds General Mailbox"** — shared
  mailboxes, not people. Both resolved to real executives.
* **Chris Lefkovitz** — genuinely correct, corroborated at Leftbank Holdings,
  and refused by an earlier draft purely because the CRM says "Chris" and the
  profile says "Christopher".
* **Vincent Flaska** — real person, but the most recent open-ended role on his
  profile was *"Member, YPO Chicago Chapter"*. A membership is not a job, and
  taking "the current role" would have written it into his title field.

Nothing here writes. Every return value is a proposal carrying the URL it came
from and the employer that corroborated it, so a human reviewing in Teams can
see what it matched on rather than watching a value appear.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any

from app.integrations.hubspot_company_lookup import (
    FREE_MAIL_DOMAINS,
    domain_of,
)

# linkedin.com/in/<slug>, with or without a country subdomain.
_PROFILE_RE = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[A-Za-z0-9\-_%]+", re.I)

# How many candidate profiles to open per contact. Trying more only ever finds
# more *true* positives -- a wrong profile still has to clear the employer check
# -- but each one is a network round trip, so this is a time budget, not a
# safety limit.
MAX_CANDIDATES = 2

# Local-parts that are a function, not a person. Writing a job title onto
# accounting@ makes the record look like a human being.
_ROLE_MAILBOXES = frozenset(
    {
        "info", "sales", "support", "admin", "contact", "hello", "office",
        "accounting", "billing", "ap", "ar", "accountspayable", "hr", "careers",
        "jobs", "team", "mail", "inquiries", "enquiries", "service", "help",
        "noreply", "no-reply", "donotreply", "marketing", "press", "media",
        "orders", "invoices", "payroll", "reception", "frontdesk", "general",
    }
)

# Words that make a *name* a mailbox or a group rather than a person. Matched as
# whole words against the name, so "Teamster" and "Grouper" are safe.
_NON_PERSON_WORDS = frozenset(
    {
        "team", "mailbox", "inbox", "group", "dept", "department", "office",
        "general", "accounting", "billing", "payroll", "admin", "info",
        "support", "sales", "hr", "careers", "reception", "desk", "committee",
        "board", "llc", "inc", "corp", "ltd", "company", "partners", "holdings",
        "capital", "associates", "fund", "funds", "trust", "staff", "everyone",
    }
)

# Irregular short forms -- the ones a prefix check cannot catch. "chris" ->
# "christopher" needs no entry because it is a prefix; "bob" -> "robert" does.
_NICKNAMES: dict[str, frozenset[str]] = {
    "bob": frozenset({"robert"}), "bobby": frozenset({"robert"}),
    "rob": frozenset({"robert"}), "robbie": frozenset({"robert"}),
    "bill": frozenset({"william"}), "billy": frozenset({"william"}),
    "will": frozenset({"william"}), "willie": frozenset({"william"}),
    "dick": frozenset({"richard"}), "rick": frozenset({"richard"}),
    "ricky": frozenset({"richard"}), "rich": frozenset({"richard"}),
    "mike": frozenset({"michael"}), "mickey": frozenset({"michael"}),
    "dave": frozenset({"david"}), "davy": frozenset({"david"}),
    "jack": frozenset({"john", "jackson"}), "johnny": frozenset({"john"}),
    "chuck": frozenset({"charles"}), "charlie": frozenset({"charles"}),
    "hank": frozenset({"henry"}), "harry": frozenset({"henry", "harold"}),
    "jim": frozenset({"james"}), "jimmy": frozenset({"james"}),
    "jamie": frozenset({"james"}),
    "tony": frozenset({"anthony"}), "andy": frozenset({"andrew"}),
    "drew": frozenset({"andrew"}), "ted": frozenset({"edward", "theodore"}),
    "ned": frozenset({"edward"}), "teddy": frozenset({"theodore"}),
    "larry": frozenset({"lawrence", "laurence"}),
    "gerry": frozenset({"gerald"}), "jerry": frozenset({"gerald", "jerome"}),
    "peggy": frozenset({"margaret"}), "meg": frozenset({"margaret"}),
    "maggie": frozenset({"margaret"}), "midge": frozenset({"margaret"}),
    "betsy": frozenset({"elizabeth"}), "beth": frozenset({"elizabeth"}),
    "liz": frozenset({"elizabeth"}), "lizzie": frozenset({"elizabeth"}),
    "sue": frozenset({"susan"}), "susie": frozenset({"susan"}),
    "kathy": frozenset({"katherine", "kathleen"}),
    "kate": frozenset({"katherine"}), "katie": frozenset({"katherine"}),
    "trish": frozenset({"patricia"}), "tricia": frozenset({"patricia"}),
    "patty": frozenset({"patricia"}), "pat": frozenset({"patricia", "patrick"}),
    "becky": frozenset({"rebecca"}), "cindy": frozenset({"cynthia"}),
    "debbie": frozenset({"deborah"}), "deb": frozenset({"deborah"}),
    "barb": frozenset({"barbara"}), "pam": frozenset({"pamela"}),
    "sandy": frozenset({"sandra"}), "mandy": frozenset({"amanda"}),
    "vicki": frozenset({"victoria"}), "vicky": frozenset({"victoria"}),
    "angie": frozenset({"angela"}), "abby": frozenset({"abigail"}),
    "connie": frozenset({"constance"}), "dot": frozenset({"dorothy"}),
    "fran": frozenset({"frances", "francis"}), "gina": frozenset({"regina"}),
    "nan": frozenset({"nancy"}), "tina": frozenset({"christina"}),
    "tom": frozenset({"thomas"}), "tommy": frozenset({"thomas"}),
    "steve": frozenset({"stephen", "steven"}),
    "joe": frozenset({"joseph"}), "joey": frozenset({"joseph"}),
    "ken": frozenset({"kenneth"}), "kenny": frozenset({"kenneth"}),
    "ron": frozenset({"ronald"}), "ronnie": frozenset({"ronald"}),
    "russ": frozenset({"russell"}), "walt": frozenset({"walter"}),
    "art": frozenset({"arthur"}), "fred": frozenset({"frederick", "alfred"}),
    "phil": frozenset({"philip", "phillip"}), "ray": frozenset({"raymond"}),
    "vince": frozenset({"vincent"}), "stan": frozenset({"stanley"}),
    "doug": frozenset({"douglas"}), "greg": frozenset({"gregory"}),
    "nick": frozenset({"nicholas"}), "sam": frozenset({"samuel", "samantha"}),
    "ben": frozenset({"benjamin"}), "matt": frozenset({"matthew"}),
    "dan": frozenset({"daniel"}), "danny": frozenset({"daniel"}),
    "jeff": frozenset({"jeffrey", "geoffrey"}), "jon": frozenset({"jonathan"}),
    "alex": frozenset({"alexander", "alexandra"}),
    "max": frozenset({"maxwell", "maximilian"}),
    "gabe": frozenset({"gabriel"}), "nate": frozenset({"nathan", "nathaniel"}),
    "zach": frozenset({"zachary"}), "josh": frozenset({"joshua"}),
    "tim": frozenset({"timothy"}), "cal": frozenset({"calvin"}),
    "al": frozenset({"albert", "alan", "allen", "alfred"}),
}

# Roles that describe a *membership or affiliation*, not employment. Vincent
# Flaska's most recent open-ended entry was "Member, YPO Chicago Chapter" -- YPO
# is a peer network, not his employer, and it would have become his job title.
_NON_EMPLOYMENT_TITLES = re.compile(
    r"^(?:member|mentor|advisor|adviser|volunteer|alumni|alumnus|alumna|"
    r"fellow|ambassador|contributor|speaker|guest|participant|student|"
    r"candidate|attendee|subscriber|follower|supporter)\b",
    re.I,
)

_profile_cache: dict[str, dict[str, Any] | None] = {}
_search_cache: dict[str, list[str]] = {}


def reset_cache() -> None:
    _profile_cache.clear()
    _search_cache.clear()


# --- identity ---------------------------------------------------------------


def _tokens(name: Any) -> list[str]:
    """Name words, lowercased, punctuation dropped, suffixes removed."""
    raw = re.sub(r"[^a-z\s'\-]", " ", str(name or "").lower())
    parts = [p.strip("'-") for p in raw.split() if p.strip("'-")]
    return [p for p in parts if p not in {"jr", "sr", "ii", "iii", "iv", "md", "phd", "cpa", "esq"}]


def non_person_reason(name: Any, email: Any = "") -> str:
    """Why this record is not an individual, or "" if it looks like one.

    A shared mailbox must never receive a job title. Both mailbox records in the
    probe resolved to a real executive at the right company -- the employer
    check passes, because the mailbox genuinely belongs to that company. Only a
    name check stops it.
    """
    local = str(email or "").strip().lower().rpartition("@")[0].replace(".", "").replace("_", "")
    if local and local in _ROLE_MAILBOXES:
        return f"{local}@ is a role mailbox, not a person"

    parts = _tokens(name)
    if not parts:
        return "no name on the record"
    hits = sorted(_NON_PERSON_WORDS.intersection(parts))
    if hits:
        return f"the name contains {hits[0]!r} — this reads as a mailbox or group, not a person"
    if len(parts) < 2:
        return "only one name word — cannot tell one person from another"
    return ""


def _first_names_match(a: str, b: str) -> bool:
    if a == b:
        return True
    # Prefix covers most short forms: chris/christopher, matt/matthew, tim/timothy.
    short, long = sorted((a, b), key=len)
    if len(short) >= 3 and long.startswith(short):
        return True
    # And an explicit table for the ones it cannot: bob/robert, peggy/margaret.
    return bool(
        (_NICKNAMES.get(a, frozenset()) & ({b} | _NICKNAMES.get(b, frozenset())))
        or (_NICKNAMES.get(b, frozenset()) & {a})
    )


def _edit_distance_one(a: str, b: str) -> bool:
    """True when one edit turns a into b. Used only for surnames, only long ones."""
    if abs(len(a) - len(b)) > 1:
        return False
    if a == b:
        return False
    short, long = sorted((a, b), key=len)
    i = j = 0
    edited = False
    while i < len(short) and j < len(long):
        if short[i] != long[j]:
            if edited:
                return False
            edited = True
            if len(short) == len(long):
                i += 1
            j += 1
            continue
        i += 1
        j += 1
    return True


def compare_names(crm_name: Any, profile_name: Any) -> dict[str, Any]:
    """Whether these are the same human, and how confident that is.

    Compares first and last only -- profiles carry middle names the CRM does not
    ("Timothy J. White"), and a middle name is never what distinguishes two
    people in this database.
    """
    crm, prof = _tokens(crm_name), _tokens(profile_name)
    if len(crm) < 2 or len(prof) < 2:
        return {"match": False, "reason": "not enough name to compare"}

    if not _first_names_match(crm[0], prof[0]):
        return {"match": False, "reason": f"first name {crm[0]!r} is not {prof[0]!r}"}

    if crm[-1] == prof[-1]:
        return {"match": True, "note": ""}

    # A one-character surname difference on a long name is a typo, not a
    # different family -- HubSpot has "Pelligrino" where LinkedIn has
    # "Pellegrino". Accepted, but surfaced: it is also a spelling error worth
    # fixing, and a reviewer should see what was matched.
    if len(crm[-1]) >= 6 and _edit_distance_one(crm[-1], prof[-1]):
        return {
            "match": True,
            "note": (
                f"HubSpot spells the surname {crm[-1].title()!r}, LinkedIn "
                f"{prof[-1].title()!r} — one character apart, likely a typo in the CRM"
            ),
        }
    return {"match": False, "reason": f"surname {crm[-1]!r} is not {prof[-1]!r}"}


# --- employer corroboration -------------------------------------------------


def _key(text: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


_COMPANY_NOISE = re.compile(
    r"\b(?:inc|llc|l\.l\.c|ltd|limited|co|corp|corporation|company|group|"
    r"holdings|partners|lp|llp|plc|pllc|the|and|of)\b",
    re.I,
)


def _company_key(name: Any) -> str:
    """Company name reduced to its distinctive part, for comparison only."""
    stripped = _COMPANY_NOISE.sub(" ", str(name or ""))
    return _key(stripped) or _key(name)


def employer_matches(profile_company: Any, *, known_company: str = "", domain: str = "") -> str:
    """How this profile employer corroborates what we already knew, or "".

    Two independent routes, either of which is enough: the name we already hold,
    or the domain the contact's email came from. The domain route is the reason
    "Holmes Murphy & Associates" can corroborate holmesmurphy.com.
    """
    prof_key = _company_key(profile_company)
    if not prof_key:
        return ""

    if known_company:
        known_key = _company_key(known_company)
        if known_key and (known_key == prof_key or known_key in prof_key or prof_key in known_key):
            return f"their profile lists {str(profile_company).strip()}, which is the employer already on the record"

    if domain and domain not in FREE_MAIL_DOMAINS:
        domain_key = _key(domain.rpartition(".")[0])
        if domain_key and (domain_key in prof_key or prof_key in domain_key):
            return f"their profile lists {str(profile_company).strip()}, which matches their email domain {domain}"
        # Or the domain is an acronym of the company words.
        words = [w for w in re.split(r"[^a-z0-9]+", _COMPANY_NOISE.sub(" ", str(profile_company)).lower()) if w]
        if words and "".join(w[0] for w in words) == domain_key:
            return f"their profile lists {str(profile_company).strip()}, whose initials are their email domain {domain}"
    return ""


def _ended(dates: Any) -> date | None:
    raw = (dates or {}).get("to") if isinstance(dates, dict) else None
    if not raw:
        return None
    try:
        return datetime.strptime(str(raw)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def role_at_known_employer(
    entity: dict[str, Any], *, known_company: str = "", domain: str = "", today: date | None = None
) -> dict[str, Any] | None:
    """The role this person holds at the employer we already knew, or None.

    This is both the corroboration and the answer: we do not find the profile,
    then separately ask what they do. We look for the one role that proves the
    profile is the right person, and that role's title is the fill.
    """
    today = today or date.today()
    best: dict[str, Any] | None = None
    for work in entity.get("workHistory") or []:
        if not isinstance(work, dict):
            continue
        company = (work.get("company") or {}).get("name") if isinstance(work.get("company"), dict) else None
        title = str(work.get("title") or "").strip()
        if not company or not title:
            continue
        corroboration = employer_matches(company, known_company=known_company, domain=domain)
        if not corroboration:
            continue
        if _NON_EMPLOYMENT_TITLES.match(title):
            # A membership at the right organisation still is not a job.
            continue
        ended = _ended(work.get("dates"))
        candidate = {
            "title": title,
            "company": str(company).strip(),
            "corroboration": corroboration,
            "ended": ended.isoformat() if ended else "",
            "current": ended is None or ended >= today,
        }
        # A role they still hold beats one they have left.
        if best is None or (candidate["current"] and not best["current"]):
            best = candidate
    return best


# --- finding and fetching ---------------------------------------------------


def find_profile_urls(name: str, anchor: str) -> list[str]:
    """Candidate LinkedIn profile URLs for this person at this employer."""
    query = f"{name} {anchor} linkedin".strip()
    if query in _search_cache:
        return _search_cache[query]
    urls: list[str] = []
    try:
        from app.integrations.composio_search import search_enabled, web_search

        if search_enabled():
            found = web_search(query)
            seen: set[str] = set()
            for url in _PROFILE_RE.findall(json.dumps(found, ensure_ascii=False)):
                clean = url.rstrip("/")
                key = clean.lower().rpartition("/in/")[2]
                if key and key not in seen:
                    seen.add(key)
                    urls.append(clean)
    except Exception:
        urls = []
    _search_cache[query] = urls
    return urls


def fetch_person(url: str) -> dict[str, Any] | None:
    """The structured person record behind a LinkedIn URL, or None.

    Deliberately the *entity*, not a written answer. An answer endpoint would
    happily compose a plausible job title out of nothing; an entity is a record
    with fields that are either present or absent.
    """
    key = url.lower().rstrip("/")
    if key in _profile_cache:
        return _profile_cache[key]
    entity: dict[str, Any] | None = None
    try:
        from app.integrations.composio_search import fetch_url_content, search_enabled

        if search_enabled():
            payload = (fetch_url_content(url, max_characters=4000) or {}).get("data") or {}
            for row in payload.get("results") or []:
                if not isinstance(row, dict):
                    continue
                for candidate in row.get("entities") or []:
                    props = (candidate or {}).get("properties") or {}
                    if props.get("name") and props.get("workHistory"):
                        entity = props
                        break
                if entity:
                    break
    except Exception:
        entity = None
    _profile_cache[key] = entity
    return entity


# --- the whole thing --------------------------------------------------------


def resolve_person(
    contact: dict[str, Any],
    *,
    known_company: str = "",
    max_candidates: int = MAX_CANDIDATES,
    today: date | None = None,
) -> dict[str, Any] | None:
    """A defensible job title for this contact, or None. Never a guess.

    Returns ``{"fields": {...}, "evidence": {...}}`` shaped for the enrichment
    proposal path, or a ``{"skip": reason}`` marker for records that are not
    people -- those are a finding in their own right, not a failed lookup.
    """
    name = str(contact.get("name") or "").strip()
    email = str(contact.get("email") or "").strip()
    domain = domain_of(email)

    blocked = non_person_reason(name, email)
    if blocked:
        return {"skip": "not_a_person", "reason": blocked}

    anchor = known_company or (domain if domain and domain not in FREE_MAIL_DOMAINS else "")
    if not anchor:
        # Nothing to check an answer against. A personal address with no
        # employer on file is exactly the case where a confident wrong title
        # would sail straight through.
        return None

    for url in find_profile_urls(name, anchor)[:max_candidates]:
        entity = fetch_person(url)
        if not entity:
            continue
        verdict = compare_names(name, entity.get("name"))
        if not verdict.get("match"):
            continue
        role = role_at_known_employer(
            entity, known_company=known_company, domain=domain, today=today
        )
        if not role:
            continue
        if not role["current"]:
            # They have left. Writing the old title would make a stale record
            # look freshly confirmed, which is worse than the blank.
            return {
                "skip": "may_have_moved",
                "reason": (
                    f"{name} appears to have left {role['company']} "
                    f"(profile shows the role ending {role['ended']}). "
                    "Worth a look rather than a fill."
                ),
                "profile_url": url,
            }

        detail = f"{role['corroboration']} ({url})."
        if verdict.get("note"):
            detail += f" {verdict['note']}."
        fields = {"jobtitle": role["title"]}
        evidence = {
            "jobtitle": {
                "source": "linkedin_profile_corroborated",
                "detail": detail,
                "confidence": "corroborated",
                "profile_url": url,
            }
        }
        # The employer only counts as a *fill* when we matched on the domain --
        # if we matched on a company name we already had, there is no gap.
        if not known_company and role["company"]:
            fields["company"] = role["company"]
            evidence["company"] = dict(evidence["jobtitle"])
        # Free improvement: the URL we just proved belongs to this person. It
        # also feeds Sales Navigator's own matching, which is a pleasant irony.
        if not str(contact.get("hs_linkedin_url") or "").strip():
            fields["hs_linkedin_url"] = url
            evidence["hs_linkedin_url"] = {
                "source": "linkedin_profile_corroborated",
                "detail": f"Profile confirmed by their role at {role['company']}.",
                "confidence": "corroborated",
                "profile_url": url,
            }
        return {"fields": fields, "evidence": evidence, "profile_url": url}
    return None
