"""Headless Lexi orchestrator — email ingress without FastAPI :8000."""

from __future__ import annotations

import logging
import os
import sys
import threading
from typing import Any

from app.orchestrator import request_orchestrator_shutdown, run_orchestration_daemon
from app.worker.webhook_server import WebhookServerThread
from scripts.init_lexi_db import init_lexi_db

logger = logging.getLogger(__name__)

_worker_lock = threading.Lock()
_orchestrator_thread: threading.Thread | None = None
_webhook_server: WebhookServerThread | None = None
_worker_started = False


def _env_truthy(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).lower() in {"1", "true", "yes"}


def _webhook_enabled(explicit: bool | None) -> bool:
    if explicit is not None:
        return explicit
    if _env_truthy("LEXI_WEBHOOK_ENABLED"):
        return True
    # Webhook on if a port is set explicitly (non-zero).
    port = os.getenv("LEXI_WEBHOOK_PORT", "").strip()
    return bool(port and port != "0")


def is_worker_running() -> bool:
    return _worker_started and _orchestrator_thread is not None and _orchestrator_thread.is_alive()


def start_lexi_worker(
    *,
    webhook: bool | None = None,
    poll_outlook: bool | None = None,
    interval_seconds: int | None = None,
) -> dict[str, Any]:
    """Start Lexi background worker (idempotent).

    Production default (LEXI_WEBHOOK_ENABLED=true):
      - Composio webhook on LEXI_WEBHOOK_PORT (8780)
      - No frequent inbox poll (saves Composio budget)
      - Optional backup poll via LEXI_ORCHESTRATOR_BACKUP_POLL_MINUTES
    """
    global _orchestrator_thread, _webhook_server, _worker_started

    with _worker_lock:
        if _worker_started and _orchestrator_thread and _orchestrator_thread.is_alive():
            return _worker_status("already_running")

        init_lexi_db()

        # Rotating file + console logging (plan Phase 4).
        try:
            from app.utils.logging_setup import configure_logging

            configure_logging()
        except Exception:
            pass

        # Safety posture banner — the effective value of every kill switch at boot.
        from app.config import safety_posture_summary

        posture = safety_posture_summary()
        gates = " ".join(f"{k.replace('LEXI_', '')}={v}" for k, v in posture.items())
        print(f"[lexi-worker] SAFETY POSTURE | {gates}", file=sys.stderr, flush=True)

        use_webhook = _webhook_enabled(webhook)
        if poll_outlook is None:
            # Poll when webhook is off; both can run if explicitly enabled.
            poll = not use_webhook or _env_truthy("LEXI_ORCHESTRATOR_POLL_OUTLOOK", "false")
        else:
            poll = poll_outlook

        os.environ["LEXI_ORCHESTRATOR_ENABLED"] = "true"
        os.environ["LEXI_ORCHESTRATOR_POLL_OUTLOOK"] = "true" if poll else "false"

        interval = interval_seconds or int(os.getenv("LEXI_ORCHESTRATOR_INTERVAL", "30"))

        if use_webhook:
            # Default to loopback (audit 2026-08-15, A1): the box's traefik runs
            # host-network and forwards /webhooks to 127.0.0.1:8780, so binding
            # loopback keeps ingestion working while removing the direct public
            # 0.0.0.0:8780 attack surface. Override only if a proxy needs a
            # non-loopback address.
            host = os.getenv("LEXI_WEBHOOK_HOST", "127.0.0.1").strip() or "127.0.0.1"
            port = int(os.getenv("LEXI_WEBHOOK_PORT", "8780"))
            _webhook_server = WebhookServerThread(host=host, port=port)
            _webhook_server.start()

        _orchestrator_thread = threading.Thread(
            target=run_orchestration_daemon,
            kwargs={"interval_seconds": interval},
            name="lexi-orchestrator",
            daemon=True,
        )
        _orchestrator_thread.start()
        _worker_started = True

        mode = []
        if use_webhook:
            mode.append("webhook")
        if poll:
            mode.append("poll_kory_inbox")
        backup_min = int(os.getenv("LEXI_ORCHESTRATOR_BACKUP_POLL_MINUTES", "0") or 0)
        if not poll and backup_min > 0:
            mode.append(f"backup_poll_{backup_min}m")

        from app.orchestrator import describe_ingress_mode

        ingress = describe_ingress_mode(interval_seconds=interval)

        print(
            f"[lexi-worker] started | modes={'+'.join(mode) or 'orchestrator_only'} "
            f"| interval={interval}s | ingress={ingress['mode']} | teams=hermes_only",
            file=sys.stderr,
            flush=True,
        )
        _start_cache_warmup()
        status = _worker_status("started")
        status["ingress"] = ingress
        return status


def _start_cache_warmup() -> None:
    """Pre-fetch the reads Kory's first scheduling request needs.

    Measured on the box 2026-08-18, through the gateway's own tool path:

        availability (7 days)   first call 17.5s   then  7ms
        today's calendar        first call  1.1s   then 774ms
        pending                 first call  1.3s   then  1.6ms

    The cost is per-process: Composio's tool schema is fetched before every
    execute until it is cached, and the calendar context caches after its first
    read. Every deploy restarts the service and clears the Hermes session, so
    Kory's FIRST message of the day reliably paid the full ~20s while every
    message after it was instant. That is the shape of "she's slow" — it was
    never the engine, which runs in 2-3ms.

    Runs on a daemon thread so a slow or failing Composio never delays or
    blocks startup; a failure here costs nothing but the old latency.
    """
    import threading

    def _warm() -> None:
        try:
            from app.integrations.named_calendars import list_all_calendars
            from app.scheduling.calendar_context import load_scheduling_calendar_context

            list_all_calendars()
            load_scheduling_calendar_context(subject="", body="next week")
            logger.info("cache warmup complete")
        except Exception:  # noqa: BLE001 — warmup is best-effort by design
            logger.warning("cache warmup skipped", exc_info=True)

    if os.getenv("LEXI_CACHE_WARMUP", "true").strip().lower() in {"1", "true", "yes"}:
        threading.Thread(target=_warm, name="lexi-cache-warmup", daemon=True).start()


def stop_lexi_worker() -> None:
    """Signal orchestrator shutdown (best-effort)."""
    global _worker_started
    request_orchestrator_shutdown()
    _worker_started = False


def _worker_status(state: str) -> dict[str, Any]:
    from app.orchestrator import describe_ingress_mode

    webhook_url = _webhook_server.url if _webhook_server else None
    interval = int(os.getenv("LEXI_ORCHESTRATOR_INTERVAL", "30"))
    ingress = describe_ingress_mode(interval_seconds=interval)
    public = os.getenv("LEXI_WEBHOOK_PUBLIC_URL", "").strip()
    return {
        "state": state,
        "running": is_worker_running(),
        "webhook_url": webhook_url,
        "webhook_public_url": public or None,
        "poll_outlook": _env_truthy("LEXI_ORCHESTRATOR_POLL_OUTLOOK", "false"),
        "backup_poll_minutes": ingress.get("backup_poll_minutes", 0),
        "ingress": ingress,
        "interval_seconds": interval,
        "teams_path": "hermes_only",
    }
