"""Recipient timezone detection — Heidi order: email chain → area code/domain → unknown.

When uncertain, compose uses Mountain Time with ET/CT/PT equivalents (see email_format).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import settings
from app.scheduling.email_format import DOMAIN_TIMEZONE_HINTS, TZ_ABBREV

MT = ZoneInfo(settings.scheduling_timezone)

# Body phrases → IANA zone (explicit self-identification only; no loose city/state words).
BODY_TZ_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(?:i am|i'm|we are|we're)\s+(?:in|on)\s+(?:the\s+)?eastern\b", re.I), "America/New_York"),
    (re.compile(r"\b(?:i am|i'm)\s+(?:in|on)\s+(?:the\s+)?pacific\b", re.I), "America/Los_Angeles"),
    (re.compile(r"\b(?:i am|i'm)\s+(?:in|on)\s+(?:the\s+)?central\b", re.I), "America/Chicago"),
    (re.compile(r"\b(?:i am|i'm)\s+(?:in|on)\s+(?:the\s+)?mountain\b", re.I), "America/Denver"),
    (re.compile(r"\bdenver[- ]based\b", re.I), "America/Denver"),
    (re.compile(r"\b(?:based in|located in|from)\s+denver\b", re.I), "America/Denver"),
    (re.compile(r"\b(?:based in|located in)\s+london\b", re.I), "Europe/London"),
    (re.compile(r"\b(?:based in|located in)\s+(?:the\s+)?uk\b", re.I), "Europe/London"),
    (re.compile(r"\b(?:i'm|i am)\s+on\s+(?:the\s+)?east\s+coast\b", re.I), "America/New_York"),
    (re.compile(r"\beast\s+coast\s*(?:\(?\s*et\s*\)?)?\b", re.I), "America/New_York"),
    (re.compile(r"\b(?:eastern|et)\s+time\b", re.I), "America/New_York"),
    (re.compile(r"\b(?:pacific|pt)\s+time\b", re.I), "America/Los_Angeles"),
    (re.compile(r"\b(?:central|ct)\s+time\b", re.I), "America/Chicago"),
    (re.compile(r"\b(?:mountain|mt)\s+time\b", re.I), "America/Denver"),
    (re.compile(r"\bgmt\b|\butc\b|\buk\s+time\b", re.I), "Europe/London"),
]

# Date header offset → approximate zone (sender mail client at send time).
_OFFSET_TO_ZONES: dict[int, tuple[str, ...]] = {
    -480: ("America/Los_Angeles",),
    -420: ("America/Denver",),
    -360: ("America/Denver", "America/Chicago"),
    -300: ("America/New_York",),
    -240: ("America/New_York",),  # EDT
    0: ("Europe/London",),
    60: ("Europe/London", "Europe/Paris"),
    330: ("Asia/Kolkata",),
    480: ("Asia/Shanghai", "Asia/Singapore"),
    540: ("Asia/Tokyo",),
}

_UNTRUSTED_PROFILE_SOURCES = frozenset({
    "body",
    "prior_email_body",
    "header_received",
    "profile",
    "prior_email_header",
    "received_at",
})

_TRUSTED_PROFILE_SOURCES = frozenset({
    "domain",
    "signature",
    "area_code",
    "prior_email_signature",
    "prior_email_area_code",
    "stored",
    "chain_signature",
    "chain_area_code",
})


@dataclass(frozen=True)
class RecipientTimezoneResult:
    timezone: ZoneInfo | None
    confidence: str  # known | inferred | unknown
    source: str  # domain | body | header_date | unknown | ...
    detail: str = ""

    def tz_name(self) -> str | None:
        return str(self.timezone) if self.timezone else None

    def label(self) -> str:
        if not self.timezone:
            return "unknown"
        return TZ_ABBREV.get(str(self.timezone), str(self.timezone))


_INTERNAL_EMAIL_DOMAINS = frozenset({"iconicfounders.com", "ifg.vc"})


def is_internal_org_email(sender_email: str | None) -> bool:
    return _is_internal_org_email(sender_email)


def is_timezone_uncertain(result: RecipientTimezoneResult) -> bool:
    """True when we should not present times as the recipient's local zone."""
    if result.confidence == "unknown" or not result.timezone:
        return True
    return result.source in {"unknown", "default_mt", "none"}


