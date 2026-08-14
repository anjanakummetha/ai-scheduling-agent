"""Kory can file tasks on ANY Asana project, not just Kory NON-IFG.

The old behavior hardcoded settings.asana_project_gid into every create, so
"add a task to IFG Tasks" silently landed on his personal project.
"""

from __future__ import annotations

import pytest

import app.integrations.asana_manager as am

PROJECTS = [
    {"gid": "p-home", "name": "Kory NON-IFG"},
    {"gid": "p-anju", "name": "Anju - CEO executive tools"},
    {"gid": "p-ifg", "name": "IFG Tasks"},
    {"gid": "p-mkt", "name": "Marketing Content Calendar"},
]


@pytest.fixture
def wired(monkeypatch):
    calls: list[tuple[str, dict]] = []

    def fake_tool(slug, args):
        calls.append((slug, args))
        if slug == "ASANA_CREATE_A_TASK":
            return {"data": {"data": {"gid": "900"}}}
        return {"data": {}}

    monkeypatch.setattr(am, "execute_asana_tool", fake_tool)
    monkeypatch.setattr(am, "_should_simulate_asana", lambda: False)
    monkeypatch.setattr(am, "list_asana_project_options", lambda: {"projects": PROJECTS})
    monkeypatch.setattr(
        am,
        "settings",
        type("S", (), {"asana_project_gid": "p-home", "asana_section_gid": "sec-home-default"})(),
    )
    return calls


def test_resolve_project_by_name_case_insensitive(monkeypatch):
    monkeypatch.setattr(am, "list_asana_project_options", lambda: {"projects": PROJECTS})
    assert am.resolve_project_gid("IFG Tasks") == ("p-ifg", "IFG Tasks")
    assert am.resolve_project_gid("ifg tasks") == ("p-ifg", "IFG Tasks")
    assert am.resolve_project_gid("anju") == ("p-anju", "Anju - CEO executive tools")
    assert am.resolve_project_gid("p-anju") == ("", "")  # not a digit gid, not a name
    assert am.resolve_project_gid("") == ("", "")


def test_resolve_project_refuses_ambiguity(monkeypatch):
    monkeypatch.setattr(am, "list_asana_project_options", lambda: {"projects": [
        {"gid": "1", "name": "Marketing Content Calendar"},
        {"gid": "2", "name": "Marketing Experiments"},
    ]})
    assert am.resolve_project_gid("marketing") == ("", "")


def test_create_targets_the_named_project(wired):
    out = am.create_asana_task_from_chat(
        title="T", project="Anju - CEO executive tools",
        section="General", due_on="no due date", approved=True,
    )
    assert out["ok"] is True, out
    assert out["project"] == "Anju - CEO executive tools"
    create = next(a for s, a in wired if s == "ASANA_CREATE_A_TASK")
    assert create["data"]["projects"] == ["p-anju"]


def test_create_on_other_project_skips_home_default_section(wired):
    """The default section gid belongs to the personal project — applying it to
    a task on another project would silently drag the task back home."""
    am.create_asana_task_from_chat(
        title="T", project="IFG Tasks", section="General",
        due_on="no due date", approved=True,
    )
    assert not any(s == "ASANA_ADD_TASK_TO_SECTION" for s, _ in wired)


def test_create_without_a_project_asks_instead_of_choosing(wired):
    """Kory keeps eight projects. Filing silently into the default put the task
    somewhere he wasn't looking while Lexi reported it done."""
    out = am.create_asana_task_from_chat(title="T", approved=True)
    assert out["ok"] is False
    assert out["error_code"] == "project_required"
    assert out["projects"], "the question has to name the options"
    assert not any(s == "ASANA_CREATE_A_TASK" for s, _ in wired), "nothing written"


