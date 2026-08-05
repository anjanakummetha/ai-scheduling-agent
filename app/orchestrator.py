"""Lexi Phase 5: production ingress orchestration and auto-execute policy engine."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import signal
import sqlite3
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.agents.comms_agent import execute_lexi_approval
from app.agents.scheduler_agent import process_pending_schedules
from app.agents.inbound_filter import evaluate_inbound_notification, normalize_subject_key
from app.agents.delegation import detect_delegation
from app.agents.inbound_reply import (
    AWAITING_REPLY_PROMPT,
    NEEDS_SCHEDULING_GUIDANCE,
    NO_REPLY_NEEDED,
    begin_delegation_draft,
    set_proposal_delegation_metadata,
)
from app.bot.teams_publisher import (
    schedule_teams_approval_push,
    schedule_teams_reply_prompt_push,
)
from app.agents.triage_agent import process_new_email
from app.config import settings
from app.integrations.composio_client import ComposioNotConfiguredError, execute_tool
from app.integrations.outlook_calendar import has_conflict
from app.integrations.outlook_email import (
    build_inbound_raw_email,
    extract_recipient_list,
    get_message,
    merge_list_message_fields,
    normalize_message,
)
from app.storage.lexi_db import get_lexi_connection

logger = logging.getLogger(__name__)

PENDING_TRIAGE = "pending_triage"
PENDING_APPROVAL = "pending_approval"
STATUS_EXECUTED = "executed"

AUTO_EXECUTE_MIN_CONFIDENCE = float(os.getenv("LEXI_AUTO_EXECUTE_CONFIDENCE", "0.95"))
AUTO_EXECUTE_ENABLED = os.getenv("LEXI_AUTO_EXECUTE_ENABLED", "false").lower() in {
    "1",
    "true",
    "yes",
}
def _outlook_poll_enabled() -> bool:
    """Read at runtime so app.worker can enable poll before the daemon thread starts."""
    return os.getenv("LEXI_ORCHESTRATOR_POLL_OUTLOOK", "false").lower() in {
        "1",
        "true",
        "yes",
    }


def _backup_poll_minutes() -> int:
    try:
        return max(0, int(os.getenv("LEXI_ORCHESTRATOR_BACKUP_POLL_MINUTES", "0")))
    except ValueError:
        return 0


def _should_poll_outlook_this_cycle(cycle_number: int, interval_seconds: int) -> bool:
    """Frequent poll (legacy) or slow backup poll when webhook is primary ingress."""
    if _outlook_poll_enabled():
        return cycle_number % 2 == 1
    backup_min = _backup_poll_minutes()
    if backup_min <= 0:
        return False
    cycles_per_backup = max(1, round((backup_min * 60) / max(interval_seconds, 1)))
    return cycle_number % cycles_per_backup == 1


def describe_ingress_mode(*, interval_seconds: int = 30) -> dict[str, Any]:
    """Summarize email ingress for status endpoints and deploy checks."""
    poll = _outlook_poll_enabled()
    webhook = os.getenv("LEXI_WEBHOOK_ENABLED", "false").lower() in {"1", "true", "yes"} or bool(
        os.getenv("LEXI_WEBHOOK_PORT", "").strip() not in {"", "0"}
    )
    backup_min = _backup_poll_minutes()
    if poll:
        mode = "poll_primary"
        detail = f"inbox list ~every {interval_seconds * 2}s"
    elif webhook and backup_min > 0:
        mode = "webhook_primary_backup_poll"
        detail = f"webhook + backup inbox list every {backup_min}m"
    elif webhook:
        mode = "webhook_only"
        detail = "webhook only (no idle inbox polling)"
    else:
        mode = "manual_only"
        detail = "no poll or webhook — ingress disabled until configured"
    public_url = os.getenv("LEXI_WEBHOOK_PUBLIC_URL", "").strip()
    return {
        "mode": mode,
        "detail": detail,
        "webhook_enabled": webhook,
        "poll_outlook": poll,
        "backup_poll_minutes": backup_min,
        "webhook_public_url": public_url or None,
        "webhook_path": "/webhooks/composio",
    }
TRUSTED_INTERNAL_DOMAINS = (
    "@iconicfounders.com",
    "@ifg.vc",
    "@iconicfounders.co",
)

_ORCHESTRATOR_LOCK = threading.Lock()
_SHUTDOWN_REQUESTED = threading.Event()
_INBOUND_QUEUE: queue.Queue[dict[str, Any]] = queue.Queue()


@dataclass
class InboundResult:
    proposal_id: int
    thread_id: str
    triage_status: str
    scheduler_processed: bool
    final_status: str
    auto_executed: bool
    auto_execute_reason: str | None = None
    execution: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "thread_id": self.thread_id,
            "triage_status": self.triage_status,
            "scheduler_processed": self.scheduler_processed,
            "final_status": self.final_status,
            "auto_executed": self.auto_executed,
            "auto_execute_reason": self.auto_execute_reason,
            "execution": self.execution,
        }


def enqueue_inbound(raw_email: dict[str, Any]) -> None:
    """Queue a webhook/poll payload for the daemon loop (thread-safe)."""
    _INBOUND_QUEUE.put(dict(raw_email))


def handle_inbound_stream(raw_email: dict[str, Any]) -> dict[str, Any]:
    """Run triage → scheduler → optional auto-execute for one inbound email."""
    with _ORCHESTRATOR_LOCK:
        return _handle_inbound_stream_locked(raw_email)


async def handle_inbound_stream_async(raw_email: dict[str, Any]) -> dict[str, Any]:
    """Async wrapper for ingress handlers (runs blocking pipeline in a worker thread)."""
    return await asyncio.to_thread(handle_inbound_stream, raw_email)


async def run_orchestration_daemon_async(interval_seconds: int = 30) -> None:
    """Async entrypoint that runs the blocking daemon loop in a worker thread."""
    await asyncio.to_thread(run_orchestration_daemon, interval_seconds)


def request_orchestrator_shutdown() -> None:
    """Signal the orchestrator loop to exit gracefully."""
    _SHUTDOWN_REQUESTED.set()


def run_orchestration_daemon(interval_seconds: int = 30) -> None:
    """Background worker: drain ingress queue, recover state, optionally poll Outlook."""
    _register_signal_handlers()
    logger.info(
        "Lexi orchestrator starting (interval=%ss, auto_execute=%s, ingress=%s)",
        interval_seconds,
        AUTO_EXECUTE_ENABLED,
        describe_ingress_mode(interval_seconds=interval_seconds),
    )
    print(
        f"[lexi-orchestrator] started | interval={interval_seconds}s | "
        f"auto_execute={AUTO_EXECUTE_ENABLED} | "
        f"ingress={describe_ingress_mode(interval_seconds=interval_seconds)['mode']}",
        file=sys.stderr,
    )

    cycle = 0
    while not _SHUTDOWN_REQUESTED.is_set():
        cycle += 1
        cycle_started = time.perf_counter()
        try:
            processed = _run_daemon_cycle(cycle, interval_seconds)
            elapsed = round(time.perf_counter() - cycle_started, 2)
            print(
                f"[lexi-orchestrator] cycle={cycle} processed={processed} "
                f"elapsed={elapsed}s queue_depth={_INBOUND_QUEUE.qsize()}",
                file=sys.stderr,
            )
        except Exception as exc:
            _log_orchestrator_error(
                step_name="orchestrator_cycle",
                reference_id=f"cycle-{cycle}",
                message="Daemon cycle failed; continuing.",
                exc=exc,
            )
            print(f"[lexi-orchestrator] cycle={cycle} ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)

        if _SHUTDOWN_REQUESTED.wait(timeout=interval_seconds):
            break

    print("[lexi-orchestrator] shutdown complete.", file=sys.stderr)


def _handle_inbound_stream_locked(raw_email: dict[str, Any]) -> dict[str, Any]:
    thread_id = str(
        raw_email.get("thread_id")
        or raw_email.get("outlook_message_id")
        or ""
    ).strip()
    if not thread_id:
        raise ValueError("raw_email must include thread_id or outlook_message_id")

    sender = str(raw_email.get("sender") or raw_email.get("sender_email") or "").strip()
    subject = str(raw_email.get("subject") or "").strip()
    body_preview = str(raw_email.get("raw_body") or raw_email.get("body") or "")

    if _thread_already_ingested(thread_id):
        delegation_replay = detect_delegation(
            subject=subject,
            body=body_preview,
            sender=sender,
            raw_email=raw_email,
        )
        if delegation_replay.is_delegation:
            followup = _handle_delegation_followup(raw_email, delegation_replay)
            if followup:
                return followup
        message = f"Thread {thread_id} already ingested; skipping duplicate ingress."
        logger.info(message)
        return {
            "skipped": True,
            "thread_id": thread_id,
            "reason": message,
            "action": "already_ingested",
        }

    if _thread_has_active_proposal(thread_id):
        message = f"Active proposal already exists for thread {thread_id}; skipping duplicate ingress."
        logger.info(message)
        return {
            "skipped": True,
            "thread_id": thread_id,
            "reason": message,
        }

    if _skip_inbound_for_local_test_mode(subject=subject):
        message = "Local Mac testing: only subjects containing TEST are processed."
        logger.info("%s subject=%s", message, subject)
        return {
            "skipped": True,
            "thread_id": thread_id,
            "reason": message,
            "action": "local_test_only",
        }

    from app.agents.lexi_mail_intent import handle_lexi_direct_mail, is_mail_to_lexi

    if is_mail_to_lexi(raw_email):
        direct = handle_lexi_direct_mail(raw_email)
        if direct.get("handled"):
            _record_polled_message_id(thread_id, raw_email)
            return direct

    from app.agents.lexi_thread_followup import try_handle_lexi_thread_followup

    followup = try_handle_lexi_thread_followup(raw_email)
    if followup and followup.get("action"):
        _record_polled_message_id(thread_id, raw_email)
        return followup

    delegation_early = detect_delegation(
        subject=subject,
        body=body_preview,
        sender=sender,
        raw_email=raw_email,
    )
    if delegation_early.is_delegation:
        followup = _handle_delegation_followup(raw_email, delegation_early)
        if followup:
            return followup

    conversation_id = str(raw_email.get("conversation_id") or "").strip()
    if conversation_id and _conversation_has_proposal(conversation_id):
        _record_polled_message_id(thread_id, raw_email)
        logger.info(
            "Conversation %s already tracked — skipping duplicate triage for message %s.",
            conversation_id,
            thread_id,
        )
        return {
            "skipped": True,
            "thread_id": thread_id,
            "conversation_id": conversation_id,
            "reason": "Conversation already has a Lexi proposal.",
            "action": "conversation_already_tracked",
        }

    if _duplicate_newsletter_burst(sender, subject, body_preview):
        message = f"Duplicate newsletter/digest from {sender}; skipping."
        logger.info("%s subject=%s", message, subject)
        return {
            "skipped": True,
            "thread_id": thread_id,
            "reason": message,
            "action": "duplicate_newsletter",
        }

    proposal_id = process_new_email(raw_email)
    if proposal_id is None:
        message = "Email classified as non_scheduling; no proposal staged."
        logger.info("%s thread_id=%s", message, thread_id)
        return {
            "skipped": True,
            "thread_id": thread_id,
            "reason": message,
            "action": "no_action",
        }

    bundle = _fetch_proposal_bundle(proposal_id) or {}
    try:
        from app.scheduling.introducer import resolve_introducer_for_contact
        from app.storage.recipient_profiles import normalize_sender_email

        guest = normalize_sender_email(str(bundle.get("sender") or sender))
        if guest:
            resolve_introducer_for_contact(
                email=guest,
                subject=subject,
                body=body_preview,
                sender=sender,
                to_recipients=raw_email.get("to_recipients"),
                cc_recipients=raw_email.get("cc_recipients"),
            )
    except Exception:
        pass

    triage_status = _fetch_proposal_status(proposal_id) or AWAITING_REPLY_PROMPT
    delegation = detect_delegation(
        subject=str(bundle.get("subject") or subject),
        body=str(bundle.get("raw_body") or raw_email.get("raw_body") or ""),
        sender=str(bundle.get("sender") or sender),
        raw_email=raw_email,
    )
    notification = evaluate_inbound_notification(
        intent=str(bundle.get("intent_classification") or ""),
        priority=str(bundle.get("priority_tier") or ""),
        sender=str(bundle.get("sender") or sender),
        subject=str(bundle.get("subject") or subject),
        body=str(bundle.get("raw_body") or raw_email.get("raw_body") or ""),
        is_delegation=delegation.is_delegation,
    )
    if triage_status == AWAITING_REPLY_PROMPT and notification.auto_skip:
        _mark_proposal_no_reply(proposal_id, reason=notification.reason)
        triage_status = NO_REPLY_NEEDED
        logger.info(
            "Auto-skipped proposal %s (%s) — not important enough for Teams.",
            proposal_id,
            notification.reason,
        )

    scheduler_processed = False
    final_status = triage_status
    auto_executed = False
    auto_reason: str | None = None
    execution_payload: dict[str, Any] | None = None

    if (
        delegation.is_delegation
        and settings.lexi_delegation_auto_draft
        and final_status == AWAITING_REPLY_PROMPT
    ):
        set_proposal_delegation_metadata(
            proposal_id,
            voice_mode="lexi",
            send_channel="lexi",
            is_delegation=True,
            reply_message_id=str(
                raw_email.get("message_id") or raw_email.get("thread_id") or ""
            ),
        )
        # If Kory delegated (he's the sender), Lexi schedules with and replies to the
        # counterpart (a To recipient who isn't Kory/Lexi) — not Kory. Point the thread
        # sender at the counterpart so the draft greets and addresses them correctly.
        from app.agents.delegation import (
            _kory_addresses as _kory_addrs,
            delegation_counterpart_contact,
        )

        current_sender = str(bundle.get("sender_email") or bundle.get("sender") or "").lower()
        if current_sender in _kory_addrs():
            cp_email, cp_name = delegation_counterpart_contact(raw_email)
            if cp_email and cp_email != current_sender:
                with get_lexi_connection() as _conn:
                    _conn.execute(
                        "UPDATE email_threads SET sender = ?, sender_email = ? WHERE thread_id = ?",
                        (cp_name or cp_email, cp_email, thread_id),
                    )
                    _conn.commit()
        draft_result = begin_delegation_draft(proposal_id)
        scheduler_processed = draft_result.get("status") == PENDING_APPROVAL
        final_status = str(draft_result.get("status") or final_status)
        logger.info(
            "Delegation auto-draft proposal %s (%s) → %s",
            proposal_id,
            delegation.reason,
            final_status,
        )
        if (
            final_status == PENDING_APPROVAL
            and settings.lexi_teams_enabled
            and draft_result.get("ok")
        ):
            schedule_teams_approval_push(proposal_id)
        elif (
            final_status in {NEEDS_SCHEDULING_GUIDANCE, "needs_kory"}
            and settings.lexi_teams_enabled
        ):
            logger.info(
                "Scheduling blocked for proposal %s (status=%s).",
                proposal_id,
                final_status,
            )
    elif final_status == AWAITING_REPLY_PROMPT and settings.lexi_teams_enabled and notification.notify:
        schedule_teams_reply_prompt_push(proposal_id)
    elif final_status == PENDING_APPROVAL:
        should_auto, auto_reason = evaluate_auto_execute_policy(proposal_id)
        if should_auto:
            execution = _try_auto_execute(proposal_id)
            auto_executed = execution is not None and execution.ok
            execution_payload = execution.to_dict() if execution else None
            final_status = _fetch_proposal_status(proposal_id) or final_status
        if final_status == PENDING_APPROVAL and settings.lexi_teams_enabled:
            schedule_teams_approval_push(proposal_id)

    result = InboundResult(
        proposal_id=proposal_id,
        thread_id=thread_id,
        triage_status=triage_status,
        scheduler_processed=scheduler_processed,
        final_status=final_status,
        auto_executed=auto_executed,
        auto_execute_reason=auto_reason,
        execution=execution_payload,
    )
    return result.to_dict()


def evaluate_auto_execute_policy(proposal_id: int) -> tuple[bool, str]:
    """Return whether Lexi may bypass Teams approval for this proposal."""
    from app.safety.approval_gate import auto_execute_allowed, kory_approves_all

    if kory_approves_all():
        return False, "kory_approves_all_phase1"
    if not AUTO_EXECUTE_ENABLED or not auto_execute_allowed():
        return False, "auto_execute_disabled"

    bundle = _fetch_proposal_bundle(proposal_id)
    if not bundle:
        return False, "proposal_not_found"
    if bundle["status"] != PENDING_APPROVAL:
        return False, f"status_not_pending_approval ({bundle['status']})"

    priority = (bundle.get("priority_tier") or "medium").lower()
    intent = (bundle.get("intent_classification") or "unknown").lower()
    confidence = float(bundle.get("confidence_score") or 0.0)
    sender = (bundle.get("sender") or "").lower()
    slots = _parse_json_list(bundle.get("proposed_slots"))

    if priority == "low":
        return True, "priority_tier_low"

    if intent == "internal_sync" and _sender_is_trusted_domain(sender):
        return True, "internal_sync_trusted_domain"

    if confidence > AUTO_EXECUTE_MIN_CONFIDENCE:
        if not slots:
            return False, "high_confidence_but_no_slots"
        if _proposal_slots_have_zero_conflicts(slots):
            return True, f"confidence>{AUTO_EXECUTE_MIN_CONFIDENCE}_no_calendar_conflicts"
        return False, "high_confidence_but_calendar_conflicts_present"

    return False, "manual_approval_required"


def _try_auto_execute(proposal_id: int) -> Any:
    bundle = _fetch_proposal_bundle(proposal_id)
    if not bundle:
        return None

    slots = _parse_json_list(bundle.get("proposed_slots"))
    if not slots:
        _log_orchestrator_error(
            step_name="auto_execution_dispatch",
            reference_id=str(proposal_id),
            message="Auto-execute skipped: no proposed slots available.",
            exc=ValueError("missing slots"),
        )
        return None

    selected_slot = str(slots[0].get("start", ""))
    if not selected_slot:
        return None

    try:
        result = execute_lexi_approval(
            proposal_id=proposal_id,
            decision="approved",
            selected_slot=selected_slot,
            authorized_by="lexi_auto_policy",
            decision_source="auto_execute",
        )
        _insert_audit_log(
            step_name="auto_execution_dispatch",
            reference_id=str(proposal_id),
            log_level="INFO" if result.ok else "ERROR",
            message=(
                f"Auto-execute dispatch completed for proposal {proposal_id} "
                f"(ok={result.ok})."
            ),
            payload={
                "proposal_id": proposal_id,
                "selected_slot": selected_slot,
                "authorized_by": "lexi_auto_policy",
                "result": result.to_dict(),
            },
        )
        return result
    except Exception as exc:
        _log_orchestrator_error(
            step_name="auto_execution_dispatch",
            reference_id=str(proposal_id),
            message="Auto-execute failed; proposal remains pending_approval.",
            exc=exc,
            extra={"selected_slot": selected_slot},
        )
        return None


def _run_daemon_cycle(cycle_number: int, interval_seconds: int = 30) -> int:
    processed = 0

    try:
        from app.storage.heartbeat import touch_heartbeat

        touch_heartbeat()
    except Exception:
        pass

    while not _INBOUND_QUEUE.empty() and not _SHUTDOWN_REQUESTED.is_set():
        try:
            raw_email = _INBOUND_QUEUE.get_nowait()
        except queue.Empty:
            break
        try:
            handle_inbound_stream(raw_email)
            processed += 1
        except Exception as exc:
            thread_id = str(raw_email.get("thread_id") or "unknown")
            _log_orchestrator_error(
                step_name="inbound_stream",
                reference_id=thread_id,
                message="Inbound stream processing failed; continuing daemon.",
                exc=exc,
                extra={"cycle": cycle_number, "raw_email": raw_email},
            )

    processed += _recover_pending_triage()
    _run_hold_lifecycle()
    _run_kory_briefings()
    _run_protection_audit_weekly()
    if cycle_number % _db_maintenance_interval() == 0:
        _run_db_maintenance()

    if _should_poll_outlook_this_cycle(cycle_number, interval_seconds):
        processed += _poll_outlook_ingress()

    return processed


_last_protection_audit_week: tuple[int, int] | None = None


def _run_protection_audit_weekly() -> None:
    """Sunday evening (MT), at most once per ISO week, surface protection drift to Kory."""
    global _last_protection_audit_week
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from app.config import settings

        now_mt = datetime.now(ZoneInfo(settings.scheduling_timezone))
        if now_mt.weekday() != 6 or now_mt.hour < 18:  # Sunday, evening
            return
        iso_week = now_mt.isocalendar()[:2]
        if _last_protection_audit_week == iso_week:
            return
        _last_protection_audit_week = iso_week

        from app.jobs.protection_audit import run_protection_audit

        result = run_protection_audit(push_to_kory=True)
        if result.get("expected_missing"):
            logger.info("Protection audit: %s", result)
    except Exception as exc:  # never let the audit break the daemon
        _log_orchestrator_error(
            step_name="protection_audit",
            reference_id="protection_audit",
            message="Weekly protection audit failed.",
            exc=exc,
        )


def _run_hold_lifecycle() -> None:
    try:
        from app.jobs.hold_lifecycle import run_hold_lifecycle_cycle

        result = run_hold_lifecycle_cycle()
        if result.get("released_expired") or result.get("friday_cleanup"):
            logger.info("Hold lifecycle: %s", result)
    except Exception as exc:
        _log_orchestrator_error(
            step_name="hold_lifecycle",
            reference_id="daemon",
            message="Hold lifecycle cycle failed.",
            exc=exc,
        )


def _run_kory_briefings() -> None:
    try:
        from app.jobs.kory_briefings import run_kory_briefing_cycle

        result = run_kory_briefing_cycle()
        if result.get("daily_briefing_sent") or result.get("kory_24h_reminders"):
            logger.info("Kory briefings: %s", result)
    except Exception as exc:
        _log_orchestrator_error(
            step_name="kory_briefings",
            reference_id="daemon",
            message="Kory briefing cycle failed.",
            exc=exc,
        )


def _db_maintenance_interval() -> int:
    try:
        return max(10, int(os.getenv("LEXI_DB_MAINTENANCE_EVERY_CYCLES", "120")))
    except ValueError:
        return 120


def _run_db_maintenance() -> None:
    try:
        from app.jobs.db_maintenance import run_db_maintenance_cycle

        run_db_maintenance_cycle()
    except Exception as exc:
        _log_orchestrator_error(
            step_name="db_maintenance",
            reference_id="daemon",
            message="DB maintenance cycle failed.",
            exc=exc,
        )


def _recover_pending_triage() -> int:
    """Advance any proposals stuck in pending_triage (scheduler recovery)."""
    try:
        pending_ids = _fetch_proposal_ids_by_status(PENDING_TRIAGE)
        if not pending_ids:
            return 0
        processed = process_pending_schedules()
        count = len(processed)
        if count:
            logger.info("Recovered %s pending_triage proposal(s): %s", count, processed)
            for proposal_id in processed:
                if (
                    _fetch_proposal_status(proposal_id) == PENDING_APPROVAL
                    and settings.lexi_teams_enabled
                ):
                    schedule_teams_approval_push(proposal_id)
        return count
    except Exception as exc:
        _log_orchestrator_error(
            step_name="scheduler_recovery",
            reference_id="pending_triage",
            message="Pending triage recovery failed.",
            exc=exc,
        )
        return 0


def _poll_outlook_ingress() -> int:
    """Poll Kory inbox + sent items for messages Composio triggers may miss locally.

    Kory's Sent Items is what carries a delegation reply: the
    OUTLOOK_MESSAGE_TRIGGER webhook lives on Kory's connection only, and his
    reply never lands in his inbox, so this poll is that flow's only ingress.

    Lexi's mailbox holds the same message (she is CC'd) but is opt-in via
    LEXI_POLL_LEXI_MAILBOX: Graph ids are mailbox-scoped and the rest of the
    pipeline re-fetches a stored thread_id with Kory's connection, so ingesting
    Lexi-scoped ids makes those later reads 404.
    """
    if not settings.composio_api_key:
        return 0

    processed = 0
    window_start = datetime.now(timezone.utc) - timedelta(hours=24)
    for folder in ("inbox", "sentitems"):
        processed += _poll_outlook_folder(folder, window_start=window_start)
    if _lexi_mailbox_poll_enabled():
        processed += _poll_outlook_folder(
            "inbox", window_start=window_start, role="lexi"
        )
    return processed


def _lexi_mailbox_poll_enabled() -> bool:
    enabled = os.getenv("LEXI_POLL_LEXI_MAILBOX", "false").lower() in {"1", "true", "yes"}
    return enabled and bool((settings.lexi_mailbox_email or "").strip())


def _poll_outlook_folder(
    folder: str,
    *,
    window_start: datetime,
    role: str = "read",
) -> int:
    processed = 0
    try:
        from app.integrations.composio_client import execute_tool

        result = execute_tool(
            "OUTLOOK_LIST_MESSAGES",
            {
                "user_id": "me",
                "folder": folder,
                "top": 15,
                "orderby": ["receivedDateTime desc"],
                "select": [
                    "id",
                    "subject",
                    "from",
                    "toRecipients",
                    "ccRecipients",
                    "bccRecipients",
                    "receivedDateTime",
                    "bodyPreview",
                    "conversationId",
                ],
            },
            role=role,
        )
        messages = _extract_messages(result.get("data"))
    except ComposioNotConfiguredError:
        return 0
    except Exception as exc:
        _log_orchestrator_error(
            step_name="outlook_poll",
            reference_id=f"{role}:{folder}",
            message=f"Outlook poll failed for folder={folder} role={role}.",
            exc=exc,
        )
        return 0

    for message in reversed(messages):
        if _SHUTDOWN_REQUESTED.is_set():
            break
        message_id = str(message.get("id") or "").strip()
        if not message_id or _thread_already_ingested(message_id):
            continue

        subject_preview = str(message.get("subject") or "")
        if _skip_inbound_for_local_test_mode(subject=subject_preview):
            continue

        received_at = _parse_received_at(message.get("receivedDateTime"))
        if received_at and received_at < window_start:
            continue

        log_id: str | None = None
        try:
            full_message, log_id = get_message(message_id, role=role)
            full_message = merge_list_message_fields(full_message, message)
            normalized = normalize_message(
                full_message,
                {
                    "source": "orchestrator_poll",
                    "message_id": message_id,
                    "folder": folder,
                    "mailbox_role": role,
                },
            )
            recipients = extract_recipient_list(full_message)
            raw_email = build_inbound_raw_email(
                message_id=message_id,
                normalized=normalized,
                recipients=recipients,
            )
            handle_inbound_stream(raw_email)
            processed += 1
        except Exception as exc:
            _log_orchestrator_error(
                step_name="inbound_stream",
                reference_id=message_id,
                message="Outlook poller failed to process a message.",
                exc=exc,
                extra={
                    "composio_log_id": log_id,
                    "folder": folder,
                    "mailbox_role": role,
                },
            )
    return processed


def composio_webhook_to_lexi_email(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Convert a Composio trigger/webhook payload into a Lexi inbound email dict."""
    data = payload.get("data") or payload
    message_id = (
        data.get("message_id")
        or data.get("id")
        or (data.get("message") or {}).get("id")
    )
    if not message_id:
        return None

    try:
        full_message, _ = get_message(str(message_id))
        normalized = normalize_message(
            full_message,
            {"source": "composio_webhook", "message_id": message_id},
        )
        recipients = extract_recipient_list(full_message)
        return build_inbound_raw_email(
            message_id=str(message_id),
            normalized=normalized,
            recipients=recipients,
        )
    except Exception as exc:
        _log_orchestrator_error(
            step_name="webhook_normalization",
            reference_id=str(message_id),
            message="Failed to normalize Composio webhook payload.",
            exc=exc,
            extra={"payload": payload},
        )
        return None


