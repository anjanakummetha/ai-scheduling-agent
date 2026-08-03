"""Who introduced Kory — parse intro threads and persist on recipient profiles."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.storage.recipient_profiles import (
    get_recipient_profile,
    normalize_sender_email,
    upsert_introducer,
)


_INTRO_SUBJECT_RE = re.compile(
    r"\b(intro(?:ducing|duction)?|connect(?:ing)?|meet(?:ing)?)\b",
    re.IGNORECASE,
)
_INTRO_BODY_RE = re.compile(
    r"(?:wanted to |pleased to |happy to )?"
    r"(?:introduce you to|introducing you to|connect you with|connecting you with|"
    r"loop(?:ing)? (?:you )?in with|please meet|meet my (?:friend|colleague))",
    re.IGNORECASE,
)
_NAMED_INTRO_RE = re.compile(
    r"(?:introduced (?:you to|by)|met (?:through|via)|connected (?:us|you) (?:through|via))\s+"
    r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class IntroducerInfo:
    name: str
    email: str | None = None
    source: str = "inferred"


def _kory_emails() -> set[str]:
    emails: set[str] = set()
    for addr in settings.kory_sender_emails:
        emails.add(addr.lower())
    for domain in ("kory",):
        emails.add(domain)
    return emails


def _lexi_emails() -> set[str]:
    addrs: set[str] = set()
    if settings.lexi_mailbox_email:
        addrs.add(settings.lexi_mailbox_email.lower())
    if settings.lexi_cc_emails:
        for part in settings.lexi_cc_emails.split(","):
            if part.strip():
                addrs.add(part.strip().lower())
    return addrs


def _extract_emails(recipients: Any) -> list[str]:
    if not recipients:
        return []
    out: list[str] = []
    if isinstance(recipients, str):
        out.extend(re.findall(r"[\w.+-]+@[\w.-]+\.\w+", recipients))
    elif isinstance(recipients, list):
        for item in recipients:
            if isinstance(item, str):
                out.extend(re.findall(r"[\w.+-]+@[\w.-]+\.\w+", item))
            elif isinstance(item, dict):
                addr = item.get("emailAddress") or item.get("address") or {}
                if isinstance(addr, dict):
                    email = addr.get("address") or addr.get("email")
                else:
                    email = addr
                if email:
                    out.append(str(email).lower())
    return [e.lower() for e in out]


def _is_kory(email: str) -> bool:
    low = email.lower()
    if low in _kory_emails():
        return True
    return any(token in low for token in ("kory.mitchell", "kory@ifg", "kory@iconic"))


def _is_lexi(email: str) -> bool:
    return email.lower() in _lexi_emails()


def extract_introducer_from_email(
    *,
    subject: str = "",
    body: str = "",
    sender: str = "",
    to_recipients: Any = None,
    cc_recipients: Any = None,
) -> IntroducerInfo | None:
    """Best-effort introducer from an intro-style email."""
    combined = f"{subject}\n{body}"
    sender_email = normalize_sender_email(sender) or ""
    is_intro = bool(_INTRO_SUBJECT_RE.search(subject) or _INTRO_BODY_RE.search(combined))
    if not is_intro:
        named = _NAMED_INTRO_RE.search(combined)
        if named:
            return IntroducerInfo(name=named.group(1).strip(), source="body_phrase")
        return None

    if sender_email and not _is_kory(sender_email) and not _is_lexi(sender_email):
        name = _display_name_from_sender(sender) or _name_from_email(sender_email)
        return IntroducerInfo(name=name, email=sender_email, source="intro_sender")

    # Third party on CC who isn't Kory/Lexi/guest
    all_cc = _extract_emails(cc_recipients)
    guest_emails = set(_extract_emails(to_recipients))
    for cc in all_cc:
        if _is_kory(cc) or _is_lexi(cc) or cc in guest_emails:
            continue
        return IntroducerInfo(
            name=_name_from_email(cc),
            email=cc,
            source="cc_chain",
        )
    return None


def _name_from_email(email: str) -> str:
    """Human-readable name for an address with no display name on the header.

    Prefers the name already learned for this contact — the profile store holds
    "Anjana Kummetha" while the local part is "anjanakummetha", and it was the
    raw local part that reached meeting titles and Teams cards.
    """
    address = (email or "").strip().lower()
    if not address:
        return ""

    try:
        from app.storage.recipient_profiles import get_recipient_profile

        profile = get_recipient_profile(address) or {}
        stored = str(profile.get("display_name") or "").strip()
        if stored:
            return stored
    except Exception:  # profile store unavailable — fall back to the local part
        pass

    local = address.split("@", 1)[0]
    # dots/underscores/hyphens are word separators; a run-together local part
    # ("anjanakummetha") can't be split reliably, so title-case it as one word.
    cleaned = re.sub(r"[._-]+", " ", local)
    cleaned = re.sub(r"\d+", "", cleaned).strip()
    return cleaned.title() if cleaned else local


def _display_name_from_sender(sender: str) -> str | None:
    match = re.match(r"^([^<]+)<", sender.strip())
    if match:
        name = match.group(1).strip().strip('"')
        return name or None
    if "@" not in sender:
        return sender.strip() or None
    return None


def resolve_introducer_for_contact(
    *,
    email: str,
    subject: str = "",
    body: str = "",
    sender: str = "",
    to_recipients: Any = None,
    cc_recipients: Any = None,
) -> IntroducerInfo | None:
    """Profile store first, then parse this thread."""
    profile = get_recipient_profile(email)
    if profile:
        name = (profile.get("introducer_name") or "").strip()
        intro_email = (profile.get("introducer_email") or "").strip() or None
        # Rows written before the name fix stored the bare local part
        # ("anjanakummetha"). Recompute those at read time rather than migrating,
        # so the stored value can't keep leaking into meeting titles and cards.
        if name and intro_email and name.lower() == intro_email.split("@", 1)[0].lower():
            name = _name_from_email(intro_email)
        if name:
            return IntroducerInfo(
                name=name,
                email=intro_email,
                source=str(profile.get("introducer_source") or "profile"),
            )

    parsed = extract_introducer_from_email(
        subject=subject,
        body=body,
        sender=sender,
        to_recipients=to_recipients,
        cc_recipients=cc_recipients,
    )
    if parsed and parsed.email:
        upsert_introducer(
            email=email,
            introducer_name=parsed.name,
            introducer_email=parsed.email,
            source=parsed.source,
        )
    return parsed


def format_introducer_line(info: IntroducerInfo | None) -> str:
    if not info or not (info.name or "").strip():
        return "**Introduced by:** Unknown"
    if info.email:
        return f"**Introduced by:** {info.name} ({info.email})"
    return f"**Introduced by:** {info.name}"
