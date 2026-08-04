"""Kory email reply formatting — recipient TZ first, MT in parentheses."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.config import settings

import rules as kory_rules

MT = ZoneInfo(settings.scheduling_timezone)

# Rough recipient TZ hints from email domain / TLD patterns.
DOMAIN_TIMEZONE_HINTS: dict[str, str] = {
    "iconicfounders.com": "America/Denver",
    "ifg.vc": "America/Denver",
    "newportadvisors.co": "America/New_York",
    "solamerecapital.com": "America/New_York",
    "daybreakadvisory.com": "America/Los_Angeles",
    "price.co": "America/Los_Angeles",
}

TZ_ABBREV = {
    "America/New_York": "ET",
    "America/Chicago": "CT",
    "America/Denver": "MT",
    "America/Los_Angeles": "PT",
    "Europe/London": "UK",
}

US_ZONE_ABBREVS = ("ET", "CT", "PT")
US_ZONE_IANA = {
    "ET": ZoneInfo("America/New_York"),
    "CT": ZoneInfo("America/Chicago"),
    "PT": ZoneInfo("America/Los_Angeles"),
}


def recipient_timezone_confidence(sender_email: str | None) -> tuple[ZoneInfo | None, str]:
    """Return (timezone, confidence). Prefer detect_recipient_timezone() with headers when available."""
    from app.scheduling.timezone_intel import detect_recipient_timezone

    result = detect_recipient_timezone(sender_email=sender_email)
    return result.timezone, result.confidence


def recipient_timezone_from_context(
    *,
    sender_email: str | None = None,
    body: str = "",
    internet_headers: list | None = None,
    stored_timezone: str | None = None,
    received_at: str | None = None,
) -> tuple[ZoneInfo | None, str, str]:
    """Full context TZ lookup; stored_timezone from proposal DB wins if set."""
    if stored_timezone:
        try:
            return ZoneInfo(stored_timezone), "known", "stored"
        except Exception:
            pass
    from app.scheduling.timezone_intel import lookup_recipient_timezone

    result = lookup_recipient_timezone(
        sender_email=sender_email,
        body=body,
        internet_headers=internet_headers,
        received_at=received_at,
        stored_timezone=stored_timezone,
    )
    return result.timezone, result.confidence, result.source


def infer_recipient_timezone(sender_email: str | None) -> ZoneInfo:
    """Legacy — returns MT only when we have no signal (formatting fallback)."""
    tz, confidence = recipient_timezone_confidence(sender_email)
    if tz is not None and confidence != "unknown":
        return tz
    return MT


def format_slot_for_email(
    slot: dict[str, str],
    *,
    recipient_tz: ZoneInfo | None = None,
) -> str:
    """Format one offered slot: recipient local time first, MT in parentheses."""
    recipient_tz = recipient_tz or MT
    start_raw = slot.get("start", "")
    end_raw = slot.get("end", "")
    try:
        start = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
        end = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return f"{start_raw} – {end_raw}"

    start_local = start.astimezone(recipient_tz)
    end_local = end.astimezone(recipient_tz)
    start_mt = start.astimezone(MT)
    end_mt = end.astimezone(MT)

    recipient_label = TZ_ABBREV.get(str(recipient_tz), "local")
    day = start_local.strftime("%A, %B %-d")
    start_t = start_local.strftime("%-I:%M %p").lstrip("0")
    end_t = end_local.strftime("%-I:%M %p").lstrip("0")
    start_mt_t = start_mt.strftime("%-I:%M %p").lstrip("0")
    end_mt_t = end_mt.strftime("%-I:%M %p").lstrip("0")

    if str(recipient_tz) == str(MT):
        return f"{day} at {start_mt_t}–{end_mt_t} MT"

    # Legacy display labels for non-US zones
    display_label = recipient_label
    if display_label in {"ET", "CT", "PT", "MT"}:
        pass
    elif str(recipient_tz) == "America/New_York":
        display_label = "Eastern"
    elif str(recipient_tz) == "America/Chicago":
        display_label = "Central"
    elif str(recipient_tz) == "America/Los_Angeles":
        display_label = "Pacific"

    return (
        f"{day} at {start_t}–{end_t} {display_label} "
        f"({start_mt_t}–{end_mt_t} MT)"
    )


def format_slot_for_email_uncertain_us(slot: dict[str, str]) -> str:
    """Heidi fallback: MT first, then ET / CT / PT in one parenthetical."""
    start_raw = slot.get("start", "")
    end_raw = slot.get("end", "")
    try:
        start = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
        end = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return f"{start_raw} – {end_raw}"

    start_mt = start.astimezone(MT)
    end_mt = end.astimezone(MT)
    day = start_mt.strftime("%A, %B %-d")
    start_mt_t = start_mt.strftime("%-I:%M %p").lstrip("0")
    end_mt_t = end_mt.strftime("%-I:%M %p").lstrip("0")

    zone_parts: list[str] = []
    for abbrev in US_ZONE_ABBREVS:
        z = US_ZONE_IANA[abbrev]
        zs = start.astimezone(z).strftime("%-I:%M %p").lstrip("0")
        ze = end.astimezone(z).strftime("%-I:%M %p").lstrip("0")
        zone_parts.append(f"{zs}–{ze} {abbrev}")

    return f"{day} at {start_mt_t}–{end_mt_t} MT ({' / '.join(zone_parts)})"


def should_use_us_equivalent_slot_format(
    *,
    sender_email: str | None,
    uncertain: bool = False,
    tz_confidence: str = "",
    tz_source: str = "",
    intent: str = "",
    meeting_format: str = "",
) -> bool:
    """Deprecated for offer emails — we no longer bulk ET/CT/PT into every line."""
    return False


def should_note_mt_only_timezone(
    *,
    sender_email: str | None,
    uncertain: bool = False,
    tz_confidence: str = "",
    tz_source: str = "",
) -> bool:
    """True when Lexi should say TZ is unknown and list Mountain Time only."""
    from app.scheduling.timezone_intel import is_internal_org_email

    if uncertain:
        return True
    if is_internal_org_email(sender_email) and tz_source in {"internal_default", "stored"}:
        return False
    known_sources = {
        "body",
        "signature",
        "profile",
        "header_date",
        "domain",
        "prior_email_body",
        "prior_email_signature",
        "area_code",
        "chain_area_code",
    }
    if tz_confidence == "known" and tz_source in known_sources:
        return False
    return tz_source in {"unknown", "default_mt", "none"} or (
        not is_internal_org_email(sender_email) and tz_confidence != "known"
    )


def lexi_unknown_timezone_note(*, voice_mode: str = "lexi") -> str:
    if voice_mode == "lexi":
        return (
            "I couldn't identify your time zone from our conversation, "
            "so I've listed the options below in Mountain Time (MT).\n\n"
        )
    return (
        "I couldn't identify your time zone, so I've listed the options below "
        "in Mountain Time (MT).\n\n"
    )


def format_offer_slot_block(
    slots: list[dict[str, str]],
    *,
    recipient_tz: ZoneInfo | None = None,
) -> str:
    """Bullet block for scheduling offers — MT-only or recipient-local formatting."""
    tz = recipient_tz or MT
    lines = [f"• {format_slot_for_email(slot, recipient_tz=tz)}" for slot in slots[:3]]
    return "\n".join(lines)


def build_meeting_scheduling_phrase(
    *,
    intent: str | None = None,
    subject: str = "",
    body: str = "",
) -> str:
    """Short noun phrase for outbound drafts (type + duration + format)."""
    from app.scheduling.meeting_type import resolve_meeting_type
    from app.scheduling.slot_engine import infer_meeting_format

    spec = resolve_meeting_type(intent=intent, subject=subject, body=body)
    fmt = infer_meeting_format(spec.type_key, subject=subject, body=body)
    virtual = fmt == "virtual"

    if spec.type_key == "referral_or_intro":
        return "a 30-minute virtual intro call on Teams"
    if spec.type_key == "new_client":
        return f"a 60-minute {'virtual Teams call' if virtual else 'meeting'}"
    if spec.type_key == "coffee":
        return "a 60-minute coffee in Cherry Creek (30 minutes kept clear after on Kory's calendar)"
    if spec.type_key == "happy_hour":
        return "a happy hour (1.5 hours, ending by 6:00 PM MT)"
    if spec.type_key == "dinner":
        return "a dinner meeting (evening, post-6:00 PM MT)"
    if spec.type_key == "podcast":
        return "a podcast recording for The Turn"
    return spec.draft_type_label()


def build_scheduling_reply(
    *,
    recipient_first_name: str,
    slots: list[dict[str, str]],
    sender_email: str | None = None,
    meeting_context: str = "",
    intent: str | None = None,
    subject: str = "",
    voice_mode: str = "kory",
    recipient_body: str = "",
    internet_headers: list | None = None,
    stored_recipient_timezone: str | None = None,
) -> str:
    """Build a scheduling reply matching Kory's rules (plain text email body)."""
    from app.scheduling.lexi_voice import normalize_voice_mode

    mode = normalize_voice_mode(voice_mode)
    sign_off = kory_rules.EMAIL_RULES.get("sign_off", "Let's Win")
    if mode == "lexi":
        from app.scheduling.lexi_voice import LEXI_SIGNOFF_BLOCK

        closing = LEXI_SIGNOFF_BLOCK
    else:
        closing = f"{sign_off},\nKory"
    name = recipient_display_name(
        sender_email,
        recipient_body,
        fallback_first_name=recipient_first_name,
    )
    from app.scheduling.timezone_intel import detect_recipient_timezone, is_internal_org_email, is_timezone_uncertain

    tz_result = detect_recipient_timezone(
        sender_email=sender_email,
        body=recipient_body,
        internet_headers=internet_headers,
        stored_timezone=stored_recipient_timezone,
        allow_prior_threads=True,
    )
    recipient_tz = tz_result.timezone
    tz_confidence = tz_result.confidence
    tz_source = tz_result.source
    needs_tz_confirm = (
        not is_internal_org_email(sender_email)
        and is_timezone_uncertain(tz_result)
    )
    format_tz = recipient_tz if recipient_tz and not needs_tz_confirm else MT

    if not slots:
        greeting = f"Hi {name},"
        if mode == "lexi":
            body_intro = (
                f"{greeting}\n\n"
                "I'm Lexi, Kory's assistant. Appreciate you reaching out — "
                "I'm going to review the calendar and follow up with times that work.\n\n"
            )
        else:
            body_intro = (
                f"{greeting}\n\n"
                "Appreciate you reaching out. I'm going to review the calendar and follow up "
                "with times that work.\n\n"
            )
        return normalize_draft_for_display(
            f"{body_intro}{closing}",
            max_chars=None,
            voice_mode=mode,
        )

    context_line = ""
    if slots:
        phrase = build_meeting_scheduling_phrase(
            intent=intent,
            subject=subject or recipient_body,
            body=recipient_body,
        )
        if mode == "lexi":
            context_line = (
                f"I have a few times for {phrase}:\n\n"
            )
        else:
            context_line = f"I have a few times for {phrase}:\n\n"

    tz_ask_line = ""
    if needs_tz_confirm:
        lines = "\n".join(
            f"• {format_slot_for_email(slot, recipient_tz=MT)}"
            for slot in slots[:3]
        )
        tz_note = (
            "[Kory: recipient timezone unknown — times below are Mountain Time only. "
            "Confirm or edit before sending.]\n\n"
        )
        if mode == "lexi":
            tz_ask_line = lexi_unknown_timezone_note(voice_mode=mode)
        else:
            tz_ask_line = (
                "I couldn't identify your time zone, so I've listed the options below "
                "in Mountain Time (MT).\n\n"
            )
    else:
        lines = "\n".join(
            f"• {format_slot_for_email(slot, recipient_tz=format_tz)}"
            for slot in slots[:3]
        )
        tz_note = ""

    if mode == "lexi":
        opening = (
            f"Hi {name},\n\n"
            "I'm Lexi, Kory's assistant. Thanks for reaching out — "
        )
        if context_line:
            opening = f"{opening}{context_line.lstrip()}"
        else:
            opening = f"{opening}a few options that work on Kory's end:\n\n"
    else:
        opening = f"Hi {name},\n\nThanks for reaching out — "
        if context_line:
            opening = f"{opening}{context_line.lstrip()}"
        else:
            opening = f"{opening}a few options that work on my end:\n\n"

    return normalize_draft_for_display(
        f"{tz_note}"
        f"{opening}"
        f"{tz_ask_line}"
        f"{lines}\n\n"
        "Let me know which works best and I can send a calendar invite.\n\n"
        f"{closing}",
        max_chars=None,
        voice_mode=mode,
    )