def test_saying_default_still_uses_the_home_project(wired, monkeypatch):
    """The escape hatch: he can decline to choose, but he has to say so.

    General, not Reservation Reminders — that board is Lexi's automated booking
    surface, and a task Kory asked for landing there is a task he never sees.
    """
    monkeypatch.setattr(
        am, "list_project_sections",
        lambda project_gid="": [
            {"gid": "sec-general", "name": "General"},
            {"gid": "sec-home-default", "name": "Reservation Reminders"},
        ],
    )
    out = am.create_asana_task_from_chat(
        title="T", project="default", section="General",
        due_on="no due date", approved=True,
    )
    assert out["ok"] is True
    create = next(a for s, a in wired if s == "ASANA_CREATE_A_TASK")
    assert create["data"]["projects"] == ["p-home"]
    placed = next(a for s, a in wired if s == "ASANA_ADD_TASK_TO_SECTION")
    assert placed["section_gid"] == "sec-general"
    assert out["project"] == am.ASANA_PARENT_PROJECT_NAME


def test_unknown_project_errors_and_never_reaches_asana(wired):
    out = am.create_asana_task_from_chat(title="T", project="Nonexistent", approved=True)
    assert out["ok"] is False
    assert "Available projects" in out["error"]
    assert "Anju - CEO executive tools" in out["error"]
    assert not wired, "must not call Asana for an unknown project"


def test_explicit_section_resolves_in_target_project(wired, monkeypatch):
    seen = {}

    def fake_resolve(section, project_gid=""):
        seen["project_gid"] = project_gid
        return "sec-anju-1"

    monkeypatch.setattr(am, "resolve_section_gid", fake_resolve)
    am.create_asana_task_from_chat(
        title="T", section="Backlog", project="Anju - CEO executive tools",
        due_on="no due date", approved=True,
    )
    assert seen["project_gid"] == "p-anju"
    placed = next(a for s, a in wired if s == "ASANA_ADD_TASK_TO_SECTION")
    assert placed["section_gid"] == "sec-anju-1"


def test_move_resolves_section_within_named_project(wired, monkeypatch):
    monkeypatch.setattr(am, "resolve_task_or_error", lambda t, owner_ack=False: ("42", None))
    monkeypatch.setattr(
        am,
        "resolve_section_gid",
        lambda section, project_gid="": "sec-anju-2" if project_gid == "p-anju" else "",
    )
    out = am.move_asana_task_to_section(
        task_gid="42",
        section_name="Done",
        project="Anju - CEO executive tools",
        approved=True,
    )
    assert out["ok"] is True, out
    assert out["section_gid"] == "sec-anju-2"


def test_project_write_still_passes_the_approval_gate(wired, monkeypatch):
    gate_calls = []
    import app.safety.approval_gate as gate

    def fake_gate(*, approved, action):
        gate_calls.append((approved, action))
        raise PermissionError("blocked")

    monkeypatch.setattr(gate, "assert_kory_approved_write", fake_gate)
    with pytest.raises(PermissionError):
        am.create_asana_task_from_chat(title="T", project="IFG Tasks", approved=False)
    assert gate_calls == [(False, "Asana create task")]
    assert not wired, "gate must fire before any Asana call"


def test_create_can_assign_in_one_step(wired, monkeypatch):
    """"create a task for Heidi" was two round trips; now it is one request."""
    seen = {}

    def fake_update(**kw):
        seen.update(kw)
        return {"ok": True, "assignee": "Heidi Heckler"}

    monkeypatch.setattr(am, "update_asana_task", fake_update)
    out = am.create_asana_task_from_chat(
        title="Send the deck", project="IFG Tasks", assignee="Heidi",
        section="General", due_on="no due date", approved=True,
    )
    assert out["ok"] is True
    assert seen["assignee"] == "Heidi", "the create must carry the assignment through"
    # Kory is told who it actually landed on, not just what he typed — the two
    # differ when a first name resolves to a full account.
    assert out["assignee"] == "Heidi Heckler"


