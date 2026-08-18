"""Lexi Phase 2: inbound email triage, classification, and proposal staging."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
import re
import sqlite3
import time
import traceback
from typing import Any

from app.config import settings
from app.llm.hermes_client import get_hermes_client
from app.rules.rule_engine import is_priority_contact, load_rules
from app.scheduling.proposal_state import ProposalStatus
from app.storage.lexi_db import get_lexi_connection

TRIAGE_STATUS = ProposalStatus.PENDING_TRIAGE
AWAITING_REPLY_PROMPT = ProposalStatus.AWAITING_REPLY_PROMPT

VALID_INTENTS = frozenset(
    {
        "board_meeting",
        "dinner_request",
        "lunch_request",
        "pitch",
        "internal_sync",
        "coffee",
        "happy_hour",
        "reschedule",
        "cancellation",
        "delegation",
        "non_scheduling",
        "unknown",
        "referral_or_intro",
        "meeting_request",
        "podcast",
    }
)
VALID_PRIORITIES = frozenset({"high", "medium", "low"})
NON_SCHEDULING_INTENTS = frozenset({"non_scheduling"})

PRIORITY_KEYWORDS = (
    "investor",
    "term sheet",
    "diligence",
    "board meeting",
    "ic prep",
    "acquisition",
    "loi",
    "portfolio company",
)

PRIORITY_EMAIL_DOMAINS = (
    "@iconicfounders.com",
    "@ifg.vc",
)

TRIAGE_SYSTEM_PROMPT = """You are Lexi, an executive scheduling triage engine for a CEO inbox.
Analyze the inbound email subject and body.

Return ONLY a single valid JSON object with exactly these keys:
- intent: string — one of: referral_or_intro, meeting_request, pitch, new_client, coffee, happy_hour, dinner_request, lunch_request, podcast, internal_sync, board_meeting, reschedule, cancellation, delegation, non_scheduling, unknown
  Use referral_or_intro for 30-minute intro/referral calls. Use pitch or meeting_request for investor/diligence/deal calls (60 min). Use coffee, happy_hour, dinner_request ONLY when the sender explicitly asks for that meeting type (the word coffee / drinks / dinner, or an unambiguous in-person meal request). Kory's default meeting format is VIRTUAL: an ambiguous ask ("quick sync", "grab 30 minutes", "catch up") is referral_or_intro, never coffee — misclassifying books a 60-minute in-person block with venues instead of a 30-minute call. Use podcast for The Turn podcast recording requests.
- priority: string, exactly one of high, medium, low
- confidence_score: float between 0.0 and 1.0
- justification: string, one sentence explaining intent and priority

