"""Inbound email reply workflow: ask Kory before drafting, then draft → revise → send."""

from __future__ import annotations

import json
import re
import time
import traceback
from typing import Any

from app.agents.scheduler_agent import PENDING_TRIAGE, process_proposal_schedule
from app.agents.triage_agent import NON_SCHEDULING_INTENTS, VALID_INTENTS
from app.config import settings
from app.llm.hermes_client import get_hermes_client
from app.storage.lexi_db import get_lexi_connection
from app.storage.lexi_store import update_drafted_reply

AWAITING_REPLY_PROMPT = "awaiting_reply_prompt"
NO_REPLY_NEEDED = "no_reply_needed"
PENDING_APPROVAL = "pending_approval"
NEEDS_SCHEDULING_GUIDANCE = "needs_scheduling_guidance"

SCHEDULING_INTENTS = frozenset(VALID_INTENTS) - NON_SCHEDULING_INTENTS


def is_scheduling_intent(intent: str | None) -> bool:
    """True when begin_draft_reply should run the scheduler (slots + holds)."""
    return (intent or "unknown").strip().lower() in SCHEDULING_INTENTS


def should_run_scheduler_for_bundle(bundle: dict[str, Any]) -> bool:
    """Route to slot engine when intent or email content is clearly scheduling."""
    from app.scheduling.meeting_type import should_run_scheduler

    return should_run_scheduler(
        intent=str(bundle.get("intent_classification") or ""),
        subject=str(bundle.get("subject") or ""),
        body=str(bundle.get("raw_body") or ""),
    )


def _ensure_scheduling_intent_on_proposal(proposal_id: int, bundle: dict[str, Any]) -> str:
    """Persist corrected intent when email content overrides mis-triage."""
    from app.scheduling.meeting_type import effective_scheduling_intent

    current = str(bundle.get("intent_classification") or "unknown").lower()
    subject = str(bundle.get("subject") or "")
    body = str(bundle.get("raw_body") or "")
    corrected = effective_scheduling_intent(current, subject=subject, body=body)
    if corrected in {"non_scheduling", "unknown"} and should_run_scheduler_for_bundle(bundle):
        corrected = "referral_or_intro"
    priority = str(bundle.get("priority_tier") or "medium").lower()
    if should_run_scheduler_for_bundle(bundle) and priority == "low":
        priority = "medium"
    if corrected == current and priority == str(bundle.get("priority_tier") or "medium").lower():
        return current
    with get_lexi_connection() as conn:
        conn.execute(
            """
            UPDATE proposals
            SET intent_classification = ?, priority_tier = ?, updated_at = datetime('now')
            WHERE id = ?
            """,
            (corrected, priority, proposal_id),
        )
        conn.commit()
    return corrected


def _latest_scheduler_failure(proposal_id: int) -> str:
    with get_lexi_connection() as conn:
        row = conn.execute(
            """
            SELECT payload FROM audit_log
            WHERE reference_id = ? AND step_name = 'scheduler_engine' AND log_level = 'ERROR'
            ORDER BY id DESC LIMIT 1
            """,
            (str(proposal_id),),
        ).fetchone()
    if not row or not row["payload"]:
        return ""
    try:
        payload = json.loads(str(row["payload"]))
    except (TypeError, json.JSONDecodeError):
        return ""
    return str(payload.get("error") or "")


def humanize_scheduler_failure(error: str, *, intent: str = "") -> str:
    """One plain sentence for Kory when slot search fails."""
    text = (error or "").strip()
    if text.startswith("I couldn't find"):
        return text.split(" Engine diagnostics:")[0].strip()
    text_lower = text.lower()
    label = {
        "coffee": "a coffee slot",
        "referral_or_intro": "an intro slot",
        "pitch": "a meeting slot",
        "new_client": "a meeting slot",
        "happy_hour": "a happy hour slot",
        "dinner_request": "a dinner slot",
        "lunch_request": "a lunch slot",
        "podcast": "a recording slot",
    }.get((intent or "").strip().lower(), "a slot")

    if "calendar unavailable" in text_lower or "could not read" in text_lower:
        return "I can't read the calendar right now."
    if "this week" in text_lower:
        return f"I couldn't find {label} this week."
    if "next week" in text_lower or "insufficient_slots" in text_lower or "no valid meeting slots" in text_lower:
        return f"I couldn't find {label} next week. Should I try a different week?"
    return f"I couldn't find {label} in that window. Should I try a different week?"