def test_a_task_that_could_not_be_assigned_is_not_reported_as_assigned(wired, monkeypatch):
    """The task exists but is unowned — saying it is Heidi's would be a lie."""
    monkeypatch.setattr(
        am, "update_asana_task",
        lambda **kw: {"ok": False, "error": "no such user"} if kw.get("assignee") else {"ok": True},
    )
    out = am.create_asana_task_from_chat(
        title="Send the deck", project="IFG Tasks", assignee="Nobody",
        section="General", due_on="no due date", approved=True,
    )
    assert out["ok"] is False
    assert "assigning it to" in out["error"]
    assert "created" in out["error"], "must still say the task exists"


def _only_anju_has_this_week(monkeypatch):
    def fake_sections(project_gid=""):
        return [{"gid": "sec-week", "name": "📌 This Week"}] if project_gid == "p-anju" else [
            {"gid": "sec-gen", "name": "General"}
        ]

    monkeypatch.setattr(am, "resolve_task_or_error", lambda t, owner_ack=False: ("42", None))
    monkeypatch.setattr(am, "list_project_sections", fake_sections)


def test_a_board_is_looked_for_in_the_project_holding_the_task(wired, monkeypatch):
    """"move it to This Week" answered "no such board" — sections were resolved
    against the home project, not the project the task actually lives in."""
    _only_anju_has_this_week(monkeypatch)
    monkeypatch.setattr(am, "_task_project_gids", lambda gid: ["p-anju"])
    out = am.move_asana_task_to_section(task_gid="42", section_name="This Week", approved=True)
    assert out["ok"] is True
    assert out["section_gid"] == "sec-week"


def test_a_wrongly_named_project_does_not_hide_the_board(wired, monkeypatch):
    """Lexi guessed the personal project, so the board "did not exist in Asana".
    A wrong guess must not be the last word — widen the search."""
    _only_anju_has_this_week(monkeypatch)
    monkeypatch.setattr(am, "_task_project_gids", lambda gid: ["p-anju"])
    out = am.move_asana_task_to_section(
        task_gid="42", section_name="This Week", project="Kory NON-IFG", approved=True
    )
    assert out["ok"] is True
    assert out["section_gid"] == "sec-week"


def test_a_board_nobody_has_is_still_reported_missing(wired, monkeypatch):
    _only_anju_has_this_week(monkeypatch)
    monkeypatch.setattr(am, "_task_project_gids", lambda gid: ["p-anju"])
    out = am.move_asana_task_to_section(task_gid="42", section_name="Atlantis", approved=True)
    assert out["ok"] is False
    assert "Atlantis" in out["error"]


def test_an_emoji_prefixed_board_still_matches_what_kory_types(monkeypatch):
    monkeypatch.setattr(
        am, "list_project_sections",
        lambda project_gid="": [
            {"gid": "s1", "name": "📌 This Week"},
            {"gid": "s2", "name": "📋 Backlog"},
            {"gid": "s3", "name": "✅ Done"},
        ],
    )
    assert am.resolve_section_gid("This Week", "p-anju") == "s1"
    assert am.resolve_section_gid("backlog", "p-anju") == "s2"
    assert am.resolve_section_gid("done", "p-anju") == "s3"


def test_an_unknown_board_names_the_project_it_looked_in(wired, monkeypatch):
    monkeypatch.setattr(am, "resolve_task_or_error", lambda t, owner_ack=False: ("42", None))
    monkeypatch.setattr(am, "_task_project_gids", lambda gid: ["p-anju"])
    monkeypatch.setattr(
        am, "list_project_sections",
        lambda project_gid="": [{"gid": "s1", "name": "📌 This Week"}],
    )
    out = am.move_asana_task_to_section(task_gid="42", section_name="Nowhere", approved=True)
    assert out["ok"] is False
    assert "This Week" in out["error"], "say what boards exist"
    assert "Anju" in out["error"], "and which project was searched"


def _boards(monkeypatch, names=("General", "Personal", "YPO", "Reservation Reminders")):
    monkeypatch.setattr(
        am, "list_project_sections",
        lambda project_gid="": [{"gid": f"s{i}", "name": n} for i, n in enumerate(names)],
    )


