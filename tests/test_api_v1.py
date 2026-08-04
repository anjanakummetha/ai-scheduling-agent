"""Read-only /api/v1 dashboard API (token auth + shape)."""

from __future__ import annotations

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient

from app.storage.lexi_db import get_lexi_connection


TOKEN = "test-token-123"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("LEXI_API_ENABLED", "true")
    monkeypatch.setenv("LEXI_API_TOKEN", TOKEN)
    monkeypatch.setenv("LEXI_DASHBOARD_ENABLED", "false")
    from app.main import create_app

    return TestClient(create_app())


@pytest.fixture
def seed_proposal():
    import uuid

    thread = f"apiv1-thread-{uuid.uuid4().hex[:8]}"
    with get_lexi_connection() as conn:
        conn.execute(
            "INSERT INTO email_threads(thread_id, subject, sender) VALUES (?,?,?)",
            (thread, "TEST — intro", "prospect@example.com"),
        )
        cur = conn.execute(
            "INSERT INTO proposals(thread_id, status, intent_classification, proposed_slots) "
            "VALUES (?,?,?,?)",
            (thread, "pending_approval", "referral_or_intro",
             '[{"start":"2026-07-28T09:00:00-06:00","end":"2026-07-28T09:30:00-06:00"}]'),
        )
        pid = cur.lastrowid
        conn.execute(
            "INSERT INTO holds(proposal_id, event_id, slot_start, slot_end, expires_at) "
            "VALUES (?,?,?,?,?)",
            (pid, "evt-1", "2026-07-28T09:00:00-06:00", "2026-07-28T09:30:00-06:00",
             "2026-07-31T00:00:00Z"),
        )
        conn.commit()
    yield pid


def test_health_is_public(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert "db_ok" in r.json()


def test_pending_requires_token(client):
    assert client.get("/api/v1/pending-approvals").status_code == 401
    assert client.get(
        "/api/v1/pending-approvals", headers={"Authorization": "Bearer wrong"}
    ).status_code == 401


def test_pending_approvals_with_token(client, seed_proposal):
    r = client.get("/api/v1/pending-approvals", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    data = r.json()
    assert data["count"] >= 1
    item = next(i for i in data["items"] if i["id"] == seed_proposal)
    assert item["subject"] == "TEST — intro"
    assert item["requester"] == "prospect@example.com"
    assert isinstance(item["proposed_slots"], list) and item["proposed_slots"]


def test_holds_with_token(client, seed_proposal):
    r = client.get("/api/v1/holds", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    assert r.json()["count"] >= 1


def test_costs_and_audit_shape(client):
    h = {"Authorization": f"Bearer {TOKEN}"}
    assert client.get("/api/v1/costs", headers=h).status_code == 200
    assert "items" in client.get("/api/v1/audit?limit=5", headers=h).json()


def test_api_disabled_without_env(monkeypatch):
    monkeypatch.delenv("LEXI_API_ENABLED", raising=False)
    monkeypatch.setenv("LEXI_DASHBOARD_ENABLED", "false")
    from app.main import create_app

    c = TestClient(create_app())
    # Router not mounted → 404, never an unauthenticated data leak.
    assert c.get("/api/v1/pending-approvals").status_code == 404


# --- /unanswered-scheduling -------------------------------------------------
# Teams no longer cards cold scheduling mail, so these asks have to surface
# somewhere. This endpoint is what the morning briefing batches.


@pytest.fixture
def seed_aged_scheduling():
    """Three staged asks: 50h old, 2h old, and one already handled."""
    import uuid

    made = []
    with get_lexi_connection() as conn:
        # Purge leftovers from earlier runs — the suite shares the local DB, so
        # these 50h-old seeds accumulate until they push the current run's row
        # past the endpoint's result cap and the test fails with no code change.
        conn.execute("DELETE FROM proposals WHERE thread_id LIKE 'unans-%'")
        conn.execute("DELETE FROM email_threads WHERE thread_id LIKE 'unans-%'")
        for label, age, status in (
            ("stale", "-50 hours", "awaiting_reply_prompt"),
            ("fresh", "-2 hours", "awaiting_reply_prompt"),
            ("handled", "-50 hours", "executed"),
        ):
            thread = f"unans-{label}-{uuid.uuid4().hex[:8]}"
            conn.execute(
                "INSERT INTO email_threads(thread_id, subject, sender) VALUES (?,?,?)",
                (thread, f"[TEST] {label} ask", "prospect@example.com"),
            )
            cur = conn.execute(
                "INSERT INTO proposals(thread_id, status, intent_classification, created_at) "
                "VALUES (?,?,?,datetime('now',?))",
                (thread, status, "referral_or_intro", age),
            )
            made.append((label, cur.lastrowid))
        conn.commit()
    yield dict(made)
    with get_lexi_connection() as conn:
        conn.execute("DELETE FROM proposals WHERE thread_id LIKE 'unans-%'")
        conn.execute("DELETE FROM email_threads WHERE thread_id LIKE 'unans-%'")
        conn.commit()


def test_unanswered_scheduling_requires_a_token(client) -> None:
    assert client.get("/api/v1/unanswered-scheduling").status_code == 401


def test_unanswered_scheduling_returns_only_aged_staged_asks(client, seed_aged_scheduling) -> None:
    # max_days=3 isolates the seeded rows from whatever history the DB carries.
    response = client.get(
        "/api/v1/unanswered-scheduling?min_hours=24&max_days=3",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 200
    ids = {item["id"] for item in response.json()["items"]}

    assert seed_aged_scheduling["stale"] in ids, "a 50h-old staged ask must surface"
    assert seed_aged_scheduling["fresh"] not in ids, "a 2h-old ask is not yet unanswered"
    assert seed_aged_scheduling["handled"] not in ids, "executed proposals are done"


def test_unanswered_scheduling_reports_age_and_oldest_first(client, seed_aged_scheduling) -> None:
    response = client.get(
        "/api/v1/unanswered-scheduling?min_hours=24&max_days=3",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    items = response.json()["items"]
    stale = next(i for i in items if i["id"] == seed_aged_scheduling["stale"])
    assert stale["age_hours"] >= 24
    assert stale["requester"] == "prospect@example.com"
    ages = [i["age_hours"] for i in items]
    assert ages == sorted(ages, reverse=True), "oldest first — it has waited longest"
