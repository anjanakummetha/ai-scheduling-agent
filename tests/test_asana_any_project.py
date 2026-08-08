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
        title="T", project="Anju - CEO executive tools", approved=True
    )
    assert out["ok"] is True, out
    assert out["project"] == "Anju - CEO executive tools"
    create = next(a for s, a in wired if s == "ASANA_CREATE_A_TASK")
    assert create["data"]["projects"] == ["p-anju"]


def test_create_on_other_project_skips_home_default_section(wired):
    """The default section gid belongs to the personal project — applying it to
    a task on another project would silently drag the task back home."""
    am.create_asana_task_from_chat(title="T", project="IFG Tasks", approved=True)
    assert not any(s == "ASANA_ADD_TASK_TO_SECTION" for s, _ in wired)


def test_create_without_project_keeps_old_behavior(wired):
    out = am.create_asana_task_from_chat(title="T", approved=True)
    assert out["ok"] is True
    create = next(a for s, a in wired if s == "ASANA_CREATE_A_TASK")
    assert create["data"]["projects"] == ["p-home"]
    placed = next(a for s, a in wired if s == "ASANA_ADD_TASK_TO_SECTION")
    assert placed["section_gid"] == "sec-home-default"
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
        title="T", section="Backlog", project="Anju - CEO executive tools", approved=True
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