def normalize_draft_for_display(body: str, *, max_chars: int | None = 2000, voice_mode: str = "kory") -> str:
    """Normalize line breaks and sign-off for Teams previews and send."""
    from app.scheduling.lexi_voice import normalize_voice_mode

    if normalize_voice_mode(voice_mode) == "lexi":
        return finalize_lexi_email_body(body, max_chars=max_chars)
    return finalize_outbound_email_body(body, max_chars=max_chars)


def _strip_trailing_outlook_signature_block(text: str) -> str:
    """Remove Kory's rich Outlook signature if already present (avoid double sign-off)."""
    if not text.strip():
        return text
    lowered = text.lower()
    markers = (
        "see amazing founders",
        "kory mitchell - ceo",
        "kory mitchell – ceo",
        "kory mitchell — ceo",
        "iconic founders group",
        "denver, colorado",
        "preserving legacy",
        "m: 720",
        "p: 720",
    )
    cut_at = len(text)
    for marker in markers:
        idx = lowered.find(marker)
        if idx >= 0:
            cut_at = min(cut_at, idx)
    if cut_at < len(text):
        text = text[:cut_at].rstrip()
    patterns = [
        r"\n*See amazing founders.*$",
        r"\n*Kory Mitchell\s*[-–—]\s*CEO.*$",
        r"\n*Iconic Founders Group.*$",
        r"\n*Denver, Colorado.*$",
        r"\n*M:\s*720[-.\s]?561[-.\s]?0611.*$",
        r"\n*PRESERVING LEGACY.*$",
    ]
    cleaned = text
    for pattern in patterns:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE | re.DOTALL).rstrip()
    return cleaned