def _proposal_slots_have_zero_conflicts(slots: list[dict[str, Any]]) -> bool:
    for slot in slots:
        start = slot.get("start")
        end = slot.get("end")
        if not start or not end:
            return False
        try:
            conflict, _, _ = has_conflict({"start": str(start), "end": str(end)})
        except Exception:
            return False
        if conflict:
            return False
    return True


def _sender_is_trusted_domain(sender: str) -> bool:
    return any(domain in sender for domain in TRUSTED_INTERNAL_DOMAINS)


def _thread_already_ingested(thread_id: str) -> bool:
    with get_lexi_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM email_threads WHERE thread_id = ? LIMIT 1",
            (thread_id,),
        ).fetchone()
        return row is not None


def _skip_inbound_for_local_test_mode(*, subject: str) -> bool:
    """When LEXI_LOCAL_MODE=true, ignore inbox mail unless subject contains TEST."""
    if os.getenv("LEXI_LOCAL_MODE", "").strip().lower() not in {"1", "true", "yes"}:
        return False
    return "test" not in (subject or "").strip().lower()


def _normalize_thread_subject(subject: str) -> str:
    s = (subject or "").strip().lower()
    while True:
        if s.startswith("re:"):
            s = s[3:].strip()
        elif s.startswith("fwd:"):
            s = s[4:].strip()
        else:
            break
    return s