def _set_needs_scheduling_guidance(
    proposal_id: int,
    *,
    reason: str,
    clear_draft: bool = True,
    suggested_guidance: str | None = None,
) -> None:
    with get_lexi_connection() as conn:
        if clear_draft:
            conn.execute(
                """
                UPDATE proposals
                SET status = ?, drafted_reply = NULL, proposed_slots = NULL,
                    teams_approval_notified_at = NULL,
                    scheduling_note = ?,
                    kory_scheduling_guidance = COALESCE(?, kory_scheduling_guidance),
                    updated_at = datetime('now')
                WHERE id = ?
                """,
                (NEEDS_SCHEDULING_GUIDANCE, reason, suggested_guidance, proposal_id),
            )
        else:
            conn.execute(
                """
                UPDATE proposals
                SET status = ?, teams_approval_notified_at = NULL,
                    scheduling_note = ?,
                    kory_scheduling_guidance = COALESCE(?, kory_scheduling_guidance),
                    updated_at = datetime('now')
                WHERE id = ?
                """,
                (NEEDS_SCHEDULING_GUIDANCE, reason, suggested_guidance, proposal_id),
            )
        _audit(
            conn,
            proposal_id,
            "scheduling_needs_guidance",
            reason or "Scheduler could not find valid slots after delegation.",
        )
        conn.commit()


def notify_kory_scheduling_blocked(proposal_id: int, *, reason: str = "") -> None:
    """Blocked scheduling — escalate to Kory in Teams."""
    from app.scheduling.kory_escalation import escalate_to_kory

    bundle = _fetch_proposal_bundle(proposal_id) or {}
    intent = str(bundle.get("intent_classification") or "")
    failure = _latest_scheduler_failure(proposal_id)
    failure_text = (reason or "").strip() or humanize_scheduler_failure(failure, intent=intent)
    escalate_to_kory(proposal_id, reason=failure_text, failure_error=failure_text)


def _extract_suggested_guidance(reason: str) -> str | None:
    import re

    match = re.search(r"\bI can (offer the [^—?.]+)", reason or "", re.I)
    if match:
        return match.group(1).strip()
    return None


