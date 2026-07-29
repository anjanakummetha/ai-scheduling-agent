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


def test_recent_past_dates_are_not_thrown_a_year_forward():
    """A task due yesterday must not silently become due next year.

    The MT reference and the local date differ across midnight, so "today" was
    landing a year out — with Asana live writes on."""
    from datetime import timedelta

    for days in (1, 3, 14):
        recent = (date.today() - timedelta(days=days)).isoformat()
        assert normalize_due_on(recent) == recent, f"{days}d ago must be left alone"


def test_clearly_stale_dates_still_roll_forward():
    """The original bug: "August 3" resolved to last year."""
    from datetime import timedelta

    stale = (date.today() - timedelta(days=300)).isoformat()
    assert normalize_due_on(stale) > date.today().isoformat()


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

    monkeypatch.setattr(am, "search_asana_tasks", lambda *, query, limit=5, mine_only=True: {
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


def test_date_buckets_need_opt_fields(monkeypatch):
    """Asana returns only gid+name unless opt_fields asks for more, so due_on
    and completed were always None and every date bucket came back empty —
    29 genuinely overdue tasks were reported as "you're all clear"."""
    import app.integrations.asana_manager as am

    seen = {}

    def fake_tool(slug, args):
        seen["args"] = args
        return {"data": {"data": [
            {"gid": "1", "name": "old thing", "due_on": "2020-01-01", "completed": False},
            {"gid": "2", "name": "done thing", "due_on": "2020-01-01", "completed": True},
            {"gid": "3", "name": "future thing", "due_on": "2999-01-01", "completed": False},
        ]}}

    monkeypatch.setattr(am, "execute_asana_tool", fake_tool)
    monkeypatch.setattr(am, "_should_simulate_asana", lambda: False)

    result = am.list_asana_tasks(bucket="overdue", project_gid="proj-1", mine_only=False)
    assert "opt_fields" in seen["args"], "must request due_on/completed"
    assert "due_on" in seen["args"]["opt_fields"]
    names = [t["name"] for t in result["tasks"]]
    assert names == ["old thing"], names  # not completed, not future


def test_buckets_span_every_project(monkeypatch):
    """Overdue should cover all of Kory's projects, not just the personal one."""
    import app.integrations.asana_manager as am

    monkeypatch.setattr(am, "list_asana_project_options", lambda: {"projects": [
        {"gid": "p1", "name": "Kory NON-IFG"}, {"gid": "p2", "name": "IFG Tasks"},
    ]})
    monkeypatch.setattr(am, "list_asana_tasks", am.list_asana_tasks)

    def fake_single(*, bucket, limit, project_gid, project_name, mine_only=True):
        return {"tasks": [{"gid": project_gid, "name": f"task in {project_name}",
                           "due_on": "2020-01-01"}]}

    monkeypatch.setattr(am, "list_asana_tasks", fake_single)
    out = am._list_tasks_across_projects(bucket="overdue", limit=10)
    assert out["total_found"] == 2, out
    assert {t["name"] for t in out["tasks"]} == {
        "task in Kory NON-IFG", "task in IFG Tasks"}


def test_only_korys_tasks_are_reported_by_default(monkeypatch):
    """Of 29 overdue tasks across shared boards, 18 belonged to Jason, Anju or
    Heidi. Reporting those as Kory's overdue list hands him other people's work."""
    import app.integrations.asana_manager as am

    monkeypatch.setattr(am, "_should_simulate_asana", lambda: False)
    monkeypatch.setattr(am, "execute_asana_tool", lambda slug, args: {"data": {"data": [
        {"gid": "1", "name": "kory item", "due_on": "2020-01-01",
         "completed": False, "assignee": {"name": "Kory Mitchell"}},
        {"gid": "2", "name": "jason item", "due_on": "2020-01-01",
         "completed": False, "assignee": {"name": "Jason Quesada"}},
        {"gid": "3", "name": "orphan item", "due_on": "2020-01-01",
         "completed": False, "assignee": None},
    ]}})

    shared = am.list_asana_tasks(bucket="overdue", project_gid="shared-proj")
    assert [t["name"] for t in shared["tasks"]] == ["kory item"], shared["tasks"]

    everyone = am.list_asana_tasks(
        bucket="overdue", project_gid="shared-proj", mine_only=False
    )
    assert len(everyone["tasks"]) == 3


def test_personal_project_counts_as_korys(monkeypatch):
    """His own board is all his, assigned or not."""
    import app.integrations.asana_manager as am

    monkeypatch.setattr(
        am, "settings", type("S", (), {"asana_project_gid": "personal", "asana_section_gid": ""})()
    )
    assert am.is_korys_task({"assignee": None}, project_gid="personal") is True
    assert am.is_korys_task({"assignee": None}, project_gid="shared") is False
    assert am.is_korys_task({"assignee": "Kory Mitchell"}, project_gid="shared") is True


def test_completed_bucket_returns_finished_work():
    """"Show me my completed tasks" had no bucket to answer from — every view
    filtered to incomplete, so finished work was unreachable."""
    from app.integrations.asana_manager import _filter_tasks_by_bucket

    tasks = [
        {"name": "done", "completed": True, "due_on": "2026-08-01"},
        {"name": "open", "completed": False, "due_on": "2026-08-01"},
    ]
    assert [t["name"] for t in _filter_tasks_by_bucket(tasks, bucket="completed")] == ["done"]
    assert [t["name"] for t in _filter_tasks_by_bucket(tasks, bucket="all")] == ["open"]


def test_create_reports_the_board_it_actually_used(monkeypatch):
    """A task filed under Personal was announced as "Reservation Reminders"
    because the response returned a constant instead of the real section."""
    import app.integrations.asana_manager as am

    monkeypatch.setattr(am, "_should_simulate_asana", lambda: False)
    monkeypatch.setattr(
        am, "settings", type("S", (), {"asana_project_gid": "p1", "asana_section_gid": ""})()
    )
    monkeypatch.setattr(am, "resolve_section_gid", lambda section, project_gid="": "sec-personal")
    monkeypatch.setattr(am, "list_project_sections", lambda project_gid="": [
        {"gid": "sec-personal", "name": "Personal"},
        {"gid": "sec-resv", "name": "Reservation Reminders"},
    ])
    monkeypatch.setattr(am, "execute_asana_tool", lambda slug, args: (
        {"data": {"data": {"gid": "9"}}} if slug == "ASANA_CREATE_A_TASK" else {"data": {}}
    ))

    result = am._create_asana_task(title="T", notes="N", section="Personal")
    assert result["board"] == "Personal", result


def test_unresolvable_task_name_explains_itself(monkeypatch):
    """An unresolved name reached Asana verbatim and returned
    "task: Not a Long: <name>", which got relayed as "commenting isn't
    supported". Say which task is meant instead."""
    import app.integrations.asana_manager as am

    monkeypatch.setattr(am, "_should_simulate_asana", lambda: False)

    monkeypatch.setattr(am, "search_asana_tasks", lambda *, query, limit=5, mine_only=True: {"tasks": []})
    gid, err = am.resolve_task_or_error("does-not-exist")
    assert gid == "" and err and "No task found" in err["error"]

    monkeypatch.setattr(am, "search_asana_tasks", lambda *, query, limit=5, mine_only=True: {"tasks": [
        {"gid": "1", "name": "book flights"}, {"gid": "2", "name": "book hotel"},
    ]})
    gid, err = am.resolve_task_or_error("book")
    assert gid == ""
    assert "matches 2 tasks" in err["error"], err
    assert len(err["candidates"]) == 2


def test_writes_refuse_an_ambiguous_name(monkeypatch):
    import app.integrations.asana_manager as am

    monkeypatch.setattr(am, "_should_simulate_asana", lambda: False)
    monkeypatch.setattr(am, "search_asana_tasks", lambda *, query, limit=5, mine_only=True: {"tasks": [
        {"gid": "1", "name": "book flights"}, {"gid": "2", "name": "book hotel"},
    ]})
    called = []
    monkeypatch.setattr(am, "execute_asana_tool", lambda s, a: called.append(s))

    out = am.delete_asana_task(task_gid="book", approved=True)
    assert out["ok"] is False and not called, "must not call Asana with an ambiguous name"


def _other_task(monkeypatch):
    import app.integrations.asana_manager as am

    monkeypatch.setattr(am, "_should_simulate_asana", lambda: False)
    monkeypatch.setattr(am, "search_asana_tasks", lambda *, query, limit=6, mine_only=True: {
        "tasks": [{"gid": "77", "name": "Candid Conversation Video",
                   "assignee": "Jason Quesada", "project": "Marketing Content Calendar"}]
    })
    return am


def test_touching_someone_elses_task_asks_first(monkeypatch):
    """Kory may legitimately close a team task — but never silently."""
    am = _other_task(monkeypatch)
    called = []
    monkeypatch.setattr(am, "execute_asana_tool", lambda s, a: called.append(s))

    out = am.complete_asana_task(task_gid="Candid Conversation Video", approved=True)
    assert out["ok"] is False
    assert out["error_code"] == "owner_confirmation_required"
    assert "Jason Quesada" in out["error"]
    assert not called, "must not reach Asana before Kory acknowledges the owner"


def test_owner_ack_lets_it_through(monkeypatch):
    am = _other_task(monkeypatch)
    monkeypatch.setattr(am, "execute_asana_tool", lambda s, a: {"data": {}})

    out = am.complete_asana_task(
        task_gid="Candid Conversation Video", approved=True, owner_ack=True
    )
    assert out["ok"] is True, out


def test_korys_own_task_needs_no_owner_ack(monkeypatch):
    import app.integrations.asana_manager as am

    monkeypatch.setattr(am, "_should_simulate_asana", lambda: False)
    monkeypatch.setattr(am, "search_asana_tasks", lambda *, query, limit=6, mine_only=True: {
        "tasks": [{"gid": "5", "name": "buy cigars", "assignee": "Kory Mitchell"}]
    })
    monkeypatch.setattr(am, "execute_asana_tool", lambda s, a: {"data": {}})
    assert am.complete_asana_task(task_gid="buy cigars", approved=True)["ok"] is True