def _find_proposal_for_delegation_followup(
    *,
    conversation_id: str,
    subject: str,
) -> int | None:
    with get_lexi_connection() as conn:
        if conversation_id:
            row = conn.execute(
                """
                SELECT p.id
                FROM proposals AS p
                INNER JOIN email_threads AS e ON e.thread_id = p.thread_id
                WHERE e.conversation_id = ?
                ORDER BY p.id DESC
                LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
            if row:
                return int(row["id"])

        norm = _normalize_thread_subject(subject)
        if not norm:
            return None
        rows = conn.execute(
            """
            SELECT e.thread_id, e.subject
            FROM email_threads AS e
            ORDER BY e.id DESC
            LIMIT 50
            """
        ).fetchall()
        for row in rows:
            if _normalize_thread_subject(str(row["subject"] or "")) != norm:
                continue
            prop = conn.execute(
                """
                SELECT id FROM proposals
                WHERE thread_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (row["thread_id"],),
            ).fetchone()
            if prop:
                return int(prop["id"])
    return None


def _update_proposal_conversation_id(proposal_id: int, conversation_id: str) -> None:
    if not conversation_id.strip():
        return
    with get_lexi_connection() as conn:
        row = conn.execute(
            "SELECT thread_id FROM proposals WHERE id = ?",
            (proposal_id,),
        ).fetchone()
        if not row:
            return
        conn.execute(
            """
            UPDATE email_threads
            SET conversation_id = COALESCE(conversation_id, ?)
            WHERE thread_id = ?
            """,
            (conversation_id.strip(), row["thread_id"]),
        )
        conn.commit()


