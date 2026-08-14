"""The create-task tool has to be reached before its guards can run.

Every guard inside create_asana_task_from_chat — the duplicate check, the
project question, the owner ask — is dead code if the model answers Kory
itself instead of calling the tool. That is exactly what happened: the
description said project and due_on were "REQUIRED before anything is
written", the model read that as "collect them first", and asked three
questions about a task he already had open. The tool was never called, so no
test in the suite could see it.

These assert the contract the description makes with the model. They are
coarse on purpose — they cannot check that the model obeys, only that the
instruction to call first is still there and has not been edited back into
"gather the details, then call".
"""

from __future__ import annotations

import hermes_mcp_server as s


def _create_task_description() -> str:
    tools = {t.name: t for t in s.mcp._tool_manager.list_tools()}
    tool = tools.get("lexi_create_asana_task")
    assert tool is not None, "lexi_create_asana_task is not registered"
    return (tool.description or "").lower()


def test_description_tells_the_model_to_call_before_asking():
    text = _create_task_description()
    assert "call this first" in text, (
        "the description must tell the model to call the tool before asking Kory "
        "about the board or due date — otherwise the duplicate check never runs"
    )
    assert "do not ask him" in text


def test_description_explains_the_duplicate_return():
    """A guard the model cannot interpret is a guard that gets talked over."""
    text = _create_task_description()
    assert "possible_duplicate" in text
    assert "allow_duplicate" in text


def test_description_still_forbids_choosing_the_board():
    """The original fix — never file silently into a default — must survive."""
    text = _create_task_description()
    assert "do not choose for him" in text
    assert "project_required" in text
    assert "task_details_required" in text
