"""The model-facing scheduling contract — what the Teams model actually reads.

'A guard that the model never reaches is not a guard' (handoff §0), and the
tool description IS control flow: local suites kept passing while Teams broke
because the model was steered around the validated paths. These tests pin the
three legs of Teams parity that live in THIS repo:

1. every scheduling-critical tool is registered (an unregistered tool is a
   silent no-op to the model),
2. the steering docstrings keep their load-bearing phrases,
3. deploy/SOUL.md keeps the scheduling-honesty section.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SERVER_SRC = (REPO / "hermes_mcp_server.py").read_text()
SOUL = (REPO / "deploy" / "SOUL.md").read_text()


def _tool_docstrings() -> dict[str, str]:
    """name -> docstring for every @_tool/@_campaign_tool def in the server."""
    tree = ast.parse(SERVER_SRC)
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = ast.get_docstring(node) or ""
    return out


DOCS = {name: re.sub(r"\s+", " ", doc) for name, doc in _tool_docstrings().items()}

SCHEDULING_TOOLS = [
    "lexi_retry_scheduling",
    "lexi_update_proposal_draft",
    "lexi_escalate_to_kory",
    "lexi_begin_reoffer",
    "lexi_find_slots",
    "lexi_start_scheduling",
    "lexi_get_scheduling_context",
    "lexi_recipient_timezone",
    "lexi_validate_slots",
    "lexi_check_time_slot",
    "approve_decision",
    "modify_and_approve_decision",
    "reject_decision",
    "lexi_remember_kory_fact",
    "lexi_list_kory_memory",
]


def test_every_scheduling_tool_is_defined():
    for name in SCHEDULING_TOOLS:
        assert name in DOCS, f"{name} missing from hermes_mcp_server.py"


def test_scheduling_tools_are_registered_not_orphaned():
    # Each def must sit under a registering decorator — a bare def next to the
    # others reads identically in a diff and silently never reaches the model.
    tree = ast.parse(SERVER_SRC)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in SCHEDULING_TOOLS:
            decorators = [ast.dump(d) for d in node.decorator_list]
            assert decorators, f"{node.name} has no decorator — unregistered"
            assert any("_tool" in d or "tool" in d for d in decorators), (
                f"{node.name} decorators do not register it: {decorators}"
            )


def test_retry_scheduling_docstring_steers_the_model():
    doc = DOCS["lexi_retry_scheduling"]
    for phrase in (
        "THE tool",                      # primacy for guidance replies
        "IMMEDIATELY",                   # no improvising first
        "Kory's own words",              # pass guidance verbatim
        "NEVER compose the email yourself",  # the RUN-15 hand-composed-draft class
        "rejected",                      # rejected proposals are valid input (redo path)
        "engine is the authority",
    ):
        assert phrase in doc, f"lexi_retry_scheduling docstring lost: {phrase!r}"


def test_update_draft_docstring_steers_the_model():
    doc = DOCS["lexi_update_proposal_draft"]
    for phrase in (
        "VALIDATING",
        "LIVE calendar",
        "REFUSED with the clash named",
        "ONLY safe way",
        "do not work around it",
    ):
        assert phrase in doc, f"lexi_update_proposal_draft docstring lost: {phrase!r}"


def test_find_slots_is_mandatory_for_chat_times():
    assert "MANDATORY" in DOCS["lexi_find_slots"]
    assert "never guess" in DOCS["lexi_find_slots"]


def test_start_scheduling_forbids_handpicked_times():
    doc = DOCS["lexi_start_scheduling"]
    assert "THE tool" in doc
    assert "Do NOT hand-pick times" in doc


def test_escalation_targets_kory_only():
    doc = DOCS["lexi_escalate_to_kory"]
    assert "ONLY escalation target" in doc


def test_remember_docstring_demands_verbatim_words():
    doc = DOCS["lexi_remember_kory_fact"]
    assert "verbatim" in doc.lower()
    assert "ENFORCED" in doc


def test_soul_keeps_the_scheduling_honesty_section():
    assert "times are tool output" in SOUL
    for phrase in (
        "Never state, offer, or write a meeting time that did not come from a tool",
        "lexi_retry_scheduling",
        "lexi_update_proposal_draft",
        "never say holds are placed unless the tool result lists them",
    ):
        assert re.sub(r"\s+", " ", phrase) in re.sub(r"\s+", " ", SOUL), (
            f"deploy/SOUL.md lost: {phrase!r}"
        )


def test_soul_dates_rule_present():
    # The model's internal date sense is wrong on the box; every relative-date
    # answer must go through lexi_today.
    assert "lexi_today" in SOUL
