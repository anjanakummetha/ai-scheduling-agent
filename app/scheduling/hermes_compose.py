"""Hermes intelligence layer for scheduling email drafts and Kory guidance.

Calendar truth (slots, validators, holds) stays in slot_engine — this module
only writes prose from a structured facts packet.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.config import settings
from app.llm.hermes_client import get_hermes_client
from app.scheduling.email_format import format_slot_for_email, recipient_display_name, sender_first_name
from app.scheduling.lexi_voice import normalize_voice_mode, voice_instruction_for_mode
from app.llm.kory_voice import voice_prompt_block
from app.storage.lexi_db import get_lexi_connection

import rules as kory_rules


def _resolve_greeting_name(stored_name: str | None, sender_email: str, body: str) -> str:
    """Greeting name for the counterpart. Prefer a real stored display name (e.g. a
    delegation counterpart's To-recipient name) over mining the body — the body may
    be Kory's delegation note signed by Kory. Fall back to the standard resolver."""
    stored = (stored_name or "").strip()
    email_local = sender_email.split("@")[0].lower() if sender_email else ""
    if stored and "@" not in stored and stored.lower() != email_local:
        return stored.split()[0]
    return recipient_display_name(
        sender_email, body, fallback_first_name=sender_first_name(sender_email)
    )


def _thread_sender_name(thread_id: str) -> str:
    if not thread_id:
        return ""
    try:
        with get_lexi_connection() as conn:
            row = conn.execute(
                "SELECT sender FROM email_threads WHERE thread_id = ?", (thread_id,)
            ).fetchone()
        return str(row[0]) if row and row[0] else ""
    except Exception:
        return ""


def build_scheduling_context_packet(proposal_id: int) -> dict[str, Any]:
    """Facts packet for Hermes compose — thread, type, rules, state."""
    with get_lexi_connection() as conn:
        row = conn.execute(
            """
            SELECT
                p.id AS proposal_id,
                p.thread_id,
                p.status,
                p.intent_classification,
                p.priority_tier,
                p.proposed_slots,
                p.drafted_reply,
                p.voice_mode,
                p.send_channel,
                p.is_delegation,
                p.recipient_timezone,
                p.kory_scheduling_guidance,
                e.subject,
                e.sender,
                e.sender_email,
                e.raw_body,
                e.conversation_id,
                e.received_at,
                e.internet_headers_json
            FROM proposals AS p
            INNER JOIN email_threads AS e ON e.thread_id = p.thread_id
            WHERE p.id = ?
            """,
            (proposal_id,),
        ).fetchone()
    if not row:
        return {"ok": False, "error": f"Proposal {proposal_id} not found."}

    bundle = dict(row)
    subject = str(bundle.get("subject") or "")
    body = str(bundle.get("raw_body") or "")
    sender = str(bundle.get("sender_email") or bundle.get("sender") or "")
    intent = str(bundle.get("intent_classification") or "unknown")
    conversation_id = str(bundle.get("conversation_id") or "").strip()

    from app.scheduling.meeting_type import resolve_meeting_type

    meeting = resolve_meeting_type(intent=intent, subject=subject, body=body)
    thread_context = _load_thread_context(conversation_id, bundle.get("thread_id"))

    from app.scheduling.timezone_intel import (
        extract_internet_headers,
        is_timezone_uncertain,
        lookup_recipient_timezone,
    )
    from app.scheduling.email_format import MT

    headers = _load_stored_internet_headers(bundle)
    tz_result = lookup_recipient_timezone(
        sender_email=sender,
        body=body,
        internet_headers=headers,
        received_at=str(bundle.get("received_at") or "") or None,
        stored_timezone=str(bundle.get("recipient_timezone") or "") or None,
        for_scheduling=True,
    )
    uncertain = is_timezone_uncertain(tz_result)
    from app.scheduling.email_format import (
        format_offer_slot_block,
        lexi_unknown_timezone_note,
        should_note_mt_only_timezone,
    )
    from app.scheduling.slot_engine import infer_meeting_format

    meeting_fmt = infer_meeting_format(
        meeting.type_key,
        subject=subject,
        body=body,
    )
    mt_only_note = should_note_mt_only_timezone(
        sender_email=sender,
        uncertain=uncertain,
        tz_confidence=tz_result.confidence,
        tz_source=tz_result.source,
    )
    format_tz = MT if mt_only_note else (tz_result.timezone or MT)

    slots = _parse_slots(bundle.get("proposed_slots"))
    slot_block = format_offer_slot_block(slots, recipient_tz=format_tz)

    display_name = _resolve_greeting_name(bundle.get('sender'), sender, body)

    return {
        "ok": True,
        "proposal_id": proposal_id,
        "subject": subject,
        "sender": sender,
        "recipient_display_name": display_name,
        "thread_context": thread_context,
        "latest_inbound_body": body,
        "meeting_type_key": meeting.type_key,
        "meeting_type_label": meeting.draft_type_label(),
        "intent_classification": intent,
        "voice_mode": str(bundle.get("voice_mode") or "lexi"),
        "send_channel": str(bundle.get("send_channel") or "lexi"),
        "is_delegation": bool(bundle.get("is_delegation")),
        "lexi_already_on_thread": _lexi_on_thread(thread_context, body),
        "recipient_timezone": tz_result.tz_name() or str(MT),
        "recipient_timezone_confidence": tz_result.confidence,
        "recipient_timezone_source": tz_result.source,
        "timezone_uncertain": mt_only_note,
        "timezone_note": lexi_unknown_timezone_note(
            voice_mode=str(bundle.get("voice_mode") or "lexi"),
        )
        if mt_only_note
        else "",
        "offered_slots": slots,
        "offered_times_block": slot_block,
        "scheduling_rules_summary": _rules_summary_for_type(meeting.type_key),
        "soft_blocks_summary": _soft_blocks_summary(),
        "policy_block_reason": _policy_block_reason(
            meeting.type_key,
            guidance=str(bundle.get("kory_scheduling_guidance") or ""),
        ),
        "kory_scheduling_guidance": str(bundle.get("kory_scheduling_guidance") or "").strip(),
        "status": str(bundle.get("status") or ""),
    }


def _policy_block_reason(meeting_type_key: str, *, guidance: str = "") -> str:
    """The rule (not the calendar) that blocks this request, stated plainly.

    Live C-3 defect: a lunch ask escalated as 'no lunch slots came back — zero
    candidates' and the composer improvised calendar fixes (wider weeks, moving
    Kory's blocks) for a block that Kory's own standing rule causes. When the
    cause is policy, the composer must say so — no calendar change can fix it.
    """
    from app.scheduling.preferences import load_scheduling_preferences

    prefs = load_scheduling_preferences(guidance=guidance)
    if meeting_type_key == "lunch" and not prefs.lunch_allowed:
        return (
            "Kory's standing rule: lunch meetings are exception-only, so the "
            "scheduler offers no lunch slots regardless of calendar availability. "
            "The options are: Kory approves a lunch exception for this request, "
            "or Lexi offers a non-lunch time instead."
        )
    return ""


def compose_offer_email_with_hermes(
    *,
    proposal_sender: str | None,
    proposal_subject: str,
    proposal_body: str,
    thread_id: str,
    slots: list[dict[str, str]],
    voice_mode: str = "lexi",
    stored_recipient_timezone: str | None = None,
    intent: str | None = None,
    conversation_id: str | None = None,
) -> tuple[str, str]:
    """Draft scheduling offer email using Hermes intelligence + frozen slots."""
    from app.scheduling.timezone_intel import (
        extract_internet_headers,
        is_timezone_uncertain,
        lookup_recipient_timezone,
    )
    from app.scheduling.email_format import (
        MT,
        format_offer_slot_block,
        lexi_unknown_timezone_note,
        should_note_mt_only_timezone,
    )
    from app.scheduling.meeting_type import resolve_meeting_type

    headers = None if stored_recipient_timezone else _fetch_message_headers(thread_id)
    tz_result = lookup_recipient_timezone(
        sender_email=proposal_sender,
        body=proposal_body,
        internet_headers=headers,
        stored_timezone=stored_recipient_timezone,
        for_scheduling=True,
    )
    meeting = resolve_meeting_type(
        intent=intent,
        subject=proposal_subject,
        body=proposal_body,
    )
    from app.scheduling.slot_engine import infer_meeting_format

    meeting_fmt = infer_meeting_format(
        meeting.type_key,
        subject=proposal_subject,
        body=proposal_body,
    )
    uncertain = is_timezone_uncertain(tz_result)
    mt_only_note = should_note_mt_only_timezone(
        sender_email=proposal_sender,
        uncertain=uncertain,
        tz_confidence=tz_result.confidence,
        tz_source=tz_result.source,
    )
    format_tz = MT if mt_only_note else (tz_result.timezone or MT)
    slot_block = format_offer_slot_block(slots, recipient_tz=format_tz)

    thread_context = _load_thread_context(conversation_id or "", thread_id)
    if not conversation_id and thread_id:
        conversation_id = _conversation_id_for_thread(thread_id)
        if conversation_id:
            thread_context = _load_thread_context(conversation_id, thread_id)

    display_name = _resolve_greeting_name(_thread_sender_name(thread_id), proposal_sender, proposal_body)

    packet = {
        "subject": proposal_subject,
        "recipient_display_name": display_name,
        "thread_context": thread_context,
        "latest_inbound_body": proposal_body,
        "meeting_type_label": meeting.draft_type_label(),
        "voice_mode": voice_mode,
        "lexi_already_on_thread": _lexi_on_thread(thread_context, proposal_body),
        "offered_slots": slots,
        "offered_times_block": slot_block,
        "timezone_uncertain": mt_only_note,
        "timezone_note": lexi_unknown_timezone_note(voice_mode=voice_mode) if mt_only_note else "",
        "recipient_timezone_confidence": tz_result.confidence,
        "recipient_timezone_source": tz_result.source,
        "intent_classification": intent or meeting.type_key,
        "meeting_format": meeting_fmt,
        "scheduling_rules_summary": _rules_summary_for_type(meeting.type_key),
    }

    if not settings.llm_api_key:
        return _template_fallback_offer(
            display_name,
            slot_block,
            voice_mode,
            timezone_note=lexi_unknown_timezone_note(voice_mode=voice_mode) if mt_only_note else "",
        ), "template_fallback"

    try:
        draft = _hermes_offer_compose(
            packet,
            voice_mode=voice_mode,
            sender_email=proposal_sender,
            slots=slots,
        )
        draft = _enforce_offered_times_block(draft, slot_block)
        if draft and _draft_includes_slot_block(draft, slot_block, slots):
            return draft, "hermes"
    except Exception:
        pass

    return _template_fallback_offer(
        display_name,
        slot_block,
        voice_mode,
        timezone_note=lexi_unknown_timezone_note(voice_mode=voice_mode) if mt_only_note else "",
    ), "template_fallback"


def compose_kory_guidance_with_hermes(
    proposal_id: int,
    *,
    failure_error: str = "",
    intent: str = "",
) -> str:
    """Intelligent Teams message to Kory when slots could not be found."""
    packet = build_scheduling_context_packet(proposal_id)
    if not packet.get("ok"):
        return failure_error or "I couldn't find times in that window."

    packet["scheduler_failure"] = (failure_error or "").strip()
    packet["failure_intent"] = intent

    if not settings.llm_api_key:
        return _template_fallback_guidance(packet)

    try:
        text = _hermes_kory_guidance_compose(packet)
        if text and len(text.strip()) > 20:
            return text.strip()
    except Exception:
        pass

    return _template_fallback_guidance(packet)


def get_scheduling_context_for_proposal(proposal_id: int) -> dict[str, Any]:
    """Public API for MCP / Hermes chat tools."""
    return build_scheduling_context_packet(proposal_id)


def _hermes_offer_compose(
    packet: dict[str, Any],
    *,
    voice_mode: str,
    sender_email: str | None,
    slots: list[dict[str, str]] | None = None,
) -> str:
    mode = normalize_voice_mode(voice_mode)
    slot_block = str(packet.get("offered_times_block") or "")
    offered_slots = slots or _parse_slots(packet.get("offered_slots"))
    system = (
        "You are Hermes, Kory Mitchell's scheduling intelligence — drafting an email reply.\n"
        "Calendar times are ALREADY CHOSEN. You MUST include the offered_times_block "
        "verbatim (every bullet line, unchanged).\n"
        "Do NOT add, remove, or change any offered times.\n"
        "Use recipient_display_name for the greeting (not the mailbox local-part).\n"
        "If lexi_already_on_thread is true, do NOT re-introduce Lexi — continue the thread naturally.\n"
        "If lexi_already_on_thread is false and voice_mode is lexi, one brief Lexi intro is OK — "
        "introduce her as \"Kory's assistant\", never \"Kory's Executive Assistant\".\n"
        "If timezone_uncertain is true: include timezone_note from the packet, then "
        "offered_times_block verbatim. Times are Mountain Time only — do NOT add ET/CT/PT "
        "parentheticals or claim equivalents are shown.\n"
        "If timezone_uncertain is false: do NOT say you are uncertain of their time zone. "
        "Include offered_times_block verbatim.\n"
        "Reference thread_context when useful; do not repeat information the recipient already knows.\n"
        "Describe WHEN the offered times fall truthfully, by their actual dates. If they fall "
        "outside the window the sender asked for (scheduling_note explains this), acknowledge it "
        "honestly ('the next couple of weeks are full — here's what's open after that'); NEVER "
        "claim times are 'next week' or 'this week' unless the dates actually are.\n"
        "Venues: Kory's coffee/drinks/dinner spots are in Cherry Creek, Denver. Only suggest them "
        "when the meeting is in-person in the Denver area. If the sender or thread points to a "
        "different city, do NOT attach Denver venues or invent a location — leave the venue "
        "question open or offer a call instead.\n"
        "End with a line inviting them to pick a time for a calendar invite.\n"
        + voice_instruction_for_mode(mode)
        + "\n"
        + (voice_prompt_block(recipient_email=sender_email) if mode == "kory" else "")
        + "\nReturn ONLY the email body plain text — no Subject line, no markdown fences."
    )
    client = get_hermes_client()
    response = client.chat.completions.create(
        model=settings.llm_model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(packet, default=str)},
        ],
        temperature=0.35,
    )
    text = (response.choices[0].message.content or "").strip()
    if text.lower().startswith("subject:"):
        text = re.sub(r"^subject:\s*.+\n+", "", text, flags=re.I).strip()

    slot_block = _canonical_slot_block(packet, offered_slots, sender_email)
    text = _enforce_offered_times_block(text, slot_block)
    if slot_block and slot_block not in text:
        name = packet.get("recipient_display_name") or "there"
        text = (
            f"Hi {name},\n\n{text.rstrip()}\n\n{slot_block}\n\n"
            "Let me know which works best and I can send a calendar invite."
        )
    return text


