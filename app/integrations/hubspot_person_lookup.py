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
# whole words, so "Teamster" and "Grouper" are safe.
#
# Deliberately only words that are never a surname. An earlier draft included
# group, capital, partners, holdings, board, fund, trust and desk — every one of
# those is somebody's actual last name, and blocking Sarah Board from enrichment
# because "board" looks organisational is a worse failure than missing a
# mailbox. Both real cases here are caught by the strong words anyway: "Dunes
# Point Capital Team" by *team*, "Exuma Funds General Mailbox" by *mailbox*.
_NON_PERSON_WORDS = frozenset(
    {
        "team", "mailbox", "inbox", "dept", "department", "accounting",
        "billing", "payroll", "reception", "committee", "llc", "inc", "corp",
        "ltd", "everyone", "noreply", "no-reply", "donotreply", "enquiries",
        "inquiries", "helpdesk", "postmaster", "webmaster",
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


# Letters people append to (and occasionally prepend to) their own name. Found
# live: HubSpot holds "Jason Buesing PE", and the profile for James Hite comes
# back as "CRIS James Hite" — his construction-risk credential parsed into the
# first-name field. Left in place, both read as a different human.
_CREDENTIALS = frozenset(
    {
        "jr", "sr", "ii", "iii", "iv", "v",
        "md", "phd", "cpa", "esq", "pe", "cris", "pmp", "mba", "jd", "cfa",
        "rn", "leed", "aia", "pls", "cfp", "clu", "chfc", "arm", "cic", "crm",
        "cpcu", "aic", "se", "pls", "gri", "ccim", "sior", "cbi", "ea", "cma",
        "cissp", "msn", "dds", "dvm", "do", "od", "lutcf", "cebs", "shrm",
    }
)


def _tokens(name: Any) -> list[str]:
    """Name words, lowercased, punctuation dropped, credentials removed."""
    raw = re.sub(r"[^a-z\s'\-]", " ", str(name or "").lower())
    parts = [p.strip("'-") for p in raw.split() if p.strip("'-")]
    stripped = [p for p in parts if p not in _CREDENTIALS]
    # Only if something is left. "CPA" alone is a bad record, not a nameless one,
    # and the caller needs to see it has too few words rather than none.
    return stripped or parts


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


# Tokens a company bolts onto its domain that carry no identity: mccombshq.com
# is McCombs, skygroup-co.com is Sky Group. Stripped only when what remains is
# still long enough to identify somebody.
_DOMAIN_NOISE = (
    "hq", "inc", "llc", "corp", "co", "company", "group", "holdings", "usa",
    "online", "global", "intl", "international", "team", "app", "io", "hub",
)
# A key has to be this long before *containment* counts, or a stripped stub
# matches half the companies in the book. Exact equality is held to a lower bar:
# "ima" is the whole identity of IMA Financial Group, and imacorp.com is how they
# write it. Three characters that match exactly, on a contact whose name already
# matched, is corroboration; three characters found inside a longer name is not.
_MIN_KEY = 4
_MIN_EXACT_KEY = 3


def _company_keys(name: Any) -> set[str]:
    """Every form of a company name worth comparing a domain against."""
    text = str(name or "")
    keys = {_company_key(text), _key(text)}
    words = [w for w in re.split(r"[^a-z0-9]+", _COMPANY_NOISE.sub(" ", text).lower()) if w]
    if words:
        keys.add(words[0])
        keys.add("".join(words[:2]))
    return {k for k in keys if k}


def _domain_keys(domain: str) -> set[str]:
    """The identifying part of a hostname, with and without bolt-on suffixes."""
    labels = [p for p in str(domain or "").lower().split(".") if p]
    if len(labels) < 2:
        return set()
    # The label before the TLD: mail.acme.com -> acme, mccombshq.com -> mccombshq.
    base = labels[-2]
    parts = [p for p in re.split(r"[^a-z0-9]+", base) if p]
    keys = {"".join(parts)}
    # Drop trailing noise words when the domain is hyphenated (skygroup-co).
    trimmed = list(parts)
    while len(trimmed) > 1 and trimmed[-1] in _DOMAIN_NOISE:
        trimmed.pop()
    keys.add("".join(trimmed))
    # And when it is not (mccombshq -> mccombs).
    for key in list(keys):
        for suffix in _DOMAIN_NOISE:
            if key.endswith(suffix) and len(key) - len(suffix) >= _MIN_EXACT_KEY:
                keys.add(key[: -len(suffix)])
    return {k for k in keys if len(k) >= _MIN_EXACT_KEY}


def employer_matches(profile_company: Any, *, known_company: str = "", domain: str = "") -> str:
    """How this profile employer corroborates what we already knew, or "".

    Two independent routes, either of which is enough: the name we already hold,
    or the domain the contact's email came from. The domain route is the reason
    "Holmes Murphy & Associates" can corroborate holmesmurphy.com, and why
    "McCombs Enterprises" corroborates mccombshq.com.
    """
    label = str(profile_company).strip()
    prof_keys = _company_keys(profile_company)
    if not prof_keys:
        return ""

    if known_company:
        for known in _company_keys(known_company):
            if len(known) < 3:
                continue
            if any(known == p or known in p or p in known for p in prof_keys if len(p) >= 3):
                return f"their profile lists {label}, which is the employer already on the record"

    if domain and domain not in FREE_MAIL_DOMAINS:
        dom_keys = _domain_keys(domain)
        # Exact first, because it tolerates short keys: imacorp.com -> "ima",
        # which is exactly what IMA Financial Group is called.
        if any(dom == p for dom in dom_keys for p in prof_keys):
            return f"their profile lists {label}, which matches their email domain {domain}"
        for dom in (d for d in dom_keys if len(d) >= _MIN_KEY):
            if any(dom in p or p in dom for p in prof_keys if len(p) >= _MIN_KEY):
                return f"their profile lists {label}, which matches their email domain {domain}"
        # Or the domain is an acronym of the company words.
        words = [w for w in re.split(r"[^a-z0-9]+", _COMPANY_NOISE.sub(" ", label).lower()) if w]
        if words and dom_keys and "".join(w[0] for w in words) in dom_keys:
            return f"their profile lists {label}, whose initials are their email domain {domain}"
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


def current_roles(entity: dict[str, Any], *, today: date | None = None) -> list[dict[str, str]]:
    """Roles with no end date — the jobs this person still holds."""
    today = today or date.today()
    out: list[dict[str, str]] = []
    for work in entity.get("workHistory") or []:
        if not isinstance(work, dict):
            continue
        company = (work.get("company") or {}).get("name") if isinstance(work.get("company"), dict) else None
        title = str(work.get("title") or "").strip()
        if not company or not title or _NON_EMPLOYMENT_TITLES.match(title):
            continue
        ended = _ended(work.get("dates"))
        if ended is None or ended >= today:
            out.append({"title": title, "company": str(company).strip()})
    return out


def resolve_person(
    contact: dict[str, Any],
    *,
    known_company: str = "",
    max_candidates: int = MAX_CANDIDATES,
    today: date | None = None,
) -> dict[str, Any] | None:
    """A defensible job title for this contact, or None. Never a guess.

    Returns ``{"fields": {...}, "evidence": {...}}`` shaped for the enrichment
    proposal path, or a ``{"skip": reason}`` marker -- a shared mailbox, someone
    who has changed jobs, or a person holding several roles at once are findings
    in their own right, not failed lookups.

    Two ways in. If HubSpot already holds a LinkedIn URL for this contact, that
    is the first candidate and no search is needed — but it is *not* taken on
    trust. Measured live: Phil Holland's stored URL resolves to Brian Holland,
    the same class of bad match Sales Navigator made on Thomas Heckler. The
    name check is what stands between that and a wrong job title.
    """
    name = str(contact.get("name") or "").strip()
    email = str(contact.get("email") or "").strip()
    domain = domain_of(email)

    blocked = non_person_reason(name, email)
    if blocked:
        return {"skip": "not_a_person", "reason": blocked}

    anchor = known_company or (domain if domain and domain not in FREE_MAIL_DOMAINS else "")
    on_record = str(contact.get("hs_linkedin_url") or "").strip()

    candidates: list[str] = []
    if on_record.lower().rstrip("/").find("linkedin.com/in/") != -1:
        candidates.append(on_record.rstrip("/"))
    if anchor:
        for url in find_profile_urls(name, anchor)[:max_candidates]:
            if url.lower() not in {c.lower() for c in candidates}:
                candidates.append(url)
    if not candidates:
        # Nothing to check an answer against and no URL on file. A personal
        # address with no employer is exactly the case where a confident wrong
        # title would sail straight through.
        return None

    for url in candidates:
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
            if anchor or url != candidates[0]:
                # We had something to corroborate against and the profile did
                # not carry it. That is a contradiction, not a gap.
                continue
            # No employer on file, no usable domain — but HubSpot itself put
            # this URL on this contact and the name matches. Identity rests on
            # those two facts, which is weaker than a corroborated employer and
            # is labelled as such rather than blended in with it.
            held = current_roles(entity, today=today)
            if len(held) != 1:
                if not held:
                    continue
                return {
                    "skip": "several_current_roles",
                    "reason": (
                        f"{name} lists {len(held)} current roles — "
                        + ", ".join(f"{r['title']} at {r['company']}" for r in held[:3])
                        + ". I can't tell which one belongs on the record."
                    ),
                    "profile_url": url,
                    "options": held,
                }
            only = held[0]
            fields = {"jobtitle": only["title"], "company": only["company"]}
            fields = {k: v for k, v in fields.items() if v}
            detail = (
                f"HubSpot already has this LinkedIn URL on the contact and the name "
                f"matches; the profile shows one current role, {only['title']} at "
                f"{only['company']} ({url}). Not independently corroborated — there is "
                "no employer on the record to check it against."
            )
            if verdict.get("note"):
                detail += f" {verdict['note']}."
            return {
                "fields": fields,
                "evidence": {
                    k: {
                        "source": "linkedin_profile_on_record",
                        "detail": detail,
                        "confidence": "on_record",
                        "profile_url": url,
                    }
                    for k in fields
                },
                "profile_url": url,
            }
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
