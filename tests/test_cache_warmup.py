"""Startup pre-fetches what Kory's first request needs.

Measured on the box 2026-08-18 through the gateway's tool path: availability
took 17.5s on the first call and 7ms after; today's calendar 1.1s then 774ms.
The cost is per-process, and every deploy restarts the service, so his FIRST
message of the day paid ~20s while everything after was instant. The engine was
never the problem — it runs in 2-3ms.
"""

from __future__ import annotations

from unittest.mock import patch

import app.worker.runner as runner


def test_warmup_touches_the_two_reads_the_first_request_needs():
    calls: list[str] = []
    with patch(
        "app.integrations.named_calendars.list_all_calendars",
        side_effect=lambda *a, **k: calls.append("calendars"),
    ), patch(
        "app.scheduling.calendar_context.load_scheduling_calendar_context",
        side_effect=lambda *a, **k: calls.append("context"),
    ):
        runner._start_cache_warmup()
        for thread in __import__("threading").enumerate():
            if thread.name == "lexi-cache-warmup":
                thread.join(timeout=5)
    assert set(calls) == {"calendars", "context"}, calls


def test_a_failing_warmup_never_breaks_startup():
    """Composio down at boot must cost latency, not the worker."""
    with patch(
        "app.integrations.named_calendars.list_all_calendars",
        side_effect=RuntimeError("composio down"),
    ):
        runner._start_cache_warmup()  # must not raise
        for thread in __import__("threading").enumerate():
            if thread.name == "lexi-cache-warmup":
                thread.join(timeout=5)


def test_warmup_runs_off_the_startup_thread():
    """A slow Composio must not delay the worker coming up."""
    import threading

    started = threading.Event()
    with patch(
        "app.integrations.named_calendars.list_all_calendars",
        side_effect=lambda *a, **k: started.set() or __import__("time").sleep(0.3),
    ), patch(
        "app.scheduling.calendar_context.load_scheduling_calendar_context",
        return_value={},
    ):
        runner._start_cache_warmup()  # returns immediately
        assert started.wait(timeout=5), "warmup never ran"


def test_the_warmup_can_be_switched_off():
    with patch.dict("os.environ", {"LEXI_CACHE_WARMUP": "false"}), patch(
        "app.integrations.named_calendars.list_all_calendars"
    ) as m:
        runner._start_cache_warmup()
        assert not m.called