def _reactivate_proposal_for_delegation(proposal_id: int) -> None:
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


def _handle_delegation_followup(
    raw_email: dict[str, Any],
    delegation: Any,
) -> dict[str, Any] | None:
    """When Kory CCs Lexi on an existing thread, re-open the staged proposal."""
    proposal_id = _find_proposal_for_delegation_followup(
        conversation_id=str(raw_email.get("conversation_id") or ""),
        subject=str(raw_email.get("subject") or ""),
    )
    if proposal_id is None:
        return None

    status = _fetch_proposal_status(proposal_id)
    if status not in {NO_REPLY_NEEDED, AWAITING_REPLY_PROMPT}:
        return None

    _update_proposal_conversation_id(
        proposal_id,
        str(raw_email.get("conversation_id") or ""),
    )
    set_proposal_delegation_metadata(
        proposal_id,
        voice_mode="lexi",
        send_channel="lexi",
        is_delegation=True,
        reply_message_id=str(raw_email.get("message_id") or raw_email.get("thread_id") or ""),
    )
    _reactivate_proposal_for_delegation(proposal_id)

    draft_result = begin_delegation_draft(proposal_id)
    final_status = str(draft_result.get("status") or _fetch_proposal_status(proposal_id) or "")

    if (
        final_status == PENDING_APPROVAL
        and settings.lexi_teams_enabled
        and draft_result.get("ok")
    ):
        schedule_teams_approval_push(proposal_id)

    _record_polled_message_id(str(raw_email.get("message_id") or raw_email.get("thread_id") or ""), raw_email)

    return {
        "proposal_id": proposal_id,
        "thread_id": raw_email.get("thread_id"),
        "action": "delegation_followup",
        "delegation_reason": delegation.reason,
        "final_status": final_status,
        "draft_result": draft_result,
        "skipped": False,
    }