def test_create_asks_for_board_and_due_date_before_writing(wired, monkeypatch):
    """An unspecified board fell through to Reservation Reminders — Lexi's
    automated booking board — and an undated task never reaches the overdue list."""
    _boards(monkeypatch)
    out = am.create_asana_task_from_chat(title="T", project="Kory NON-IFG", approved=True)
    assert out["ok"] is False
    assert out["error_code"] == "task_details_required"
    assert set(out["needs"]) == {"board", "due_on"}
    assert "General" in out["kory_message"]
    assert "overdue" in out["kory_message"], "say why the due date matters"
    assert not wired, "nothing written until he answers"


def test_only_the_missing_detail_is_asked_for(wired, monkeypatch):
    _boards(monkeypatch)
    out = am.create_asana_task_from_chat(
        title="T", project="Kory NON-IFG", section="General", approved=True
    )
    assert out["needs"] == ["due_on"]


def test_no_due_date_is_an_answer_not_a_gap(wired, monkeypatch):
    _boards(monkeypatch)
    out = am.create_asana_task_from_chat(
        title="T", project="Kory NON-IFG", section="General",
        due_on="no due date", approved=True,
    )
    assert out["ok"] is True
    assert "due_update" not in out, "declining a date must not set one"


def test_a_reservation_reminder_still_files_itself(monkeypatch):
    """The automated booking path owns that board and must not start asking."""
    created = {}
    monkeypatch.setattr(am, "_should_simulate_asana", lambda: True)
    monkeypatch.setattr(am, "_create_asana_task", lambda **kw: created.update(kw) or {"ok": True})
    am.create_venue_reservation_task(
        meeting_subject="Dinner", time_slot="Thu 7pm", participants="x@y.com", approved=True
    )
    assert created["title"].startswith(am.VENUE_TASK_PREFIX)


NON_IFG_BOARDS = [
    "General", "Personal", "YPO", "Maclain Holdings", "Money Stuff", "Shows to Watch",
    "Rollups | Non-IFG", "Heidi/Kory", "Personal Travel", "NG Board", "Canopy",
    "Family", "Books to Read", "Reservation Reminders",
]


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Renew my YPO membership", "YPO"),
        ("Prep for the NG Board meeting", "NG Board"),
        ("Review the Maclain Holdings docs", "Maclain Holdings"),
        ("Buy the new Sanderson book", "Books to Read"),
        ("Sync with Heidi about the forum", "Heidi/Kory"),
        ("Book flights to Chicago for the Canopy board meeting", "Canopy"),
    ],
)
def test_an_obvious_board_is_proposed_rather_than_asked(title, expected):
    """"Renew my YPO membership" belongs on YPO. Making Kory pick it off a list of
    fourteen asks him to redo work he did by naming it."""
    assert am.suggest_board_for_task(title, NON_IFG_BOARDS) == expected


@pytest.mark.parametrize(
    "title",
    [
        "Call the insurance broker before the renewal",
        "Send Bruce the CFO intro",
        "Follow up on the thing from Tuesday",
    ],
)
def test_a_title_that_names_no_board_asks_openly(title):
    """A wrong suggestion accepted by reflex is worse than no suggestion."""
    assert am.suggest_board_for_task(title, NON_IFG_BOARDS) == ""


def test_two_equally_good_boards_produce_no_suggestion():
    assert am.suggest_board_for_task("Personal travel plans", ["Personal", "Personal Travel"]) == ""


def test_the_suggestion_reaches_the_question(wired, monkeypatch):
    monkeypatch.setattr(
        am, "list_project_sections",
        lambda project_gid="": [{"gid": f"s{i}", "name": n} for i, n in enumerate(NON_IFG_BOARDS)],
    )
    out = am.create_asana_task_from_chat(
        title="Renew my YPO membership", project="Kory NON-IFG", approved=True
    )
    assert out["suggested_board"] == "YPO"
    assert "YPO" in out["kory_message"]
    assert not wired, "still writes nothing until he confirms"
