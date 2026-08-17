"""Pytest global fixtures — deterministic, hermetic test environment (plan Phase 1).

Two guarantees for the whole suite:

1. **Deterministic safe env.** Baseline safety-gate env vars are forced BEFORE
   ``app.config`` is imported, so tests never depend on the developer's live
   ``.env``. Every gate is on; write mode is sandbox; the run is LEXI_ENV=testing.

2. **Hermetic (no real network).** An autouse fixture makes the real Composio
   SDK factory and the real LLM client factory raise, so a test can never reach
   a real mailbox, calendar, CRM, or the Anthropic API by accident. Tests that
   intentionally exercise a live path opt in with ``@pytest.mark.live`` (never
   run in CI's default ``-m "not live"`` selection).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# --- 1. Force the safe baseline BEFORE any app import ------------------------
# app.config calls load_dotenv(override=False), so values set here win over .env.
_TEST_ENV = {
    "LEXI_ENV": "testing",
    "LEXI_WRITE_MODE": "sandbox",
    "LEXI_DRY_RUN": "true",
    "LEXI_KORY_OUTBOUND_BLOCKED": "true",
    "LEXI_KORY_SPACE_READ_ONLY": "true",
    "LEXI_ASANA_LIVE_WRITES_ENABLED": "false",
    "LEXI_HUBSPOT_LIVE_WRITES_ENABLED": "false",
    "LEXI_OUTREACH_LIVE_SENDS_ENABLED": "false",
    "LEXI_OUTREACH_OUTLOOK_DRAFTS_ENABLED": "false",
    "LEXI_REQUIRE_KORY_APPROVAL": "true",
    "LEXI_AUTO_EXECUTE_ENABLED": "false",
    "LEXI_ALLOW_IMMEDIATE_SEND": "false",
    "LEXI_TEAMS_ENABLED": "false",
    "LEXI_SUPPRESS_TEAMS_PUSH": "true",
    "LEXI_EMBED_WORKER": "false",
    "LEXI_ORCHESTRATOR_ENABLED": "false",
    # Identity default. config.py falls back to "kory" when this is unset, and .env
    # is gitignored — so the send-channel tests read the developer's own .env and
    # passed locally while failing on a runner that has none. Pinned to the
    # production identity rule (Lexi voice unless a Kory sign-off is detected).
    "LEXI_DEFAULT_SEND_CHANNEL": "lexi",
    # 3. Own database. The suite used to inherit LEXI_DATABASE_PATH from .env and
    # share whatever store the developer's stack was pointed at. That is not just
    # untidy: a fixture proposal parked in pending_invite was picked up by a
    # locally running orchestrator polling the same file and pushed to real Teams
    # as an Adaptive Card. Tests must never share a database with something that
    # acts on rows. Deliberately not data/lexi_test.db — run_teams_uat_livewrite.sh
    # owns that one.
    "LEXI_DATABASE_PATH": "data/lexi_pytest.db",
}
for _k, _v in _TEST_ENV.items():
    os.environ[_k] = _v

# Rebuild that database from scratch each run. Carrying rows between runs is what
# made failures depend on when you ran the suite (aged-ask tests drifting in and
# out as their fixtures fell outside a date window).
_DB = Path(__file__).resolve().parent / _TEST_ENV["LEXI_DATABASE_PATH"]
for _suffix in ("", "-wal", "-shm"):
    Path(str(_DB) + _suffix).unlink(missing_ok=True)

from scripts.init_lexi_db import init_lexi_db  # noqa: E402  (must follow the env above)

init_lexi_db(_DB)

import pytest  # noqa: E402

# --- 4. Opt-in frozen clock, to run the suite *as if* it were another day -----
# Scheduling tests are unusually exposed to the calendar: a fixture pinned to a
# real date drifts in and out of "next week" as time passes, and a suite that
# means something different each morning cannot certify anything. On 2026-08-17
# test_outbound_note_discloses_out_of_window_offers inverted for exactly that
# reason — August 26 had quietly become "next week" — and three send-gate tests
# in test_draft_slot_sync.py were asserting refusals they no longer got.
#
# Started HERE, at conftest import, rather than in a fixture: test modules
# compute date fixtures at module scope, which runs during collection. A
# per-test freeze activates too late and reports false failures against
# constants that were built from the real clock.
#
#     LEXI_TEST_FAKE_TODAY=2026-11-02 .venv/bin/python -m pytest -q \
#         --ignore=tests/test_api_v1.py
#
# Pick dates crossing a DST flip, a month end and a year end. test_api_v1 is
# excluded because freezegun breaks pydantic.v1's import, which is a limitation
# of the audit tool and not a finding. tick=True keeps the clock moving so
# nothing waiting on elapsed time can hang.
_FAKE_TODAY = os.getenv("LEXI_TEST_FAKE_TODAY", "").strip()
if _FAKE_TODAY:
    from freezegun import freeze_time  # noqa: E402

    _FREEZER = freeze_time(_FAKE_TODAY, tick=True)
    _FREEZER.start()

    def pytest_unconfigure(config: pytest.Config) -> None:
        _FREEZER.stop()


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "live: test intentionally makes real Composio/LLM/network calls "
        "(excluded from CI via -m 'not live').",
    )
    # Ensure the DB the app actually connects to (resolve_lexi_database_path — which
    # may be the .env.testing test DB) has the full schema before any test runs.
    from app.config import resolve_lexi_database_path
    from scripts.init_lexi_db import init_lexi_db

    init_lexi_db(resolve_lexi_database_path())


@pytest.fixture(autouse=True)
def _no_real_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """Block the real Composio SDK and LLM client unless the test is @pytest.mark.live."""
    if request.node.get_closest_marker("live"):
        yield
        return

    def _blocked_composio(*_a, **_k):
        raise RuntimeError(
            "Real Composio call blocked in tests. Mock execute_tool/get_composio, "
            "or mark the test @pytest.mark.live."
        )

    def _blocked_llm(*_a, **_k):
        raise RuntimeError(
            "Real LLM call blocked in tests. Mock the client, "
            "or mark the test @pytest.mark.live."
        )

    # Patch at the definition site; tests that mock execute_tool never reach these.
    monkeypatch.setattr(
        "app.integrations.composio_client.get_composio", _blocked_composio, raising=False
    )
    monkeypatch.setattr(
        "app.llm.hermes_client.get_hermes_client", _blocked_llm, raising=False
    )
    yield


@pytest.fixture
def live_writes():
    """Flip settings.lexi_dry_run off for a test that must exercise the live/approval path.

    The single shared frozen ``settings`` object is imported by-reference across
    modules, so mutating it here affects every reader (composio_client,
    approval_gate, outlook_email, ...). Restored on teardown.
    """
    from app.config import settings

    prev = settings.lexi_dry_run
    object.__setattr__(settings, "lexi_dry_run", False)
    try:
        yield
    finally:
        object.__setattr__(settings, "lexi_dry_run", prev)