def _record_polled_message_id(message_id: str, raw_email: dict[str, Any]) -> None:
    if not message_id.strip():
        return
    with get_lexi_connection() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO email_threads
            (thread_id, subject, sender, received_at, raw_body, conversation_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                message_id.strip(),
                str(raw_email.get("subject") or ""),
                str(raw_email.get("sender") or ""),
                str(raw_email.get("received_at") or ""),
                str(raw_email.get("raw_body") or "")[:4000],
                str(raw_email.get("conversation_id") or "").strip() or None,
            ),
        )
        conn.commit()


def _conversation_has_proposal(conversation_id: str) -> bool:
    """True when any proposal exists for this Outlook conversation."""
    if not conversation_id.strip():
        return False
    with get_lexi_connection() as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM proposals AS p
            INNER JOIN email_threads AS e ON e.thread_id = p.thread_id
            WHERE e.conversation_id = ?
            LIMIT 1
            """,
            (conversation_id.strip(),),
        ).fetchone()
        return row is not None


def _thread_has_active_proposal(thread_id: str) -> bool:
    with get_lexi_connection() as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM proposals
            WHERE thread_id = ?
              AND status IN (?, ?, ?)
            LIMIT 1
            """,
            (thread_id, PENDING_TRIAGE, PENDING_APPROVAL, AWAITING_REPLY_PROMPT),
        ).fetchone()
        return row is not None