def record_learned_recipient_timezone(result: RecipientTimezoneResult, *, sender_email: str | None) -> None:
    """Persist TZ when learned from reliable signals (not internal MT default or guesses)."""
    if not sender_email or not result.timezone:
        return
    if result.source in {"internal_default", "none", "stored", "default_mt", "unknown"}:
        return
    if result.confidence == "unknown":
        return
    # Signature/header offsets are useful but weaker — only persist explicit body/domain/header_date.
    if result.source not in {
        "body",
        "domain",
        "header_date",
        "prior_email_body",
        "prior_email_signature",
        "prior_email_area_code",
        "area_code",
        "signature",
    }:
        return
    if result.source == "prior_email_body" and result.confidence != "known":
        return
    from app.storage.recipient_profiles import normalize_sender_email, upsert_recipient_timezone

    email = normalize_sender_email(sender_email) or sender_email
    upsert_recipient_timezone(
        email=email,
        timezone=result.tz_name() or "",
        source=result.source,
    )


def _sender_reply_text(body: str) -> str:
    """Only the sender's new text — not quoted thread / prior signatures below."""
    from app.scheduling.recipient_slot import _reply_text_for_matching

    return _reply_text_for_matching(body)


def _timezone_from_profile(sender_email: str | None) -> RecipientTimezoneResult | None:
    if not sender_email:
        return None
    from app.storage.recipient_profiles import get_recipient_profile

    profile = get_recipient_profile(sender_email)
    if not profile or not profile.get("timezone"):
        return None
    profile_source = str(profile.get("timezone_source") or "").strip()
    if profile_source in _UNTRUSTED_PROFILE_SOURCES:
        return None
    if profile_source and profile_source not in _TRUSTED_PROFILE_SOURCES:
        return None
    try:
        return RecipientTimezoneResult(
            timezone=ZoneInfo(str(profile["timezone"])),
            confidence="known",
            source="profile",
            detail=f"recipient_profiles: {profile_source or 'stored'}",
        )
    except ZoneInfoNotFoundError:
        return None


def detect_recipient_timezone(
    *,
    sender_email: str | None = None,
    body: str = "",
    internet_headers: list[dict[str, Any]] | None = None,
    received_at: str | None = None,
    stored_timezone: str | None = None,
    exclude_thread_id: str | None = None,
    allow_prior_threads: bool = True,
) -> RecipientTimezoneResult:
    """Best-effort recipient TZ (Heidi workflow).

    Order: stored → current email chain → prior threads → domain → profile → headers → unknown.
    """
    if stored_timezone:
        try:
            return RecipientTimezoneResult(
                timezone=ZoneInfo(stored_timezone),
                confidence="known",
                source="stored",
                detail="proposal/thread stored timezone",
            )
        except ZoneInfoNotFoundError:
            pass

    reply_text = _sender_reply_text(body)
    for text, source_prefix in _text_sources_for_current_email(body, reply_text):
        signature_result = _timezone_from_signature(text)
        if signature_result:
            return _tag_source(signature_result, source_prefix)

        body_result = _timezone_from_body(text)
        if body_result:
            return _tag_source(body_result, source_prefix)

        area_result = _timezone_from_area_code(text)
        if area_result:
            return _tag_source(area_result, source_prefix)

    if allow_prior_threads:
        prior_result = _timezone_from_prior_emails(
            sender_email,
            exclude_thread_id=exclude_thread_id,
        )
        if prior_result:
            return prior_result

    if not _is_internal_org_email(sender_email):
        domain_result = _timezone_from_domain(sender_email)
        if domain_result:
            return domain_result

    profile_result = _timezone_from_profile(sender_email)
    if profile_result:
        return profile_result

    header_result = _timezone_from_headers(internet_headers or [], received_at=received_at)
    if header_result:
        return header_result

    if _is_internal_org_email(sender_email):
        return RecipientTimezoneResult(
            timezone=MT,
            confidence="inferred",
            source="internal_default",
            detail="Internal colleague — default to Kory Mountain Time until learned.",
        )

    return RecipientTimezoneResult(
        timezone=None,
        confidence="unknown",
        source="unknown",
        detail="No reliable timezone signal — offer Mountain Time with US equivalents.",
    )