def _dedupe_lexi_opening(text: str) -> str:
    """Keep one Lexi scheduling intro — drop stray 'Hi,' lines and duplicate openers."""
    if not text.strip():
        return text
    lines = text.replace("\r\n", "\n").split("\n")
    cleaned: list[str] = []
    seen_lexi_intro = False
    for line in lines:
        stripped = line.strip()
        lower = stripped.lower()
        if stripped in {"Hi,", "Hi"}:
            continue
        is_lexi_intro = (
            lower.startswith("hi — i'm lexi")
            or lower.startswith("hi - i'm lexi")
            or lower.startswith("i'm lexi")
            or "thanks for reaching out" in lower
            or "a few options that work" in lower
            or "a few times that work" in lower
        )
        if is_lexi_intro:
            if seen_lexi_intro:
                continue
            seen_lexi_intro = True
        cleaned.append(line)
    return "\n".join(cleaned).strip()


def _dedupe_lexi_signoff(text: str) -> str:
    from app.scheduling.lexi_voice import LEXI_SIGNOFF_BLOCK

    block = LEXI_SIGNOFF_BLOCK
    while text.count(block) > 1:
        first = text.find(block)
        second = text.find(block, first + len(block))
        if second < 0:
            break
        text = (text[:second] + text[second + len(block) :]).strip()
    return text