def retry_scheduling_with_guidance(proposal_id: int, guidance: str) -> dict[str, Any]:
    """Apply Kory's Teams guidance and re-run the scheduler."""
    guidance = (guidance or "").strip()
    bundle = _fetch_proposal_bundle(proposal_id)
    if not bundle:
        return {"ok": False, "error": f"Proposal {proposal_id} not found."}
    if not guidance:
        stored = str(bundle.get("kory_scheduling_guidance") or "").strip()
        if stored:
            guidance = stored
        else:
            return {"ok": False, "error": "guidance cannot be empty."}
    # "needs_kory" is what the escalation path actually writes
    # (kory_escalation._mark_needs_kory) — omitting it here meant the retry
    # tool rejected every proposal the escalation flow produced, so Kory's
    # guidance could never be applied end-to-end (live I-2 defect).
    # "rejected" is allowed on purpose: "reject #N" discards a draft, and the
    # natural next ask is "redo it (differently)". Without it the retry tool
    # refused and Hermes hand-composed an unstaged draft in chat — no #id, no
    # approval path (live defect, coffee thread re-draft 2026-08-05).
    if bundle["status"] not in {
        NEEDS_SCHEDULING_GUIDANCE,
        AWAITING_REPLY_PROMPT,
        PENDING_APPROVAL,
        "needs_kory",
        "rejected",
    }:
        return {
            "ok": False,
            "error": (
                f"Proposal {proposal_id} is not awaiting scheduling guidance "
                f"(status={bundle['status']})."
            ),
        }

    with get_lexi_connection() as conn:
        conn.execute(
            """
            UPDATE proposals
            SET kory_scheduling_guidance = ?, status = ?, drafted_reply = NULL,
                proposed_slots = NULL, teams_approval_notified_at = NULL,
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (guidance, PENDING_TRIAGE, proposal_id),
        )
        _audit(conn, proposal_id, "scheduling_guidance_applied", guidance)
        conn.commit()

    scheduled = process_proposal_schedule(proposal_id)
    if scheduled:
        from app.bot.teams_publisher import schedule_teams_approval_push

        schedule_teams_approval_push(proposal_id, force=True)
        return {
            "ok": True,
            "proposal_id": proposal_id,
            "status": PENDING_APPROVAL,
            "message": "Found times — approval card is ready.",
            "kory_message": "I found times — review the card when you can.",
        }

    from app.scheduling.kory_escalation import escalate_to_kory

    failure = _latest_scheduler_failure(proposal_id)
    summary = humanize_scheduler_failure(failure, intent=str(bundle.get("intent_classification") or ""))
    return escalate_to_kory(proposal_id, reason=summary, failure_error=summary)

def _general_reply_system_prompt(
    *,
    recipient_email: str | None = None,
    voice_mode: str = "kory",
) -> str:
    from app.llm.kory_voice import voice_prompt_block
    from app.scheduling.lexi_voice import normalize_voice_mode, voice_instruction_for_mode
    from app.storage.kory_memory import facts_prompt_block

    mode = normalize_voice_mode(voice_mode)
    if mode == "lexi":
        voice = voice_instruction_for_mode(mode)
    else:
        voice = voice_prompt_block(recipient_email=recipient_email)
    memory = facts_prompt_block(limit=15)
    memory_block = f"\n\n{memory}" if memory else ""
    return f"""You are Lexi, Kory's assistant, drafting an email reply.

Given the inbound email and triage metadata, write a concise reply.

{voice}
{memory_block}

Return ONLY a valid JSON object with exactly one key:
- drafted_reply: string (the full plain-text email body to send)

Rules:
- Use proper paragraph spacing (blank line between paragraphs).
- Never greet the recipient with a sign-off word (Thanks, Thank you, Best, Regards, Cheers, Sincerely) — those are the sender's closing, not their name. If you cannot confidently determine the recipient's first name from the email, greet with "Hi there,".
- When the recipient timezone is unknown, say so clearly and list times in Mountain Time with ET/CT/PT equivalents in parentheses.
- Do not invent calendar times unless the email is clearly about scheduling.
- Do not include markdown fences or text outside the JSON object."""


def get_inbound_reply_queue() -> list[dict[str, Any]]:
    """Proposals waiting for Kory to say whether Lexi should draft a reply."""
    with get_lexi_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                p.id AS proposal_id,
                p.thread_id,
                p.intent_classification,
                p.priority_tier,
                p.justification,
                e.subject,
                e.sender,
                e.received_at,
                e.raw_body
            FROM proposals AS p
            INNER JOIN email_threads AS e ON e.thread_id = p.thread_id
            WHERE p.status = ?
            ORDER BY p.id ASC
            """,
            (AWAITING_REPLY_PROMPT,),
        ).fetchall()
    return [dict(row) for row in rows]


def decline_reply(proposal_id: int, *, reason: str = "") -> dict[str, Any]:
    """Kory declined to draft a reply for this inbound email."""
    with get_lexi_connection() as conn:
        row = conn.execute(
            "SELECT status FROM proposals WHERE id = ?",
            (proposal_id,),
        ).fetchone()
        if not row:
            return {"ok": False, "error": f"Proposal {proposal_id} not found."}
        if row["status"] != AWAITING_REPLY_PROMPT:
            return {
                "ok": False,
                "error": f"Proposal {proposal_id} is not awaiting reply prompt (status={row['status']}).",
            }
        conn.execute(
            "UPDATE proposals SET status = ?, updated_at = datetime('now') WHERE id = ?",
            (NO_REPLY_NEEDED, proposal_id),
        )
        _audit(
            conn,
            proposal_id,
            "inbound_reply_declined",
            reason or "Kory declined to draft a reply.",
        )
        conn.commit()
    return {"ok": True, "proposal_id": proposal_id, "status": NO_REPLY_NEEDED}


