"""Teams renders message text as markdown, where a lone \n collapses to a
space — every multi-line message reached Kory as one run-on line. These tests
lock the normalization applied at the two boundaries (proactive pushes and MCP
tool results) and the execution-backed approve confirmation."""

from __future__ import annotations

import json
from unittest.mock import patch

from app.bot.teams_format import teams_markdown_breaks


class TestTeamsMarkdownBreaks:
    def test_lone_newlines_are_doubled(self):
        text = "**Coffee chat**\nFrom Anjana Kummetha\nTimes offered"
        assert teams_markdown_breaks(text) == (
            "**Coffee chat**\n\nFrom Anjana Kummetha\n\nTimes offered"
        )

    def test_existing_blank_lines_untouched(self):
        text = "Header\n\nBody line"
        assert teams_markdown_breaks(text) == "Header\n\nBody line"

    def test_mixed_lone_and_blank(self):
        text = "A\nB\n\nC\nD"
        assert teams_markdown_breaks(text) == "A\n\nB\n\nC\n\nD"

    def test_empty_and_none_safe(self):
        assert teams_markdown_breaks("") == ""
        assert teams_markdown_breaks(None) == ""  # type: ignore[arg-type]

    def test_single_line_unchanged(self):
        assert teams_markdown_breaks("No drafts waiting to send.") == (
            "No drafts waiting to send."
        )


class TestMcpResultNormalization:
    def test_ok_normalizes_message_fields(self):
        import hermes_mcp_server as mcp_mod

        raw = mcp_mod._ok(
            {
                "message": "Line one\nLine two",
                "result": {"kory_message": "A\nB", "data": "keep\nas-is"},
            }
        )
        payload = json.loads(raw)
        assert payload["message"] == "Line one\n\nLine two"
        assert payload["result"]["kory_message"] == "A\n\nB"
        # Non-chat fields are never rewritten.
        assert payload["result"]["data"] == "keep\nas-is"

    def test_error_normalizes_message(self):
        import hermes_mcp_server as mcp_mod

        payload = json.loads(mcp_mod._error("Bad\nthing"))
        assert payload["message"] == "Bad\n\nthing"

    def test_teams_command_tool_normalizes_message(self):
        import hermes_mcp_server as mcp_mod

        with patch(
            "app.teams.commands.handle_teams_command",
            return_value={"ok": True, "handled": True, "message": "One\nTwo"},
        ):
            payload = json.loads(mcp_mod.lexi_handle_teams_command("pending"))
        assert payload["message"] == "One\n\nTwo"


class TestProactivePushNormalization:
    def test_push_text_normalized_before_send(self, capsys):
        import asyncio

        from app.bot import teams_publisher

        # Without Teams credentials the push prints instead of sending — the
        # printed text must already carry the doubled line breaks.
        with patch.object(
            teams_publisher, "_teams_credentials_configured", return_value=False
        ):
            asyncio.run(
                teams_publisher.push_approval_text_to_teams(
                    "**Subject**\nFrom someone", proposal_id=1
                )
            )
        out = capsys.readouterr().out
        assert "**Subject**\n\nFrom someone" in out


class TestApproveConfirmationIsExecutionBacked:
    """The Teams reply to `approve #N` must restate what actually happened —
    never claim a send that the executor did not perform."""

    def _item(self):
        class _Item:
            proposal_id = 42
            holds: list = []
            proposed_slots = [{"start": "2026-08-10T16:00:00Z"}]

        return _Item()

    def test_failed_execution_never_says_sent(self):
        from app.teams import commands

        class _Result:
            ok = False
            errors = ["outbound blocked by safety gate"]

            def to_dict(self):
                return {"ok": False, "errors": self.errors}

        with (
            patch.object(commands, "find_pending_item", return_value=self._item()),
            patch.object(
                commands,
                "_fetch_bundle",
                return_value={"subject": "Intro call", "sender": "a@b.com"},
            ),
            patch.object(
                commands, "execute_lexi_approval", return_value=_Result()
            ),
        ):
            out = commands.handle_teams_command("approve #42")
        assert out["ok"] is False
        assert "outbound blocked by safety gate" in out["message"]
        assert "Sent reply" not in out["message"]

    def test_successful_execution_reports_sent(self):
        from app.teams import commands

        class _Result:
            ok = True
            errors: list[str] = []

            def to_dict(self):
                return {"ok": True, "errors": []}

        with (
            patch.object(commands, "find_pending_item", return_value=self._item()),
            patch.object(
                commands,
                "_fetch_bundle",
                return_value={"subject": "Intro call", "sender": "a@b.com"},
            ),
            patch.object(
                commands, "execute_lexi_approval", return_value=_Result()
            ),
        ):
            out = commands.handle_teams_command("approve #42")
        assert out["ok"] is True
        assert "Sent reply" in out["message"]