def _dedupe_kory_signoff(text: str) -> str:
    sign_off = kory_rules.EMAIL_RULES.get("sign_off", "Let's Win")
    closing = f"{sign_off},\nKory"
    while text.lower().count(closing.lower()) > 1:
        idx = text.lower().rfind(closing.lower())
        if idx <= 0:
            break
        text = text[:idx].rstrip()
        if not text.lower().endswith(closing.lower()):
            text = f"{text}\n\n{closing}"
    return text


def _strip_all_lexi_closings(text: str) -> str:
    """Remove every Lexi sign-off block (LLM duplicates, pasted closings)."""
    from app.scheduling.lexi_voice import LEXI_SIGNOFF_BLOCK

    cleaned = text
    while LEXI_SIGNOFF_BLOCK in cleaned:
        cleaned = cleaned.replace(LEXI_SIGNOFF_BLOCK, "").strip()
    cleaned = _strip_lexi_closing(cleaned)
    return cleaned.rstrip()


def finalize_lexi_email_body(body: str, *, max_chars: int | None = None) -> str:
    """Lexi assistant email: paragraph spacing + standard Thank you / Lexi sign-off."""
    from app.scheduling.lexi_voice import LEXI_SIGNOFF_BLOCK

    text = (body or "").strip().replace("\r\n", "\n")
    if not text:
        return LEXI_SIGNOFF_BLOCK

    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = _strip_trailing_outlook_signature_block(text)
    text = _strip_all_lexi_closings(text)
    text = _strip_kory_closing(text)

    text = _dedupe_lexi_opening(text)
    main = _ensure_paragraph_spacing(text.rstrip())
    if not main.strip():
        # Avoid wiping the draft when signature-stripping heuristics misfire.
        main = _ensure_paragraph_spacing(
            (body or "").strip().replace("\r\n", "\n").rstrip()
        )
        main = _strip_all_lexi_closings(main)
        main = _strip_kory_closing(main).rstrip()
    closing = LEXI_SIGNOFF_BLOCK
    text = f"{main}\n\n{closing}" if main else closing
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    text = _dedupe_lexi_signoff(text)

    if max_chars is not None and len(text) > max_chars:
        return text[: max_chars - 1].rstrip() + "…"
    return text