def set_proposal_delegation_metadata(
    proposal_id: int,
    *,
    voice_mode: str,
    send_channel: str,
    is_delegation: bool,
    reply_message_id: str | None = None,
) -> None:
    with get_lexi_connection() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(proposals)").fetchall()}
        if reply_message_id and "reply_message_id" in columns:
            conn.execute(
                """
                UPDATE proposals
                SET voice_mode = ?, send_channel = ?, is_delegation = ?,
                    reply_message_id = COALESCE(?, reply_message_id),
                    updated_at = datetime('now')
                WHERE id = ?
                """,
                (
                    voice_mode,
                    send_channel,
                    1 if is_delegation else 0,
                    reply_message_id.strip() or None,
                    proposal_id,
                ),
            )
        else:
            conn.execute(
                """
                UPDATE proposals
                SET voice_mode = ?, send_channel = ?, is_delegation = ?, updated_at = datetime('now')
                WHERE id = ?
                """,
                (
                    voice_mode,
                    send_channel,
                    1 if is_delegation else 0,
                    proposal_id,
                ),
            )
        conn.commit()


def begin_delegation_draft(proposal_id: int) -> dict[str, Any]:
    """Auto-draft after Kory CCs Lexi / delegates (skips awaiting_reply_prompt gate)."""
    import logging

    from app.scheduling.kory_escalation import escalate_to_kory
    from app.scheduling.hermes_task import run_delegation_scheduling_task

    logger = logging.getLogger(__name__)
    try:
        bundle = _fetch_proposal_bundle(proposal_id)
        if not bundle:
            return {"ok": False, "error": f"Proposal {proposal_id} not found."}
        status = bundle.get("status")
        if status not in {AWAITING_REPLY_PROMPT, PENDING_APPROVAL}:
            return {
                "ok": False,
                "error": f"Proposal {proposal_id} cannot auto-draft (status={status}).",
            }

        _ensure_scheduling_intent_on_proposal(proposal_id, bundle)
        bundle = _fetch_proposal_bundle(proposal_id) or bundle

        if should_run_scheduler_for_bundle(bundle):
            result = run_delegation_scheduling_task(proposal_id, bundle)
            if result.get("ok") and result.get("status") == PENDING_APPROVAL:
                return _attach_draft_verification(result, proposal_id)
            return result

        voice_mode = (bundle.get("voice_mode") or "lexi").lower()
        general = _draft_general_reply(bundle, voice_mode=voice_mode)
        if not general.get("ok"):
            return general
        _set_pending_approval(
            proposal_id,
            general["drafted_reply"],
            general.get("confidence_score", 0.5),
            voice_mode=voice_mode,
        )
        result = {
            "ok": True,
            "proposal_id": proposal_id,
            "status": PENDING_APPROVAL,
            "path": "delegation_general",
            "voice_mode": voice_mode,
            "drafted_reply": general["drafted_reply"],
            "message": "Lexi delegation draft ready for Teams approval.",
        }
        return _attach_draft_verification(result, proposal_id)
    except Exception as exc:
        logger.exception("Delegation draft failed for proposal %s", proposal_id)
        return escalate_to_kory(
            proposal_id,
            failure_error=f"{type(exc).__name__}: {exc}",
        )


def find_proposal_by_subject(subject_contains: str) -> dict[str, Any] | None:
    """Latest triaged proposal whose email subject matches (for Kory chat-initiated drafts)."""
    needle = (subject_contains or "").strip().lower()
    if not needle:
        return None
    with get_lexi_connection() as conn:
        row = conn.execute(
            """
            SELECT
                p.id AS proposal_id,
                p.status,
                p.intent_classification,
                e.subject,
                e.sender
            FROM proposals AS p
            INNER JOIN email_threads AS e ON e.thread_id = p.thread_id
            WHERE lower(e.subject) LIKE ?
            ORDER BY p.id DESC
            LIMIT 1
            """,
            (f"%{needle}%",),
        ).fetchone()
    return dict(row) if row else None


def _reactivate_proposal_for_chat_draft(proposal_id: int) -> None:
    with get_lexi_connection() as conn:
        conn.execute(
            """
            UPDATE proposals
            SET status = ?, updated_at = datetime('now')
            WHERE id = ?
            """,
            (AWAITING_REPLY_PROMPT, proposal_id),
        )
        conn.commit()


