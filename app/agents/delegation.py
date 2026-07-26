"""Detect when Kory delegates scheduling to Lexi via CC or explicit phrasing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.config import settings


DELEGATION_PHRASES = (
    r"my assistant lexi",
    r"my assistant, lexi",
    r"assistant lexi will",
    r"lexi will help",
    r"lexi can help",
    r"lexi will follow up",
    r"lexi will coordinate",
    r"lexi will schedule",
    r"looping in lexi",
    r"cc(?:'|')?ing lexi",
    r"copying lexi",
)

DELEGATION_SUBJECT_PHRASES = (
    r"introducing lexi",
    r"meet lexi",
)


@dataclass(frozen=True)
class DelegationDecision:
    is_delegation: bool
    reason: str
    lexi_cc: bool = False
    phrase_match: bool = False


def _lexi_addresses() -> tuple[str, ...]:
    raw = (settings.lexi_mailbox_email or "").strip().lower()
    extras = (settings.lexi_cc_emails or "").strip()
    addresses: list[str] = []
    if raw:
        addresses.append(raw)
    if extras:
        addresses.extend(e.strip().lower() for e in extras.split(",") if e.strip())
    return tuple(addresses)


def _recipient_list(raw_email: dict[str, Any]) -> list[str]:
    recipients: list[str] = []
    for key in ("to_recipients", "cc_recipients", "bcc_recipients", "recipients"):
        value = raw_email.get(key)
        if isinstance(value, str):
            recipients.extend(_extract_emails(value))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    recipients.extend(_extract_emails(item))
                elif isinstance(item, dict):
                    addr = item.get("emailAddress") or item.get("address") or {}
                    if isinstance(addr, dict):
                        email = addr.get("address") or addr.get("email")
                    else:
                        email = addr
                    if email:
                        recipients.append(str(email).lower())
    return recipients


def _extract_emails(text: str) -> list[str]:
    return [m.group(0).lower() for m in re.finditer(r"[\w.+-]+@[\w.-]+\.\w+", text or "")]


def _kory_addresses() -> set[str]:
    addrs = {(settings.kory_cc_email or "").strip().lower()}
    addrs |= {e.strip().lower() for e in settings.kory_sender_emails}
    return {a for a in addrs if a and "@" in a}


def _to_recipient_addresses(raw_email: dict[str, Any]) -> list[str]:
    out: list[str] = []
    value = raw_email.get("to_recipients")
    if isinstance(value, str):
        out.extend(_extract_emails(value))
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                out.extend(_extract_emails(item))
            elif isinstance(item, dict):
                addr = item.get("emailAddress") or item.get("address") or {}
                email = addr.get("address") or addr.get("email") if isinstance(addr, dict) else addr
                if email:
                    out.append(str(email).lower())
    return out


def delegation_counterpart(raw_email: dict[str, Any]) -> str:
    """When Kory delegates (CC's Lexi), the party Lexi schedules with is a To
    recipient who is neither Kory nor Lexi. Returns "" if none is found."""
    email, _name = delegation_counterpart_contact(raw_email)
    return email


def delegation_counterpart_contact(raw_email: dict[str, Any]) -> tuple[str, str]:
    """Counterpart (email, display_name) for a delegation email — the To recipient
    who isn't Kory or Lexi. display_name is "" when Outlook didn't supply one."""
    exclude = _kory_addresses() | {a.lower() for a in _lexi_addresses()}
    value = raw_email.get("to_recipients")
    items = value if isinstance(value, list) else []
    for item in items:
        if isinstance(item, dict):
            addr_obj = item.get("emailAddress") or item.get("address") or {}
            if isinstance(addr_obj, dict):
                email = str(addr_obj.get("address") or addr_obj.get("email") or "").lower()
                name = str(addr_obj.get("name") or "").strip()
            else:
                email, name = str(addr_obj).lower(), ""
            if email and "@" in email and email not in exclude:
                return email, name
        elif isinstance(item, str):
            for email in _extract_emails(item):
                if email not in exclude:
                    return email, ""
    return "", ""


KORY_SCHEDULING_ASK = (
    r"(?:find|get)\s+(?:some\s+)?time",
    r"find us a time",
    r"before i take off",
    r"before i head to",
    r"connect this week",
    r"schedule us",
    r"set up a (?:call|meeting)",
)


_QUOTED_MARKERS = (
    "[prior messages in this email chain]",
    "--- earlier in thread",
    "-----original message-----",
    "________________________________",
)


def _new_text_only(body: str) -> str:
    """Only what the sender just wrote — quoted history must not delegate.

    A delegation is an instruction someone gives now. Matching against quoted
    text makes every later message in the thread (and, when thread context is
    stitched in, unrelated mail) look like a fresh hand-off.
    """
    text = body or ""
    lowered = text.lower()
    cut = len(text)
    for marker in _QUOTED_MARKERS:
        found = lowered.find(marker)
        if found != -1:
            cut = min(cut, found)
    return text[:cut]


def detect_delegation(
    *,
    subject: str,
    body: str,
    sender: str = "",
    raw_email: dict[str, Any] | None = None,
) -> DelegationDecision:
    """True when Kory delegated to Lexi (CC lexi@ and/or delegation phrasing)."""
    combined = f"{subject}\n{_new_text_only(body)}".lower()
    sender_l = (sender or "").lower()

    lexi_cc = False
    if raw_email:
        lexi_addrs = _lexi_addresses()
        if lexi_addrs:
            for addr in _recipient_list(raw_email):
                if any(addr == la or addr.endswith(f"@{la.split('@')[-1]}") for la in lexi_addrs):
                    if addr in lexi_addrs:
                        lexi_cc = True
                        break

    phrase_match = any(re.search(p, combined) for p in DELEGATION_PHRASES)
    phrase_match = phrase_match or any(
        re.search(p, (subject or "").lower()) for p in DELEGATION_SUBJECT_PHRASES
    )

    # Kory forwarding/delegating from his own address strengthens signal.
    from_kory = any(
        domain in sender_l
        for domain in ("@iconicfounders.com", "@ifg.vc", "kory")
    )

    if lexi_cc and from_kory and any(re.search(p, combined) for p in KORY_SCHEDULING_ASK):
        return DelegationDecision(
            is_delegation=True,
            reason="kory_cc_scheduling_ask",
            lexi_cc=True,
            phrase_match=True,
        )
    if lexi_cc and (phrase_match or from_kory):
        return DelegationDecision(
            is_delegation=True,
            reason="lexi_cc_and_delegation_signal",
            lexi_cc=True,
            phrase_match=phrase_match,
        )
    if phrase_match and from_kory:
        return DelegationDecision(
            is_delegation=True,
            reason="kory_delegation_phrase",
            lexi_cc=lexi_cc,
            phrase_match=True,
        )
    if lexi_cc and settings.lexi_delegation_cc_only:
        return DelegationDecision(
            is_delegation=True,
            reason="lexi_cc_only",
            lexi_cc=True,
            phrase_match=phrase_match,
        )
    return DelegationDecision(
        is_delegation=False,
        reason="not_delegation",
        lexi_cc=lexi_cc,
        phrase_match=phrase_match,
    )