def _tag_source(result: RecipientTimezoneResult, prefix: str) -> RecipientTimezoneResult:
    source = f"{prefix}_{result.source}" if prefix else result.source
    if result.source == source:
        return result
    return RecipientTimezoneResult(
        timezone=result.timezone,
        confidence=result.confidence,
        source=source,
        detail=result.detail,
    )


def _text_sources_for_current_email(body: str, reply_text: str) -> list[tuple[str, str]]:
    """Recipient-authored blobs to scan on the current message (newest first)."""
    from app.scheduling.timezone_chain import recipient_chain_text

    sources: list[tuple[str, str]] = []
    if reply_text.strip():
        sources.append((reply_text, ""))
    chain = recipient_chain_text(body)
    if chain.strip() and chain.strip() != reply_text.strip():
        sources.append((chain, "chain"))
    return sources


def _timezone_from_prior_emails(
    sender_email: str | None,
    *,
    exclude_thread_id: str | None = None,
) -> RecipientTimezoneResult | None:
    """Scan prior ingested threads from the same sender for timezone cues.

    Two passes on purpose: TEXT evidence (signature/body/area code) across ALL
    threads first, and only then any thread's email headers. Interleaving them
    per-thread let the newest thread's Gmail headers (sender's physical UTC
    offset — the weakest signal) preempt an explicit signature sitting one
    thread older (live G-3 defect: learned NY signature lost to a -0600
    header, offer went out MT-only).
    """
    from app.scheduling.timezone_chain import recipient_chain_text
    from app.storage.recipient_profiles import list_prior_email_threads

    threads = list(
        list_prior_email_threads(sender_email, exclude_thread_id=exclude_thread_id)
    )

    for thread in threads:
        raw_body = str(thread.get("raw_body") or "")
        if not raw_body.strip():
            continue

        texts = []
        reply = _sender_reply_text(raw_body)
        if reply.strip():
            texts.append(reply)
        chain = recipient_chain_text(raw_body)
        if chain.strip() and chain.strip() != reply.strip():
            texts.append(chain)
        if not texts:
            texts = [raw_body]

        for prior_body in texts:
            signature_result = _timezone_from_signature(prior_body)
            if signature_result:
                return RecipientTimezoneResult(
                    timezone=signature_result.timezone,
                    confidence=signature_result.confidence,
                    source="prior_email_signature",
                    detail=f"Prior thread {thread.get('thread_id')}: {signature_result.detail}",
                )

            body_result = _timezone_from_body(prior_body)
            if body_result:
                return RecipientTimezoneResult(
                    timezone=body_result.timezone,
                    confidence=body_result.confidence,
                    source="prior_email_body",
                    detail=f"Prior thread {thread.get('thread_id')}: {body_result.detail}",
                )

            area_result = _timezone_from_area_code(prior_body)
            if area_result:
                return RecipientTimezoneResult(
                    timezone=area_result.timezone,
                    confidence=area_result.confidence,
                    source="prior_email_area_code",
                    detail=f"Prior thread {thread.get('thread_id')}: {area_result.detail}",
                )

    for thread in threads:
        headers: list[dict[str, Any]] = []
        raw_headers = thread.get("internet_headers_json")
        if raw_headers:
            try:
                parsed = json.loads(raw_headers)
                if isinstance(parsed, list):
                    headers = [h for h in parsed if isinstance(h, dict)]
            except (TypeError, json.JSONDecodeError):
                headers = []

        header_result = _timezone_from_headers(headers)
        if header_result:
            return RecipientTimezoneResult(
                timezone=header_result.timezone,
                confidence=header_result.confidence,
                source="prior_email_header",
                detail=f"Prior thread {thread.get('thread_id')}: {header_result.detail}",
            )

    return None


def lookup_recipient_timezone(
    *,
    sender_email: str | None = None,
    body: str = "",
    internet_headers: list[dict[str, Any]] | None = None,
    received_at: str | None = None,
    stored_timezone: str | None = None,
    for_scheduling: bool = False,
) -> RecipientTimezoneResult:
    """TZ lookup for cards/compose. Set for_scheduling=True to scan prior threads + chain."""
    return detect_recipient_timezone(
        sender_email=sender_email,
        body=body,
        internet_headers=internet_headers,
        received_at=received_at,
        stored_timezone=stored_timezone,
        allow_prior_threads=for_scheduling,
    )