_CHAT_DRAFTABLE_STATUSES = frozenset(
    {AWAITING_REPLY_PROMPT, NO_REPLY_NEEDED, NEEDS_SCHEDULING_GUIDANCE}
)


def begin_draft_reply(proposal_id: int, *, voice_mode: str = "") -> dict[str, Any]:
    """After Kory asks in chat (or says yes on prompt): draft with slots when scheduling."""
    bundle = _fetch_proposal_bundle(proposal_id)
    if not bundle:
        return {"ok": False, "error": f"Proposal {proposal_id} not found."}
    status = str(bundle.get("status") or "")
    if status not in _CHAT_DRAFTABLE_STATUSES:
        return {
            "ok": False,
            "error": (
                f"Proposal {proposal_id} is not ready for a new draft (status={status})."
            ),
        }
    if status in {NO_REPLY_NEEDED, NEEDS_SCHEDULING_GUIDANCE}:
        _reactivate_proposal_for_chat_draft(proposal_id)
        bundle = _fetch_proposal_bundle(proposal_id) or bundle

    if not voice_mode.strip():
        # Default to Lexi handling scheduling in her own voice (from lexi@); Kory
        # voice is opt-in only when Kory explicitly asks. Never silently fall back
        # to Kory's (blocked) mailbox.
        voice_mode = str(bundle.get("voice_mode") or "lexi")

    if voice_mode.strip():
        set_proposal_delegation_metadata(
            proposal_id,
            voice_mode=voice_mode.strip().lower(),
            send_channel="lexi" if voice_mode.strip().lower() == "lexi" else "kory",
            is_delegation=False,
        )
        bundle = _fetch_proposal_bundle(proposal_id) or bundle

    intent = _ensure_scheduling_intent_on_proposal(proposal_id, bundle)
    bundle = _fetch_proposal_bundle(proposal_id) or bundle
    result: dict[str, Any] | None = None
    if should_run_scheduler_for_bundle(bundle):
        scheduled = process_proposal_schedule(proposal_id)
        if scheduled:
            result = {
                "ok": True,
                "proposal_id": proposal_id,
                "status": PENDING_APPROVAL,
                "path": "scheduling",
                "message": "Draft ready on the approval card.",
                "kory_message": "Draft is ready — review the times on the card.",
            }
        else:
            notify_kory_scheduling_blocked(proposal_id)
            failure = _latest_scheduler_failure(proposal_id)
            summary = humanize_scheduler_failure(failure, intent=intent)
            bundle = _fetch_proposal_bundle(proposal_id) or {}
            kory_msg = str(bundle.get("scheduling_note") or summary).strip()
            return {
                "ok": False,
                "proposal_id": proposal_id,
                "status": NEEDS_SCHEDULING_GUIDANCE,
                "error": kory_msg,
                "kory_message": kory_msg,
            }
    else:
        general = _draft_general_reply(
            bundle,
            voice_mode=str(bundle.get("voice_mode") or voice_mode or "lexi"),
        )
        if not general.get("ok"):
            return general
        _set_pending_approval(
            proposal_id,
            general["drafted_reply"],
            general.get("confidence_score", 0.5),
            voice_mode=str(bundle.get("voice_mode") or voice_mode or "lexi"),
        )
        result = {
            "ok": True,
            "proposal_id": proposal_id,
            "status": PENDING_APPROVAL,
            "path": "general",
            "drafted_reply": general["drafted_reply"],
            "message": "Draft ready on the approval card.",
            "kory_message": "Draft is ready — review on the card.",
        }

    if result and result.get("ok") and result.get("status") == PENDING_APPROVAL:
        from app.bot.teams_publisher import schedule_teams_approval_push

        schedule_teams_approval_push(proposal_id, force=True)
    if result:
        result = _attach_draft_verification(result, proposal_id)
    return result or {"ok": False, "error": "Draft failed."}


