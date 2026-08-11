"""Smoke test for local MCP stdio integration.

This script uses the official MCP Python client to validate:
  - get_pending_decisions
  - approve_decision

Usage:
  python3 scripts/test_mcp_tools.py
  python3 scripts/test_mcp_tools.py --decision-id 12
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

class McpSmokeTestError(RuntimeError):
    """Raised when MCP wire format or response shape is invalid."""


# Deliberately out of range of any real proposal id, so approve_decision returns a
# well-formed "not found" envelope instead of acting on someone's actual meeting.
_NO_SUCH_PROPOSAL_ID = 999999999


def _extract_tool_text(content: list[Any]) -> str:
    for item in content:
        item_type = getattr(item, "type", None)
        text_value = getattr(item, "text", None)
        if item_type == "text" and text_value is not None:
            return str(text_value)
    raise McpSmokeTestError("Tool result did not include text payload.")


def _parse_backend_envelope(text_payload: str) -> dict[str, Any]:
    try:
        envelope = json.loads(text_payload)
    except json.JSONDecodeError as exc:
        raise McpSmokeTestError(f"Tool payload is not valid JSON: {text_payload}") from exc

    if "ok" not in envelope:
        raise McpSmokeTestError(f"Tool payload missing `ok`: {envelope}")
    return envelope


async def _run_smoke_test(decision_id: int | None) -> int:
    repo_root = Path(__file__).resolve().parents[1]
    server_script = repo_root / "hermes_mcp_server.py"
    if not server_script.exists():
        raise FileNotFoundError(f"Missing MCP server script: {server_script}")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root)
    env.setdefault("LEXI_EMBED_WORKER", "false")
    # The server inherits this env and calls load_dotenv(override=False), so these
    # win over the developer's .env. Without them a smoke test run on a machine
    # with LEXI_TEAMS_ENABLED=true posts into the real Teams chat.
    env["LEXI_TEAMS_ENABLED"] = "false"
    env["LEXI_SUPPRESS_TEAMS_PUSH"] = "true"

    server_params = StdioServerParameters(
        command=sys.executable,
        args=[str(server_script)],
        env=env,
    )

    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            print("[ok] initialize")

            tools_result = await session.list_tools()
            tool_names = {tool.name for tool in tools_result.tools}
            print(f"[ok] tools/list -> {sorted(tool_names)}")
            for required in (
                "get_pending_decisions",
                "approve_decision",
                "lexi_get_system_status",
                "lexi_get_calendar_availability",
                "lexi_get_inbound_reply_queue",
                "lexi_begin_draft_reply",
                "lexi_decline_inbound_reply",
                "lexi_execute_outlook_action",
                "lexi_accept_calendar_invite",
                "lexi_decline_calendar_invite",
                "lexi_find_meeting_times",
                "lexi_get_thread_context",
                "lexi_remember_kory_fact",
                "lexi_list_kory_memory",
                "lexi_web_search",
                "lexi_search_flights",
                "lexi_get_family_calendar_status",
            ):
                if required not in tool_names:
                    raise McpSmokeTestError(f"Required tool missing: {required}")

            status_result = await session.call_tool("lexi_get_system_status", {})
            status_envelope = _parse_backend_envelope(_extract_tool_text(status_result.content))
            print(f"[ok] lexi_get_system_status -> {status_envelope}")

            pending_result = await session.call_tool("get_pending_decisions", {})
            pending_envelope = _parse_backend_envelope(_extract_tool_text(pending_result.content))
            print(f"[ok] get_pending_decisions -> {pending_envelope}")

            # Never auto-target a live proposal. This is a wire-format smoke test;
            # approving a real one sends a real email and books a real slot. It was
            # safe only by accident — the envelope's key is "queue", not "decisions",
            # so the old lookup always missed and fell through to the sentinel.
            # Renaming that key would silently have started approving the first
            # pending proposal, and approve_decision now actually executes (it used
            # to return an un-awaited coroutine and do nothing). A nonexistent id
            # exercises the same tool and validates the same envelope shape.
            # Pass --decision-id to target a real one deliberately.
            target_decision = decision_id if decision_id is not None else _NO_SUCH_PROPOSAL_ID

            approve_result = await session.call_tool(
                "approve_decision",
                {"decision_id": str(target_decision)},
            )
            approve_envelope = _parse_backend_envelope(_extract_tool_text(approve_result.content))
            print(f"[ok] approve_decision({target_decision}) -> {approve_envelope}")
            print("[pass] MCP smoke test completed.")
            return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke test MCP tools over stdio.")
    parser.add_argument(
        "--decision-id",
        type=int,
        default=None,
        help="Optional pending decision ID for approve_decision test.",
    )
    args = parser.parse_args()
    return asyncio.run(_run_smoke_test(args.decision_id))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[fail] {type(exc).__name__}: {exc}")
        raise SystemExit(1)