def _strip_lexi_closing(text: str) -> str:
    from app.scheduling.lexi_voice import LEXI_SIGNOFF_BLOCK

    lowered = text.lower()
    block_lower = LEXI_SIGNOFF_BLOCK.lower()
    if lowered.endswith(block_lower):
        return text[: -len(LEXI_SIGNOFF_BLOCK)].rstrip()
    patterns = [
        r"(?:Best|Thank you),?\s*\n\s*Lexi(?:\s+Knightly)?(?:\s*\n.*)?$",
        r"Lexi(?:\s+Knightly)?\s*\n\s*(?:Executive Assistant|Assistant to Kory Mitchell).*$",
    ]
    for pattern in patterns:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE | re.DOTALL).rstrip()
    return text


def _strip_kory_closing(text: str) -> str:
    sign_off = kory_rules.EMAIL_RULES.get("sign_off", "Let's Win")
    return re.sub(
        rf"{re.escape(sign_off)},?\s*\n?\s*Kory\s*$",
        "",
        text,
        flags=re.IGNORECASE,
    ).rstrip()


def finalize_outbound_email_body(body: str, *, max_chars: int | None = None) -> str:
    """Production email body: paragraph spacing + Let's Win, / Kory on separate lines."""
    text = (body or "").strip().replace("\r\n", "\n")
    if not text:
        return ""

    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = _strip_trailing_outlook_signature_block(text)

    sign_off = kory_rules.EMAIL_RULES.get("sign_off", "Let's Win")
    closing_pattern = re.compile(
        rf"({re.escape(sign_off)}),?\s*\n?\s*Kory\s*$",
        flags=re.IGNORECASE,
    )
    match = closing_pattern.search(text)
    if match:
        main = text[: match.start()].rstrip()
        closing = f"{sign_off},\nKory"
    else:
        main = _strip_lexi_closing(text).rstrip()
        # A draft that already ends with a bare "Kory" (or "Best,/Thanks,\nKory")
        # must lose that line, or appending the canonical block doubles the
        # sign-off: "...Kory\n\nLet's Win,\nKory" (live L-1).
        main = re.sub(
            r"\n\s*(?:(?:Best|Thanks|Thank you|Warmly|Regards|Cheers)[,!]?\s*\n\s*)?"
            r"[-–—]*\s*Kory(?:\s+Mitchell)?\s*$",
            "",
            main,
            flags=re.IGNORECASE,
        ).rstrip()
        closing = f"{sign_off},\nKory"

    main = _ensure_paragraph_spacing(main)
    text = f"{main}\n\n{closing}" if main else closing

    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    text = _dedupe_kory_signoff(text)
    if max_chars is not None and len(text) > max_chars:
        return text[: max_chars - 1].rstrip() + "…"
    return text


def _ensure_paragraph_spacing(text: str) -> str:
    """Blank line between paragraphs; preserve intentional single lines (bullets, short lines)."""
    if not text.strip():
        return ""

    blocks = re.split(r"\n\n+", text.strip())
    spaced: list[str] = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        if block.startswith("•") or block.startswith("-") or block.startswith("*"):
            spaced.append(block)
            continue
        if block.count("\n") == 0:
            spaced.append(block)
            continue
        lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        if len(lines) <= 1:
            spaced.append(block)
            continue
        merged: list[str] = []
        for line in lines:
            if merged and len(line) < 60 and not line.endswith((".", "!", "?")):
                merged[-1] = f"{merged[-1]} {line}"
            else:
                merged.append(line)
        spaced.append("\n\n".join(merged))
    return "\n\n".join(spaced)


