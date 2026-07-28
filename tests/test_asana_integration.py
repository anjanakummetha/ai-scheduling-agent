"""Asana regressions found testing live writes against Kory's real account."""

from __future__ import annotations

from datetime import date, timedelta

from app.integrations.asana_manager import normalize_due_on


def test_past_due_date_rolls_forward():
    """Live bug: "August 3" was saved as 2025-08-03, a date already past."""
    assert normalize_due_on("2025-08-03") == "2026-08-03"


def test_future_date_is_untouched():
    future = (date.today() + timedelta(days=30)).isoformat()
    assert normalize_due_on(future) == future


def test_today_is_kept():
    today = date.today().isoformat()
    assert normalize_due_on(today) == today


def test_unparseable_input_is_not_mangled():
    """Truncating to 10 chars turned "next friday" into "next frida"."""
    assert normalize_due_on("next friday") == "next friday"
    assert normalize_due_on("") == ""


def test_write_slugs_exist_in_composio_toolkit():
    """ASANA_DELETE_A_TASK and ASANA_CREATE_A_STORY_COMMENT do not exist;
    the real slugs are ASANA_DELETE_TASK and ASANA_CREATE_TASK_COMMENT."""
    import app.integrations.asana_manager as am
    from pathlib import Path

    source = Path(am.__file__).read_text()
    assert "ASANA_DELETE_A_TASK" not in source
    assert "ASANA_CREATE_A_STORY_COMMENT" not in source
    assert "ASANA_DELETE_TASK" in source
    assert "ASANA_CREATE_TASK_COMMENT" in source


def test_task_name_resolves_to_gid(monkeypatch):
    """Chat only has the task name; Asana needs a numeric id, and passing the
    name through produced "task: Not a Long: <name>"."""
    import app.integrations.asana_manager as am

    monkeypatch.setattr(am, "search_asana_tasks", lambda *, query, limit=5: {
        "tasks": [{"gid": "123456", "name": "LEXI TEST 2 — delete me"}]
    })
    assert am.resolve_task_gid("LEXI TEST 2 — delete me") == "123456"
    assert am.resolve_task_gid("999") == "999"      # already a gid
    assert am.resolve_task_gid("") == ""


def test_section_name_resolves_to_gid(monkeypatch):
    """"Add it to YPO" has to reach the YPO board, not Reservation Reminders."""
    import app.integrations.asana_manager as am

    monkeypatch.setattr(am, "list_project_sections", lambda project_gid="": [
        {"gid": "1", "name": "General"},
        {"gid": "2", "name": "Personal"},
        {"gid": "3", "name": "YPO"},
    ])
    assert am.resolve_section_gid("YPO") == "3"
    assert am.resolve_section_gid("ypo") == "3"
    assert am.resolve_section_gid("personal") == "2"
    assert am.resolve_section_gid("777") == "777"
    assert am.resolve_section_gid("Nonexistent Board") == ""


def test_mcp_results_carry_todays_date():
    """The model believed it was 2025; every tool result now states the date."""
    import json
    from datetime import datetime
    from zoneinfo import ZoneInfo
    import hermes_mcp_server as h

    payload = json.loads(h._ok({"action": "x", "result": {}}))
    assert payload["today"].startswith(
        datetime.now(tz=ZoneInfo("America/Denver")).strftime("%Y-%m-%d")
    )


def test_create_returns_the_task_id_and_places_the_section(monkeypatch):
    """A NameError inside the create path was swallowed by a broad except, so
    the task was created in Asana but came back with task_id=None — which
    silently skipped both the due date and the section placement."""
    import app.integrations.asana_manager as am

    calls = []

    def fake_tool(slug, args):
        calls.append((slug, args))
        if slug == "ASANA_CREATE_A_TASK":
            return {"data": {"data": {"gid": "555", "name": args["data"]["name"]}}}
        return {"data": {}}

    monkeypatch.setattr(am, "execute_asana_tool", fake_tool)
    monkeypatch.setattr(am, "_should_simulate_asana", lambda: False)
    monkeypatch.setattr(
        am, "settings", type("S", (), {"asana_project_gid": "proj-1", "asana_section_gid": ""})()
    )
    monkeypatch.setattr(am, "resolve_section_gid", lambda section, project_gid="": "sec-9")

    result = am._create_asana_task(title="T", notes="N", section="YPO")
    assert result["ok"] is True
    assert result["task_id"] == "555", result
    assert any(slug == "ASANA_ADD_TASK_TO_SECTION" for slug, _ in calls), calls
