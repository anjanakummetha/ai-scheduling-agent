"""Drive Lexi exactly the way the Teams gateway does.

Anjana's requirement: "if I do the action in Teams it will be the exact same
result as your tests — I don't want any discrepancy."

A test that calls `handle_teams_command(...)` directly does NOT give that. The
gateway reaches Lexi through the MCP tool manager, which adds two things a
direct call skips:

  * **registration** — an unregistered tool is invisible to the model, so the
    guard inside it can never run ("a guard the model never reaches is not a
    guard"), and
  * **argument validation** — FastMCP builds a schema from the signature and
    coerces what the model sends. A model passing "1" where the tool wants an
    int either works or fails *here*, not in the function body.

So this helper goes through `mcp._tool_manager.call_tool`, the same entry point
the gateway uses. If a test here passes, the same words typed in Teams take the
identical path; the only thing left uncovered is which tool the model chooses to
call, which is what tests/test_scheduling_tool_contract.py pins.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any


def registered_tool_names() -> set[str]:
    """Every tool the Teams model can actually see."""
    import hermes_mcp_server as server

    return {tool.name for tool in server.mcp._tool_manager.list_tools()}


def call_tool(name: str, **arguments: Any) -> Any:
    """Invoke a Lexi MCP tool through the gateway's own path.

    Raises if the tool is not registered — that is the failure we most want
    surfaced, because to the model it is indistinguishable from the tool
    silently doing nothing.
    """
    import hermes_mcp_server as server

    if name not in registered_tool_names():
        raise AssertionError(
            f"{name!r} is not registered, so the Teams model cannot call it. "
            "Nothing inside it will ever run in production."
        )
    return asyncio.run(server.mcp._tool_manager.call_tool(name, arguments))


def teams(text: str, *, authorized_by: str = "kory") -> dict[str, Any]:
    """Type `text` into Teams and get Lexi's parsed reply.

    Everything Kory types — "pending", "approve draft 1", "reject draft 2 —
    not a fit" — routes through lexi_handle_teams_command, so this is the one
    door for the whole approval surface.
    """
    raw = call_tool("lexi_handle_teams_command", text=text, authorized_by=authorized_by)
    return _as_dict(raw)


def _as_dict(raw: Any) -> dict[str, Any]:
    """Tool results come back as a JSON string (sometimes wrapped by FastMCP)."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (list, tuple)) and raw:
        raw = raw[0]
    text = getattr(raw, "text", raw)
    if isinstance(text, (bytes, bytearray)):
        text = text.decode()
    if isinstance(text, str):
        try:
            return json.loads(text)
        except ValueError:
            return {"message": text}
    if isinstance(text, dict):
        return text
    return {"message": str(text)}


def message_of(result: dict[str, Any]) -> str:
    """The text Kory actually reads in the chat window."""
    for key in ("message", "text", "summary", "note"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return json.dumps(result, default=str)