def resolve_recipient_timezone_at_ingest(
    *,
    sender_email: str | None = None,
    body: str = "",
    internet_headers: list[dict[str, Any]] | None = None,
    received_at: str | None = None,
    exclude_thread_id: str | None = None,
) -> RecipientTimezoneResult:
    """Write-time TZ resolution — full pipeline + persist learned facts."""
    result = detect_recipient_timezone(
        sender_email=sender_email,
        body=body,
        internet_headers=internet_headers,
        received_at=received_at,
        exclude_thread_id=exclude_thread_id,
        allow_prior_threads=True,
    )
    record_learned_recipient_timezone(result, sender_email=sender_email)
    return result


def _is_internal_org_email(sender_email: str | None) -> bool:
    if not sender_email or "@" not in sender_email:
        return False
    domain = sender_email.split("@", 1)[1].lower()
    return domain in _INTERNAL_EMAIL_DOMAINS


def _timezone_from_domain(sender_email: str | None) -> RecipientTimezoneResult | None:
    if not sender_email or "@" not in sender_email:
        return None
    domain = sender_email.split("@", 1)[1].lower()
    for pattern, tz_name in DOMAIN_TIMEZONE_HINTS.items():
        if domain == pattern or domain.endswith("." + pattern):
            try:
                return RecipientTimezoneResult(
                    timezone=ZoneInfo(tz_name),
                    confidence="known",
                    source="domain",
                    detail=f"Known domain map: {domain}",
                )
            except ZoneInfoNotFoundError:
                return None
    if domain.endswith(".co.uk") or domain.endswith(".uk"):
        return RecipientTimezoneResult(
            timezone=ZoneInfo("Europe/London"),
            confidence="inferred",
            source="domain",
            detail=f"UK TLD: {domain}",
        )
    return None


_SIGNATURE_CITY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(?:new york|nyc|manhattan|brooklyn|boston),?\s*(?:ny|ma)\b", re.I), "America/New_York"),
    (re.compile(r"\b(?:chicago|naperville),?\s*il\b", re.I), "America/Chicago"),
    (re.compile(r"\b(?:dallas|houston|austin|san antonio|fort worth),?\s*tx\b", re.I), "America/Chicago"),
    (re.compile(r"\b(?:denver|boulder|colorado springs),?\s*co\b", re.I), "America/Denver"),
    (re.compile(r"\b(?:los angeles|san francisco|seattle|portland),?\s*(?:ca|wa|or)\b", re.I), "America/Los_Angeles"),
    (re.compile(r"\b(?:miami|atlanta|charlotte|washington),?\s*(?:fl|ga|nc|dc)\b", re.I), "America/New_York"),
    (re.compile(r"\b(?:london|manchester|edinburgh),?\s*(?:uk|england)?\b", re.I), "Europe/London"),
    (re.compile(r"\baustin,?\s*tx\s+area\b", re.I), "America/Chicago"),
    (re.compile(r"\b\d{5}\s+(?:il|illinois)\b", re.I), "America/Chicago"),
    (re.compile(r"\b\d{5}\s+(?:ny|new york)\b", re.I), "America/New_York"),
    (re.compile(r"\b\d{5}\s+(?:tx|texas)\b", re.I), "America/Chicago"),
    (re.compile(r"\b\d{5}\s+(?:co|colorado)\b", re.I), "America/Denver"),
    (re.compile(r"\b\d{5}\s+(?:ca|california)\b", re.I), "America/Los_Angeles"),
]


def _timezone_from_area_code(text: str) -> RecipientTimezoneResult | None:
    from app.scheduling.area_code_timezone import extract_area_codes, timezone_from_area_codes

    tz = timezone_from_area_codes(text)
    if not tz:
        return None
    codes = extract_area_codes(text)
    return RecipientTimezoneResult(
        timezone=tz,
        confidence="inferred",
        source="area_code",
        detail=f"Phone area code(s): {', '.join(codes[:3])}",
    )