def sender_first_name(sender: str | None) -> str:
    if not sender:
        return "there"
    local = sender.split("@", 1)[0]
    local = re.sub(r"[._+-]+", " ", local).strip()
    if not local:
        return "there"
    first = local.split()[0]
    # A run-together mailbox ("anjanakummetha") is a full name, not a first name —
    # greeting someone by it reads worse than a neutral "there".
    if any(ch.isdigit() for ch in first) or len(first) > 12:
        return "there"
    return first.title()


_BLOCKED_SIGNATURE_NAMES = frozenset(
    {
        "body", "html", "div", "span", "table", "there", "unknown", "hi", "hello",
        # Sign-off words must never be mistaken for the recipient's name.
        "thanks", "thank", "thankyou", "best", "regards", "cheers",
        "sincerely", "warmly", "br", "kind", "many",
    }
)


def recipient_display_name(
    sender_email: str | None,
    body: str = "",
    *,
    fallback_first_name: str = "",
) -> str:
    """Prefer signature / sign-off name from the inbound email, then mailbox local-part."""
    from app.integrations.outlook_email import _extract_signature_name

    text = (body or "").strip()
    same_line = re.search(
        r"(?:thanks|thank you|best|regards|cheers),?\s*([A-Za-z][A-Za-z'-]{0,24})\s*$",
        text,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    if not same_line:
        same_line = re.search(
            r"(?:thanks|thank you),?\s*([A-Za-z][A-Za-z'-]{0,24})\s*(?:\n|$)",
            text,
            flags=re.IGNORECASE,
        )
    if same_line:
        candidate = same_line.group(1).strip().title()
        if candidate.lower() not in _BLOCKED_SIGNATURE_NAMES:
            return candidate

    signature_name = _extract_signature_name(text)
    if signature_name:
        first = signature_name.split()[0].title()
        if first.lower() not in _BLOCKED_SIGNATURE_NAMES:
            return first

    profile_name = _profile_first_name(sender_email)
    if profile_name:
        return profile_name

    if fallback_first_name.strip():
        fb = fallback_first_name.strip().title()
        if fb.lower() not in _BLOCKED_SIGNATURE_NAMES:
            return fb
    return sender_first_name(sender_email)


def _profile_first_name(sender_email: str | None) -> str:
    """First name from the stored Outlook display name, if we've seen this contact."""
    if not sender_email:
        return ""
    try:
        from app.storage.recipient_profiles import get_recipient_profile

        profile = get_recipient_profile(sender_email) or {}
    except Exception:
        return ""
    stored = str(profile.get("display_name") or "").strip()
    if not stored:
        return ""
    first = stored.split()[0].title()
    return "" if first.lower() in _BLOCKED_SIGNATURE_NAMES else first


def example_draft_preview() -> dict[str, Any]:
    """Static example for docs / demos (diligence coffee-style thread)."""
    slots = [
        {
            "start": "2026-06-17T14:00:00-06:00",
            "end": "2026-06-17T14:30:00-06:00",
        },
        {
            "start": "2026-06-18T15:00:00-06:00",
            "end": "2026-06-18T15:30:00-06:00",
        },
        {
            "start": "2026-06-19T13:00:00-06:00",
            "end": "2026-06-19T13:30:00-06:00",
        },
    ]
    body = build_scheduling_reply(
        recipient_first_name="Bill",
        slots=slots,
        sender_email="bill.heermann@newportadvisors.co",
        meeting_context="Happy to connect on Project Paint diligence.",
    )
    return {
        "inbound_subject": "RE: diligence call and organization for Project Paint",
        "inbound_from": "bill.heermann@newportadvisors.co",
        "draft_body": body,
        "format_rules": [
            "Recipient timezone first (Eastern for newportadvisors.co), MT in parentheses",
            f"Sign-off: {kory_rules.EMAIL_RULES.get('sign_off')}, then Kory on its own line",
            "Bullet options (2–3 slots)",
            "Never mention YPO in outgoing drafts",
        ],
    }