def draft_reply_for_subject(
    subject_contains: str,
    *,
    voice_mode: str = "kory",
) -> dict[str, Any]:
    """Hermes path: find triaged email by subject fragment and draft (Kory chat, no CC Lexi)."""
    match = find_proposal_by_subject(subject_contains)
    if not match:
        return {
            "ok": False,
            "error": f"No proposal found for subject containing '{subject_contains}'.",
        }
    result = begin_draft_reply(
        int(match["proposal_id"]),
        voice_mode=voice_mode or "kory",
    )
    if result.get("ok"):
        result["subject"] = match.get("subject")
        result["sender"] = match.get("sender")
    return result


def begin_reoffer_schedule(proposal_id: int) -> dict[str, Any]:
    """After recipient declined times: find new slots and stage a fresh approval card."""
    from app.agents.comms_agent import STATUS_PENDING_REOFFER

    with get_lexi_connection() as conn:
        row = conn.execute(
            "SELECT status FROM proposals WHERE id = ?",
            (proposal_id,),
        ).fetchone()
        if not row:
            return {"ok": False, "error": f"Proposal {proposal_id} not found."}
        if row["status"] != STATUS_PENDING_REOFFER:
            return {
                "ok": False,
                "error": f"Proposal {proposal_id} is not awaiting re-offer (status={row['status']}).",
            }

    scheduled = process_proposal_schedule(proposal_id)
    if not scheduled:
        return {
            "ok": False,
            "error": "Could not find new valid slots — check calendar or rules.",
            "proposal_id": proposal_id,
        }
    from app.bot.teams_publisher import schedule_teams_approval_push

    schedule_teams_approval_push(proposal_id, force=True)
    return {
        "ok": True,
        "proposal_id": proposal_id,
        "status": PENDING_APPROVAL,
        "message": "New times drafted — review and send offer when ready.",
    }


def update_proposal_draft(proposal_id: int, drafted_reply: str) -> dict[str, Any]:
    """Apply Kory's edits to a draft before send."""
    body = (drafted_reply or "").strip()
    if not body:
        return {"ok": False, "error": "drafted_reply cannot be empty."}
    with get_lexi_connection() as conn:
        row = conn.execute(
            "SELECT status FROM proposals WHERE id = ?",
            (proposal_id,),
        ).fetchone()
        if not row:
            return {"ok": False, "error": f"Proposal {proposal_id} not found."}
        if row["status"] != PENDING_APPROVAL:
            return {
                "ok": False,
                "error": f"Proposal {proposal_id} is not pending approval (status={row['status']}).",
            }
    update_drafted_reply(proposal_id, body)
    return {"ok": True, "proposal_id": proposal_id, "drafted_reply": body}