def _canonical_slot_block(
    packet: dict[str, Any],
    slots: list[dict[str, str]],
    sender_email: str | None,
) -> str:
    from app.scheduling.email_format import MT, format_offer_slot_block, should_note_mt_only_timezone

    mt_only = bool(packet.get("timezone_uncertain")) or should_note_mt_only_timezone(
        sender_email=sender_email,
        uncertain=bool(packet.get("timezone_uncertain")),
        tz_confidence=str(packet.get("recipient_timezone_confidence") or ""),
        tz_source=str(packet.get("recipient_timezone_source") or ""),
    )
    format_tz = MT if mt_only else None
    if format_tz is None:
        try:
            from zoneinfo import ZoneInfo

            format_tz = ZoneInfo(str(packet.get("recipient_timezone") or str(MT)))
        except Exception:
            format_tz = MT
    return format_offer_slot_block(slots, recipient_tz=format_tz)


def _format_slot_block(
    slots: list[dict[str, str]],
    format_tz: Any,
    *,
    uncertain: bool = False,
) -> str:
    from app.scheduling.email_format import format_offer_slot_block

    return format_offer_slot_block(slots, recipient_tz=format_tz)


def _enforce_offered_times_block(draft: str, slot_block: str) -> str:
    """Replace any bullet time list with the canonical offered_times_block."""
    if not slot_block.strip():
        return draft
    lines = draft.splitlines()
    bullet_idxs = [i for i, line in enumerate(lines) if line.strip().startswith("•")]
    if bullet_idxs:
        start = bullet_idxs[0]
        end = bullet_idxs[-1]
        merged = lines[:start] + slot_block.splitlines() + lines[end + 1 :]
        return "\n".join(merged).strip()
    return draft