Do not include markdown fences or any text outside the JSON object."""


@dataclass(frozen=True)
class TriageResult:
    intent: str
    priority: str
    confidence_score: float
    justification: str
    source: str = "llm"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def process_new_email(raw_email: dict[str, Any]) -> int | None:
    """Parse an inbound email, triage via LLM + rules, persist proposal, audit.

    Returns proposal id for every inbound email (status awaiting_reply_prompt).
    Hermes/Teams asks Kory whether to draft a reply before scheduling or sending.
    """
    started = time.perf_counter()
    normalized = _normalize_raw_email(raw_email)
    thread_id = normalized["thread_id"]

    with get_lexi_connection() as conn:
        llm_error: str | None = None
        llm_traceback: str | None = None

        try:
            triage = _call_llm_triage(normalized["subject"], normalized["raw_body"])
        except Exception as exc:
            llm_error = f"{type(exc).__name__}: {exc}"
            llm_traceback = traceback.format_exc()
            triage = _fallback_triage(
                llm_error,
                subject=normalized["subject"],
                body=normalized["raw_body"],
            )

        _ensure_thread(conn, normalized)
        final_priority, rule_reasoning = _apply_rule_checklist(
            sender=normalized["sender"],
            subject=normalized["subject"],
            body=normalized["raw_body"],
            triage=triage,
        )

        from app.agents.inbound_filter import triage_adjustments_for_sender_subject

        adjusted_intent, adjusted_priority = triage_adjustments_for_sender_subject(
            sender=normalized["sender"],
            subject=normalized["subject"],
            body=normalized["raw_body"],
            intent=triage.intent,
            priority=final_priority,
        )
        if adjusted_intent != triage.intent or adjusted_priority != final_priority:
            triage = TriageResult(
                intent=adjusted_intent,
                priority=adjusted_priority,
                confidence_score=triage.confidence_score,
                justification=(
                    f"{triage.justification} (adjusted: newsletter/low-signal filter.)"
                ),
                source=triage.source,
            )
            final_priority = adjusted_priority

        triage = _correct_misclassified_scheduling_triage(
            triage,
            sender=normalized["sender"],
            subject=normalized["subject"],
            body=normalized["raw_body"],
        )
        final_priority = _scheduling_priority_floor(
            triage.priority,
            intent=triage.intent,
            sender=normalized["sender"],
        )
        if final_priority != triage.priority:
            triage = TriageResult(
                intent=triage.intent,
                priority=final_priority,
                confidence_score=triage.confidence_score,
                justification=triage.justification,
                source=triage.source,
            )

        proposal_id = _insert_proposal(
            conn,
            thread_id=thread_id,
            triage=triage,
            final_priority=final_priority,
            rule_reasoning=rule_reasoning,
            recipient_timezone=normalized.get("recipient_timezone") or None,
        )
        duration_ms = round((time.perf_counter() - started) * 1000, 2)

        if llm_error:
            _insert_audit_log(
                conn,
                step_name="triage_detection",
                reference_id=thread_id,
                log_level="ERROR",
                message="LLM triage failed; stored safe defaults on proposal.",
                payload={
                    "thread_id": thread_id,
                    "proposal_id": proposal_id,
                    "duration_ms": duration_ms,
                    "intent": triage.intent,
                    "priority": final_priority,
                    "error": llm_error,
                    "traceback": llm_traceback,
                },
            )
        else:
            _insert_audit_log(
                conn,
                step_name="triage_detection",
                reference_id=thread_id,
                log_level="INFO",
                message="Triage completed successfully.",
                payload={
                    "thread_id": thread_id,
                    "proposal_id": proposal_id,
                    "duration_ms": duration_ms,
                    "intent": triage.intent,
                    "priority": final_priority,
                    "confidence_score": triage.confidence_score,
                    "justification": triage.justification,
                    "triage_source": triage.source,
                    "rule_reasoning": rule_reasoning,
                },
            )

        _maybe_dispatch_asana_booking_reminder(
            conn,
            normalized=normalized,
            proposal_id=proposal_id,
            intent=triage.intent,
        )

        conn.commit()
        return proposal_id


def _normalize_raw_email(raw_email: dict[str, Any]) -> dict[str, str]:
    thread_id = str(raw_email.get("thread_id") or raw_email.get("outlook_message_id") or "").strip()
    if not thread_id:
        raise ValueError("raw_email must include thread_id or outlook_message_id")

    sender = str(
        raw_email.get("sender")
        or raw_email.get("sender_email")
        or raw_email.get("from")
        or ""
    ).strip()
    subject = str(raw_email.get("subject") or "").strip()
    received_at = str(raw_email.get("received_at") or "").strip()
    raw_body = str(raw_email.get("raw_body") or raw_email.get("body") or "").strip()
    conversation_id = str(raw_email.get("conversation_id") or "").strip()
    message_id = str(raw_email.get("message_id") or thread_id).strip()

    recipient_timezone = str(raw_email.get("recipient_timezone") or "").strip() or None
    if not recipient_timezone:
        from app.scheduling.timezone_intel import resolve_recipient_timezone_at_ingest

        tz_result = resolve_recipient_timezone_at_ingest(
            sender_email=sender,
            body=raw_body,
            internet_headers=raw_email.get("internet_message_headers"),
            received_at=received_at or None,
            exclude_thread_id=thread_id,
        )
        if tz_result.confidence != "unknown":
            recipient_timezone = tz_result.tz_name()

    if conversation_id:
        from app.integrations.outlook_thread import fetch_conversation_context

        prior = fetch_conversation_context(
            conversation_id,
            exclude_message_id=message_id,
            max_messages=3,
        )
        if prior:
            raw_body = f"{raw_body}\n\n[Prior messages in this email chain]\n{prior}"

    headers_raw = raw_email.get("internet_message_headers") or raw_email.get("internetMessageHeaders")
    headers_json = ""
    if isinstance(headers_raw, list):
        import json as _json

        headers_json = _json.dumps(headers_raw, default=str)

    from app.storage.recipient_profiles import normalize_sender_email

    sender_email_norm = normalize_sender_email(sender) or sender

    return {
        "thread_id": thread_id,
        "message_id": message_id,
        "conversation_id": conversation_id,
        "subject": subject,
        "sender": sender,
        "sender_email": sender_email_norm,
        "received_at": received_at,
        "raw_body": raw_body,
        "recipient_timezone": recipient_timezone or "",
        "internet_headers_json": headers_json,
    }


def _ensure_thread(conn: sqlite3.Connection, email: dict[str, str]) -> None:
    existing = conn.execute(
        "SELECT 1 FROM email_threads WHERE thread_id = ? LIMIT 1",
        (email["thread_id"],),
    ).fetchone()
    if existing:
        return

    conn.execute(
        """
        INSERT INTO email_threads (
            thread_id, subject, sender, sender_email, received_at, raw_body, conversation_id,
            recipient_timezone, internet_headers_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            email["thread_id"],
            email["subject"],
            email["sender"],
            email.get("sender_email") or None,
            email["received_at"] or None,
            email["raw_body"],
            email.get("conversation_id") or None,
            email.get("recipient_timezone") or None,
            email.get("internet_headers_json") or None,
        ),
    )