def _set_pending_approval(
    proposal_id: int,
    drafted_reply: str,
    confidence_score: float,
    *,
    voice_mode: str = "kory",
) -> None:
    from app.scheduling.email_format import normalize_draft_for_display

    body = normalize_draft_for_display(drafted_reply, max_chars=None, voice_mode=voice_mode)
    with get_lexi_connection() as conn:
        conn.execute(
            """
            UPDATE proposals
            SET status = ?,
                drafted_reply = ?,
                confidence_score = ?,
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (PENDING_APPROVAL, body, confidence_score, proposal_id),
        )
        _audit(conn, proposal_id, "inbound_reply_drafted", "General reply draft staged.")
        conn.commit()


def _draft_general_reply(bundle: dict[str, Any], *, voice_mode: str = "kory") -> dict[str, Any]:
    started = time.perf_counter()
    subject = bundle.get("subject") or ""
    sender = bundle.get("sender") or ""
    body = bundle.get("raw_body") or ""
    intent = bundle.get("intent_classification") or "unknown"
    priority = bundle.get("priority_tier") or "medium"

    user_content = json.dumps(
        {
            "subject": subject,
            "sender": sender,
            "body": body,
            "intent": intent,
            "priority": priority,
            "justification": bundle.get("justification") or "",
        },
        ensure_ascii=False,
    )

    try:
        client = get_hermes_client()
        response = client.chat.completions.create(
            model=settings.llm_model,
            messages=[
                {
                    "role": "system",
                    "content": _general_reply_system_prompt(
                        recipient_email=sender,
                        voice_mode=voice_mode or str(bundle.get("voice_mode") or "kory"),
                    ),
                },
                {"role": "user", "content": user_content},
            ],
            temperature=0.3,
        )
        content = response.choices[0].message.content or ""
        payload = _parse_json_object(content)
        drafted = _finalize_draft(
            str(payload.get("drafted_reply", "")),
            voice_mode=voice_mode or str(bundle.get("voice_mode") or "kory"),
        )
        if not drafted:
            raise ValueError("LLM returned empty drafted_reply")
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        return {
            "ok": True,
            "drafted_reply": drafted,
            "confidence_score": 0.75,
            "duration_ms": duration_ms,
            "source": "llm",
        }
    except Exception as exc:
        fallback = _template_general_reply(
            subject,
            sender,
            body,
            voice_mode=voice_mode or str(bundle.get("voice_mode") or "kory"),
        )
        return {
            "ok": True,
            "drafted_reply": fallback,
            "confidence_score": 0.4,
            "source": "template_fallback",
            "warning": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }


def _finalize_draft(draft: str, *, voice_mode: str = "kory") -> str:
    from app.scheduling.email_format import finalize_lexi_email_body, finalize_outbound_email_body
    from app.scheduling.lexi_voice import normalize_voice_mode
    from app.safety.operation_verify import verify_draft_reply

    mode = normalize_voice_mode(voice_mode)
    if mode == "lexi":
        normalized = finalize_lexi_email_body(draft.strip())
    else:
        normalized = finalize_outbound_email_body(draft.strip())
    check = verify_draft_reply(normalized, voice_mode=mode)
    if not check.ok:
        raise ValueError("; ".join(check.errors) or "Draft failed verification.")
    return normalized


def _attach_draft_verification(result: dict[str, Any], proposal_id: int) -> dict[str, Any]:
    from app.safety.operation_verify import merge_verify, verify_draft_reply

    bundle = _fetch_proposal_bundle(proposal_id)
    draft = result.get("drafted_reply") or (bundle or {}).get("drafted_reply") or ""
    if not draft:
        return result
    voice_mode = str((bundle or {}).get("voice_mode") or "kory")
    verify = verify_draft_reply(str(draft), voice_mode=voice_mode)
    return merge_verify(result, verify)


def _template_general_reply(
    subject: str,
    sender: str,
    body: str,
    *,
    voice_mode: str = "kory",
) -> str:
    from app.scheduling.lexi_voice import normalize_voice_mode

    excerpt = (body or "").strip()
    if len(excerpt) > 200:
        excerpt = excerpt[:200] + "…"
    mode = normalize_voice_mode(voice_mode)
    if mode == "lexi":
        from app.scheduling.email_format import finalize_lexi_email_body

        return finalize_lexi_email_body(
            f"Hi — I'm Lexi, Kory's assistant.\n\n"
            f"Thanks for your note regarding \"{subject or 'your message'}\". "
            "I'll follow up shortly on next steps."
        )
    return (
        f"Thanks for your note regarding \"{subject or 'your message'}\".\n\n"
        f"I received your message and will follow up shortly.\n\n"
        f"Let's Win,\n"
        f"Kory"
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


def _fetch_proposal_bundle(proposal_id: int) -> dict[str, Any] | None:
    with get_lexi_connection() as conn:
        row = conn.execute(
            """
            SELECT
                p.id AS proposal_id,
                p.thread_id,
                p.status,
                p.intent_classification,
                p.priority_tier,
                p.justification,
                p.voice_mode,
                p.send_channel,
                p.is_delegation,
                p.drafted_reply,
                p.scheduling_note,
                p.kory_scheduling_guidance,
                e.subject,
                e.sender,
                e.raw_body
            FROM proposals AS p
            INNER JOIN email_threads AS e ON e.thread_id = p.thread_id
            WHERE p.id = ?
            """,
            (proposal_id,),
        ).fetchone()
    return dict(row) if row else None


def _audit(
    conn,
    proposal_id: int,
    step_name: str,
    message: str,
) -> None:
    conn.execute(
        """
        INSERT INTO audit_log (step_name, reference_id, log_level, message, payload)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            step_name,
            str(proposal_id),
            "INFO",
            message,
            json.dumps({"proposal_id": proposal_id}, default=str),
        ),
    )