def _timezone_from_signature(body: str) -> RecipientTimezoneResult | None:
    """Infer TZ from city/state/ZIP lines in the sender's signature (reply tail only)."""
    if not body.strip():
        return None
    tail = "\n".join(body.strip().splitlines()[-12:])
    for pattern, tz_name in _SIGNATURE_CITY_PATTERNS:
        if pattern.search(tail):
            try:
                return RecipientTimezoneResult(
                    timezone=ZoneInfo(tz_name),
                    confidence="inferred",
                    source="signature",
                    detail=f"Signature region matched: {pattern.pattern[:40]}",
                )
            except ZoneInfoNotFoundError:
                continue
    return None


def _timezone_from_body(body: str) -> RecipientTimezoneResult | None:
    if not body.strip():
        return None
    for pattern, tz_name in BODY_TZ_PATTERNS:
        if pattern.search(body):
            try:
                return RecipientTimezoneResult(
                    timezone=ZoneInfo(tz_name),
                    confidence="known",
                    source="body",
                    detail=f"Body matched: {pattern.pattern[:40]}",
                )
            except ZoneInfoNotFoundError:
                continue
    return None


def _timezone_from_headers(
    headers: list[dict[str, Any]],
    *,
    received_at: str | None = None,
) -> RecipientTimezoneResult | None:
    """Infer sender TZ from Date header (RFC 2822 offset).

    Prefer the sender's Date: line (-0500, -0400, etc.). Microsoft 365 often
    rewrites headers to +0000 — those are skipped (not the sender's local zone).
    Received hops are relay servers and are not used.
    """
    normalized = _normalize_headers(headers)

    date_raw = normalized.get("date")
    if date_raw:
        result = _timezone_from_header_value(date_raw, source="header_date")
        if result:
            return result

    # Graph receivedDateTime is usually UTC — only use when a non-zero offset is present.
    if received_at:
        try:
            parsed = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
            if parsed.tzinfo:
                offset_minutes = int(parsed.utcoffset().total_seconds() // 60)  # type: ignore[union-attr]
                if offset_minutes != 0:
                    zones = _OFFSET_TO_ZONES.get(offset_minutes)
                    if zones:
                        return RecipientTimezoneResult(
                            timezone=ZoneInfo(zones[0]),
                            confidence="inferred",
                            source="received_at",
                            detail=f"Graph receivedDateTime offset → {zones[0]}",
                        )
        except (TypeError, ValueError):
            pass
    return None


def _timezone_from_header_value(value: str, *, source: str) -> RecipientTimezoneResult | None:
    offset_minutes = _parse_rfc2822_offset_minutes(value)
    if offset_minutes is None:
        return None
    # Exchange / cloud mailboxes often normalize to UTC — not the sender's zone.
    if offset_minutes == 0:
        return None
    zones = _OFFSET_TO_ZONES.get(offset_minutes)
    if not zones:
        return None
    try:
        tz = ZoneInfo(zones[0])
        return RecipientTimezoneResult(
            timezone=tz,
            confidence="inferred",
            source=source,
            detail=f"{source} offset {offset_minutes // 60:+d}h → {zones[0]}",
        )
    except ZoneInfoNotFoundError:
        return None


def _normalize_headers(headers: list[dict[str, Any]]) -> dict[str, str]:
    out: dict[str, str] = {}
    received_all: list[str] = []
    for item in headers:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("header") or "").strip().lower()
        value = str(item.get("value") or "").strip()
        if not name or not value:
            continue
        if name == "received":
            received_all.append(value)
            continue
        out[name] = value
    if received_all:
        out["received_all"] = received_all
        out["received"] = received_all[0]
    return out


def _parse_rfc2822_offset_minutes(value: str) -> int | None:
    """Parse trailing ±HHMM from Date/Received headers."""
    match = re.search(r"([+-])(\d{2})(\d{2})\s*$", value.strip())
    if not match:
        match = re.search(r"UTC([+-])(\d{2}):?(\d{2})", value, re.I)
    if not match:
        return None
    sign = -1 if match.group(1) == "-" else 1
    hours = int(match.group(2))
    minutes = int(match.group(3))
    return sign * (hours * 60 + minutes)


def extract_internet_headers(message: dict[str, Any]) -> list[dict[str, Any]]:
    raw = message.get("internetMessageHeaders") or message.get("internet_message_headers")
    if isinstance(raw, list):
        return [h for h in raw if isinstance(h, dict)]
    return []
