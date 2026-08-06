"""MCP tools must not run on the event loop.

mcp.server.fastmcp calls sync tools inline — func_metadata's
call_fn_with_arg_validation does `return fn(...)` with no thread offload. Every
tool in hermes_mcp_server does blocking work (Composio HTTP, LLM calls, SQLite),
so a sync tool freezes the whole server for its duration: other tool calls queue
behind it and the keepalive stops answering.

Observed in production as eight 120s tool timeouts over seven days, each
followed by "keepalive failed, triggering reconnect".
"""

from __future__ import annotations

import asyncio
import time

import pytest


def test_every_registered_tool_is_async():
    """A sync tool would be called inline on the loop and reintroduce the hang."""
    import hermes_mcp_server as s

    tools = s.mcp._tool_manager.list_tools()
    assert tools, "no tools registered"
    sync_tools = [t.name for t in tools if not t.is_async]
    assert not sync_tools, f"these would block the event loop: {sync_tools}"


def test_tool_schemas_come_from_the_original_signature():
    """functools.wraps must preserve the signature FastMCP builds the schema from,
    otherwise every tool would advertise (*args, **kwargs)."""
    import hermes_mcp_server as s

    tools = {t.name: t for t in s.mcp._tool_manager.list_tools()}
    params = tools["lexi_begin_draft_reply"].parameters["properties"]
    assert "proposal_id" in params
    assert "voice_mode" in params
    assert "args" not in params and "kwargs" not in params


def test_a_slow_tool_does_not_stall_the_event_loop():
    """The property that matters: while a tool blocks, the loop keeps running."""
    import hermes_mcp_server as s

    ticks = 0

    @s._tool
    def slow_probe(marker: str = "x") -> str:
        """Deliberately blocking, like a real Composio call."""
        time.sleep(0.5)
        return f"done:{marker}"

    async def scenario():
        nonlocal ticks

        async def heartbeat():
            nonlocal ticks
            for _ in range(20):
                await asyncio.sleep(0.05)
                ticks += 1

        beat = asyncio.create_task(heartbeat())
        result = await s.mcp._tool_manager.call_tool("slow_probe", {"marker": "ok"})
        beat.cancel()
        return result

    result = asyncio.run(scenario())

    assert "done:ok" in str(result)
    # Were the tool run inline, the loop would have been frozen for the whole
    # 0.5s and the heartbeat could not have advanced.
    assert ticks >= 3, f"event loop was blocked during the tool call (ticks={ticks})"


def test_tools_run_off_the_main_thread():
    """Concrete form of the same property: the body executes on a worker thread."""
    import threading

    import hermes_mcp_server as s

    seen: dict[str, int] = {}

    @s._tool
    def thread_probe() -> str:
        """Records which thread the body ran on."""
        seen["tool_thread"] = threading.get_ident()
        return "ok"

    async def scenario():
        seen["loop_thread"] = threading.get_ident()
        return await s.mcp._tool_manager.call_tool("thread_probe", {})

    asyncio.run(scenario())

    assert seen["tool_thread"] != seen["loop_thread"], (
        "tool body ran on the event loop thread — it would block the server"
    )
