"""Minimal Composio webhook HTTP server (aiohttp) — not a full Lexi FastAPI app."""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from typing import Any

from aiohttp import web

from app.workflows.webhooks import accept_composio_webhook

logger = logging.getLogger(__name__)

WEBHOOK_PATH = "/webhooks/composio"


def _webhook_secret() -> str:
    import os

    return os.getenv("LEXI_WEBHOOK_SECRET", "").strip()


def _webhook_secret_ok(request: web.Request) -> bool:
    """Shared-secret gate for the public webhook (audit 2026-08-15, A1).

    The endpoint is reachable from the internet through traefik; the handler
    otherwise authenticates nothing. Composio is registered with the secret as
    the `k` query param (or it may send an `X-Lexi-Webhook-Secret` header).

    FAIL-OPEN when unset: shipping this cannot break live ingestion. Enforcement
    turns on the moment LEXI_WEBHOOK_SECRET is set AND Composio is re-registered
    with the matching `?k=` — see the Tier-2 note in PRE_HANDOVER_AUDIT.md.
    """
    secret = _webhook_secret()
    if not secret:
        return True
    import hmac

    provided = request.query.get("k") or request.headers.get("X-Lexi-Webhook-Secret") or ""
    return hmac.compare_digest(provided, secret)


async def _composio_webhook_handler(request: web.Request) -> web.Response:
    if not _webhook_secret_ok(request):
        return web.json_response(
            {"ok": False, "queued": False, "error": "unauthorized"},
            status=401,
        )
    try:
        payload = await request.json()
    except Exception as exc:
        return web.json_response(
            {"ok": False, "queued": False, "error": f"invalid_json: {exc}"},
            status=400,
        )

    if not isinstance(payload, dict):
        return web.json_response(
            {"ok": False, "queued": False, "error": "payload_must_be_object"},
            status=400,
        )

    try:
        result = accept_composio_webhook(payload)
    except Exception as exc:
        logger.exception("Composio webhook handler failed.")
        return web.json_response(
            {
                "ok": False,
                "queued": False,
                "error": f"{type(exc).__name__}: {exc}",
            },
            status=202,
        )

    return web.json_response(result, status=202)


async def _health_handler(_request: web.Request) -> web.Response:
    from app.bot.teams_conversation_store import load_conversation_reference, teams_delivery_ready
    from app.config import settings
    from app.storage.heartbeat import heartbeat_age_seconds

    # DB write check.
    db_ok = True
    try:
        from app.storage.lexi_db import get_lexi_connection

        with get_lexi_connection() as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception:
        db_ok = False

    age = heartbeat_age_seconds()
    # Stale if the orchestrator hasn't cycled in > 5 min (well beyond the 30s interval).
    heartbeat_stale = age is not None and age > 300
    healthy = db_ok and not heartbeat_stale

    budget = None
    try:
        from app.storage.composio_call_log import budget_status

        budget = budget_status()
    except Exception:
        pass

    payload = {
        "status": "ok" if healthy else "degraded",
        "service": "lexi-worker",
        "webhook_path": WEBHOOK_PATH,
        "lexi_write_mode": settings.lexi_write_mode,
        "db_writable": db_ok,
        "heartbeat_age_seconds": round(age, 1) if age is not None else None,
        "heartbeat_stale": heartbeat_stale,
        "composio_budget": budget,
        "teams_cards_ready": teams_delivery_ready(),
        "teams_conversation_captured": load_conversation_reference() is not None,
    }
    return web.json_response(payload, status=200 if healthy else 503)


def _build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/health", _health_handler)
    app.router.add_post(WEBHOOK_PATH, _composio_webhook_handler)
    return app


class WebhookServerThread:
    """Run aiohttp webhook server in a background thread."""

    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._runner: web.AppRunner | None = None

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self._port}{WEBHOOK_PATH}"

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        def _run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            app = _build_app()
            runner = web.AppRunner(app)
            self._runner = runner
            loop.run_until_complete(runner.setup())
            site = web.TCPSite(runner, self._host, self._port)
            loop.run_until_complete(site.start())
            logger.info("Lexi webhook listening on %s", self.url)
            print(f"[lexi-worker] Composio webhook → {self.url}", file=sys.stderr, flush=True)
            loop.run_forever()

        self._thread = threading.Thread(target=_run, name="lexi-webhook", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._loop is None or self._runner is None:
            return

        async def _shutdown() -> None:
            await self._runner.cleanup()

        try:
            asyncio.run_coroutine_threadsafe(_shutdown(), self._loop).result(timeout=5)
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:
            logger.exception("Webhook server shutdown error.")