def _call_llm_triage(subject: str, body: str) -> TriageResult:
    client = get_hermes_client()
    user_content = json.dumps(
        {"subject": subject, "body": body},
        ensure_ascii=False,
    )
    response = client.chat.completions.create(
        role="triage",  # cheap, high-volume tier (Haiku by default)
        messages=[
            {"role": "system", "content": TRIAGE_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
    )
    content = response.choices[0].message.content or ""
    payload = _parse_json_object(content)
    return _coerce_triage_result(payload, source="llm")


def _infer_intent_from_text(subject: str, body: str) -> str:
    """Keyword heuristic when the LLM is unavailable."""
    from app.scheduling.meeting_type import infer_triage_intent_from_text

    return infer_triage_intent_from_text(subject, body)


def _fallback_triage(reason: str, *, subject: str = "", body: str = "") -> TriageResult:
    intent = _infer_intent_from_text(subject, body)
    if intent == "non_scheduling":
        return TriageResult(
            intent="non_scheduling",
            priority="low",
            confidence_score=0.0,
            justification=f"LLM triage unavailable; classified as non_scheduling ({reason}).",
            source="fallback",
        )
    return TriageResult(
        intent=intent,
        priority="medium",
        confidence_score=0.45 if intent != "unknown" else 0.0,
        justification=(
            f"LLM triage unavailable; keyword heuristic intent={intent} ({reason})."
            if intent != "unknown"
            else f"LLM triage unavailable; safe defaults applied ({reason})."
        ),
        source="fallback",
    )


def _correct_misclassified_scheduling_triage(
    triage: TriageResult,
    *,
    sender: str,
    subject: str,
    body: str,
) -> TriageResult:
    """Upgrade non_scheduling/unknown when the email clearly requests a meeting."""
    from app.scheduling.meeting_type import (
        effective_scheduling_intent,
        email_requests_scheduling,
    )

    if triage.intent not in {"non_scheduling", "unknown", "cancellation"}:
        return triage
    if not email_requests_scheduling(subject, body):
        return triage

    corrected = effective_scheduling_intent(triage.intent, subject=subject, body=body)
    if corrected in {"non_scheduling", "unknown"}:
        corrected = "referral_or_intro"

    return TriageResult(
        intent=corrected,
        priority="medium" if triage.priority == "low" else triage.priority,
        confidence_score=max(triage.confidence_score, 0.55),
        justification=(
            f"{triage.justification} "
            f"(corrected to {corrected}: email requests scheduling.)"
        ).strip(),
        source=triage.source,
    )


def _scheduling_priority_floor(
    priority: str,
    *,
    intent: str,
    sender: str,
) -> str:
    """Scheduling emails and internal senders should not surface as low priority."""
    if intent == "non_scheduling":
        return priority
    if priority != "low":
        return priority
    sender_lower = sender.lower()
    if any(domain in sender_lower for domain in PRIORITY_EMAIL_DOMAINS):
        return "medium"
    if intent not in {"non_scheduling", "unknown", "cancellation"}:
        return "medium"
    return priority


def _coerce_triage_result(payload: dict[str, Any], *, source: str) -> TriageResult:
    intent = str(payload.get("intent", "unknown")).strip().lower().replace(" ", "_")
    if intent not in VALID_INTENTS:
        intent = "unknown"

    priority = str(payload.get("priority", "medium")).strip().lower()
    if priority not in VALID_PRIORITIES:
        priority = "medium"

    try:
        confidence = float(payload.get("confidence_score", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))

    justification = str(payload.get("justification", "")).strip()
    if not justification:
        justification = "Classification produced with partial model output."

    return TriageResult(
        intent=intent,
        priority=priority,
        confidence_score=confidence,
        justification=justification,
        source=source,
    )


def _parse_json_object(content: str) -> dict[str, Any]:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, flags=re.DOTALL)
        if not match:
            raise ValueError("LLM response did not contain valid JSON") from None
        parsed = json.loads(match.group(0))

    if not isinstance(parsed, dict):
        raise ValueError("LLM JSON root must be an object")
    return parsed


def _apply_rule_checklist(
    *,
    sender: str,
    subject: str,
    body: str,
    triage: TriageResult,
) -> tuple[str, dict[str, Any]]:
    priority = triage.priority
    rules_applied: list[dict[str, str]] = []
    combined = f"{subject}\n{body}".lower()
    sender_lower = sender.lower()

    if is_priority_contact(sender):
        if priority != "high":
            rules_applied.append(
                {
                    "rule": "priority_contacts.yaml",
                    "match": "sender_email",
                    "effect": f"{priority} -> high",
                }
            )
        priority = "high"

    for domain in PRIORITY_EMAIL_DOMAINS:
        if domain in sender_lower:
            if priority != "high":
                rules_applied.append(
                    {
                        "rule": "priority_domain",
                        "match": domain,
                        "effect": f"{priority} -> high",
                    }
                )
            priority = "high"
            break

    for keyword in PRIORITY_KEYWORDS:
        if keyword in combined:
            bumped = _bump_priority(priority)
            if bumped != priority:
                rules_applied.append(
                    {
                        "rule": "investor_keyword",
                        "match": keyword,
                        "effect": f"{priority} -> {bumped}",
                    }
                )
            priority = bumped

    rules_snapshot = {
        "llm": triage.as_dict(),
        "rules_applied": rules_applied,
        "final_priority": priority,
        "priority_contacts_config": load_rules().get("priority_contacts", []),
    }
    return priority, rules_snapshot


def _bump_priority(current: str) -> str:
    order = ("low", "medium", "high")
    try:
        index = order.index(current)
    except ValueError:
        return "high"
    return order[min(index + 1, len(order) - 1)]


def _insert_proposal(
    conn: sqlite3.Connection,
    *,
    thread_id: str,
    triage: TriageResult,
    final_priority: str,
    rule_reasoning: dict[str, Any],
    recipient_timezone: str | None = None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO proposals (
            thread_id,
            status,
            intent_classification,
            priority_tier,
            rule_reasoning,
            confidence_score,
            justification,
            recipient_timezone
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            thread_id,
            AWAITING_REPLY_PROMPT,
            triage.intent,
            final_priority,
            json.dumps(rule_reasoning, default=str),
            triage.confidence_score,
            triage.justification,
            recipient_timezone,
        ),
    )
    return int(cursor.lastrowid)


def _asana_booking_reminder_exists(conn: sqlite3.Connection, thread_id: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM audit_log
        WHERE step_name = 'asana_booking_reminder' AND reference_id = ?
        LIMIT 1
        """,
        (thread_id,),
    ).fetchone()
    return row is not None


def _maybe_dispatch_asana_booking_reminder(
    conn: sqlite3.Connection,
    *,
    normalized: dict[str, str],
    proposal_id: int,
    intent: str,
) -> None:
    """Asana reservation reminders — only when Kory explicitly asks in Teams chat."""
    return


def _insert_audit_log(
    conn: sqlite3.Connection,
    *,
    step_name: str,
    reference_id: str,
    log_level: str,
    message: str,
    payload: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO audit_log (step_name, reference_id, log_level, message, payload)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            step_name,
            reference_id,
            log_level,
            message,
            json.dumps(payload, default=str),
        ),
    )