def _hermes_kory_guidance_compose(packet: dict[str, Any]) -> str:
    system = (
        "You are Hermes, Kory's scheduling assistant writing a SHORT message TO KORY in Teams "
        "(not an email to the prospect).\n"
        "The calendar search found NO valid times to offer yet.\n"
        "If policy_block_reason is non-empty, that IS the reason — lead with it, and do NOT "
        "attribute the block to a busy calendar, suggest wider-week searches, or suggest moving "
        "meetings: no calendar change can fix a policy block. Offer exactly the choices the "
        "policy states (e.g. approve an exception, or offer a non-lunch time).\n"
        "Otherwise use scheduler_failure, meeting_type_label, scheduling_rules_summary, "
        "soft_blocks_summary, and thread_context to explain WHY and offer 2-3 specific next steps.\n"
        "Do NOT use the same generic line every time. Be specific (e.g. coffee mornings packed, "
        "WOB block conflict, try week after).\n"
        "Do NOT draft an email to the prospect. Do NOT invent times.\n"
        "NEVER propose changing the meeting type to get an easier slot — do not suggest turning "
        "a coffee into a call or a video meeting. Kory asks for coffee with people he already "
        "knows and it needs to be booked as coffee. Suggest a different time or a different week. "
        "Only suggest moving one of Kory's own meetings as a last resort, phrased as a question, "
        "and never for anything marked DO NOT MOVE, his training sessions, or his executive "
        "coach.\n"
        "You are writing directly to Kory — issues route to Kory only. NEVER suggest "
        "any other assistant/colleague, and never claim anyone has been flagged, notified, or "
        "escalated.\n"
        "2-4 sentences. Plain text. No markdown headers."
    )
    client = get_hermes_client()
    response = client.chat.completions.create(
        model=settings.llm_model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(packet, default=str)},
        ],
        temperature=0.4,
    )
    return (response.choices[0].message.content or "").strip()