def _duplicate_newsletter_burst(
    sender: str,
    subject: str,
    body: str,
    *,
    hours: int = 24,
) -> bool:
    """Skip repeat digests only — never block real thread follow-ups (Re: same subject)."""
    from app.agents.inbound_filter import is_newsletter_or_bulk_mail

    if not is_newsletter_or_bulk_mail(sender=sender, subject=subject, body=body):
        return False
    key = normalize_subject_key(subject)
    if not sender or not key:
        return False
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with get_lexi_connection() as conn:
        rows = conn.execute(
            """
            SELECT e.subject
            FROM proposals AS p
            INNER JOIN email_threads AS e ON e.thread_id = p.thread_id
            WHERE e.sender = ? AND p.created_at >= ?
            """,
            (sender, cutoff),
        ).fetchall()
    return any(normalize_subject_key(str(r["subject"] or "")) == key for r in rows)


def _mark_proposal_no_reply(proposal_id: int, *, reason: str) -> None:
    with get_lexi_connection() as conn:
        conn.execute(
            """
            UPDATE proposals
            SET status = ?, justification = COALESCE(justification, '') || ?
            WHERE id = ?
            """,
            (NO_REPLY_NEEDED, f" [auto-skip: {reason}]", proposal_id),
        )
        conn.execute(
            """
            INSERT INTO audit_log (step_name, reference_id, log_level, message, payload)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "inbound_auto_skip",
                str(proposal_id),
                "INFO",
                f"Skipped Teams notification ({reason}).",
                json.dumps({"proposal_id": proposal_id, "reason": reason}),
            ),
        )
        conn.commit()


def _fetch_proposal_status(proposal_id: int) -> str | None:
    with get_lexi_connection() as conn:
        row = conn.execute(
            "SELECT status FROM proposals WHERE id = ?",
            (proposal_id,),
        ).fetchone()
        return str(row["status"]) if row else None


def _fetch_proposal_ids_by_status(status: str) -> list[int]:
    with get_lexi_connection() as conn:
        rows = conn.execute(
            "SELECT id FROM proposals WHERE status = ? ORDER BY id ASC",
            (status,),
        ).fetchall()
        return [int(row["id"]) for row in rows]


def _fetch_proposal_bundle(proposal_id: int) -> dict[str, Any] | None:
    with get_lexi_connection() as conn:
        row = conn.execute(
            """
            SELECT
                p.id,
                p.thread_id,
                p.status,
                p.intent_classification,
                p.priority_tier,
                p.proposed_slots,
                p.drafted_reply,
                p.confidence_score,
                p.justification,
                e.sender,
                e.subject,
                e.raw_body
            FROM proposals AS p
            INNER JOIN email_threads AS e ON e.thread_id = p.thread_id
            WHERE p.id = ?
            """,
            (proposal_id,),
        ).fetchone()
        return dict(row) if row else None


def _insert_audit_log(
    *,
    step_name: str,
    reference_id: str,
    log_level: str,
    message: str,
    payload: dict[str, Any],
) -> None:
    with get_lexi_connection() as conn:
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
        conn.commit()


def _log_orchestrator_error(
    *,
    step_name: str,
    reference_id: str,
    message: str,
    exc: BaseException,
    extra: dict[str, Any] | None = None,
) -> None:
    payload = {
        "error": f"{type(exc).__name__}: {exc}",
        "traceback": traceback.format_exc(),
        **(extra or {}),
    }
    logger.exception("%s | %s", step_name, message)
    try:
        _insert_audit_log(
            step_name=step_name,
            reference_id=reference_id,
            log_level="ERROR",
            message=message,
            payload=payload,
        )
    except sqlite3.Error:
        logger.exception("Failed to persist orchestrator audit log.")


def _register_signal_handlers() -> None:
    def _handle_signal(signum: int, _frame: Any) -> None:
        print(f"\n[lexi-orchestrator] received signal {signum}; shutting down...", file=sys.stderr)
        _SHUTDOWN_REQUESTED.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle_signal)
        except ValueError:
            pass


def _extract_messages(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        messages = data.get("value") or data.get("messages") or data.get("data") or []
        return messages if isinstance(messages, list) else []
    return []


def _parse_received_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_json_list(value: Any) -> list[dict[str, Any]]:
    if not value:
        return []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    return []


def _configure_logging() -> None:
    if logger.handlers:
        return
    logging.basicConfig(
        level=os.getenv("LEXI_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


if __name__ == "__main__":
    _configure_logging()
    interval = int(os.getenv("LEXI_ORCHESTRATOR_INTERVAL", "30"))
    run_orchestration_daemon(interval_seconds=interval)