def _load_stored_internet_headers(bundle: dict[str, Any]) -> list[dict[str, Any]] | None:
    raw = bundle.get("internet_headers_json")
    if raw:
        try:
            parsed = json.loads(str(raw))
            if isinstance(parsed, list):
                return [h for h in parsed if isinstance(h, dict)]
        except (TypeError, json.JSONDecodeError):
            pass
    thread_id = str(bundle.get("thread_id") or "")
    if thread_id:
        return _fetch_message_headers(thread_id)
    return None


def _conversation_id_for_thread(thread_id: str) -> str:
    if not thread_id:
        return ""
    with get_lexi_connection() as conn:
        row = conn.execute(
            "SELECT conversation_id FROM email_threads WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
    return str(row["conversation_id"] or "") if row else ""


def _load_thread_context(conversation_id: str | None, thread_id: str | None) -> str:
    if conversation_id:
        from app.integrations.outlook_thread import fetch_conversation_context

        ctx = fetch_conversation_context(
            conversation_id,
            exclude_message_id=str(thread_id or ""),
            max_messages=10,
        )
        if ctx:
            return ctx
    return ""


def _fetch_message_headers(thread_id: str) -> list[dict[str, Any]] | None:
    if not thread_id:
        return None
    try:
        from app.integrations.outlook_email import get_message

        full_message, _ = get_message(thread_id)
        return extract_internet_headers(full_message)
    except Exception:
        return None


def _lexi_on_thread(thread_context: str, body: str) -> bool:
    combined = f"{thread_context}\n{body}".lower()
    return "lexi@iconicfounders.com" in combined or "i'm lexi" in combined or "i am lexi" in combined


def _parse_slots(raw: Any) -> list[dict[str, str]]:
    if isinstance(raw, list):
        return [s for s in raw if isinstance(s, dict) and s.get("start")]
    if not raw:
        return []
    try:
        parsed = json.loads(str(raw))
    except (TypeError, json.JSONDecodeError):
        return []
    if isinstance(parsed, list):
        return [s for s in parsed if isinstance(s, dict) and s.get("start")]
    return []


def _rules_summary_for_type(type_key: str) -> str:
    cfg = dict(kory_rules.MEETING_TYPES.get(type_key) or {})
    if not cfg:
        return f"Meeting type: {type_key}"
    parts = [str(cfg.get("label") or type_key)]
    if cfg.get("duration_minutes"):
        parts.append(f"{cfg['duration_minutes']} minutes")
    if cfg.get("calendar_block_minutes") and cfg.get("calendar_block_minutes") != cfg.get("duration_minutes"):
        parts.append(f"{cfg['calendar_block_minutes']}-minute calendar block")
    if cfg.get("format"):
        parts.append(f"format: {cfg['format']}")
    if cfg.get("locations"):
        parts.append(f"locations: {', '.join(cfg['locations'][:2])}")
    if cfg.get("notes"):
        parts.append(str(cfg["notes"]))
    return "; ".join(parts)


def _soft_blocks_summary() -> str:
    names = [str(b.get("name") or "") for b in kory_rules.SOFT_BLOCKS if b.get("movable")]
    return "Movable if Kory approves: " + ", ".join(names) if names else ""


def _draft_includes_slot_block(draft: str, slot_block: str, slots: list[dict[str, str]]) -> bool:
    if not draft or not slots:
        return bool(draft.strip())
    if slot_block not in draft and "•" not in draft:
        return False
    for slot in slots[:3]:
        try:
            from datetime import datetime

            day = datetime.fromisoformat(str(slot["start"]).replace("Z", "+00:00")).strftime("%A")
            if day not in draft:
                return False
        except (TypeError, ValueError, KeyError):
            continue
    return True


def _template_fallback_offer(
    name: str,
    slot_block: str,
    voice_mode: str,
    *,
    timezone_note: str = "",
) -> str:
    from app.scheduling.lexi_voice import normalize_voice_mode

    intro = ""
    slots_line = "I have a few times that work:"
    if normalize_voice_mode(voice_mode) == "lexi":
        intro = "I'm Lexi, Kory's assistant — happy to help find a time.\n\n"
        slots_line = "I have a few times that work on Kory's end:"
    tz_line = timezone_note.strip()
    if tz_line:
        tz_line = f"{tz_line}\n\n"
    return (
        f"Hi {name},\n\n{intro}{tz_line}"
        f"{slots_line}\n\n"
        f"{slot_block}\n\n"
        "Let me know which works best and I can send a calendar invite."
    )


def _template_fallback_guidance(packet: dict[str, Any]) -> str:
    label = packet.get("meeting_type_label") or "meeting"
    subject = packet.get("subject") or "this thread"
    return (
        f"I couldn't find times for the {label} ({subject}). "
        "Want me to try a different week, or use a movable block like WOB if it's urgent?"
    )
