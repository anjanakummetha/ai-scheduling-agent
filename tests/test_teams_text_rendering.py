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


class TestApprovalTextCarriesGateContext:
    """Cards showed the gate's scheduling_note in Attention color; the text-only
    path must carry the same caveat, the proposal id, and the text commands —
    or Kory approves a window expansion he was never told about."""

    def test_draft_text_includes_id_note_and_commands(self):
        from app.bot.teams_format import format_draft_ready_text

        text = format_draft_ready_text(
            subject="[TEST] Intro call — LT-C1",
            sender="anjana@example.com",
            draft="Hi Anjana,\n\nHere are a few times.\n\nThank you,\nLexi Knightly",
            slots=None,
            voice_mode="lexi",
            proposal_id=6450,
            scheduling_note=(
                "No availability for week of August 10 — offering week of "
                "August 17 instead."
            ),
        )
        assert "#6450" in text
        assert "⚠️ No availability for week of August 10" in text
        assert "approve #6450" in text
        assert "reject #6450" in text
        assert "tap **Send**" not in text
        assert "Teams card" not in text
        assert "card buttons" not in text.lower()

    def test_note_omitted_when_empty(self):
        from app.bot.teams_format import format_draft_ready_text

        text = format_draft_ready_text(
            subject="Intro",
            sender="a@b.com",
            draft="Hi",
            proposal_id=7,
            scheduling_note="",
        )
        assert "⚠️" not in text
        assert "approve #7" in text

    def test_queue_item_note_reaches_notification(self):
        from app.agents.comms_agent import LexiQueueItem
        from app.bot.teams_text import format_approval_notification

        item = LexiQueueItem(
            proposal_id=6450,
            thread_id="t1",
            subject="[TEST] Intro call — LT-C1",
            sender="anjana@example.com",
            raw_body="",
            intent_classification="referral_or_intro",
            priority_tier="medium",
            proposed_slots=[],
            drafted_reply="Hi Anjana,\n\nTimes below.\n\nThank you,\nLexi Knightly",
            confidence_score=0.9,
            justification=None,
            voice_mode="lexi",
            holds=[],
            approval_card={},
            scheduling_note="Offering week of August 17 instead.",
        )
        text = format_approval_notification(item)
        assert "#6450" in text
        assert "⚠️ Offering week of August 17 instead." in text

    def test_pending_list_shows_ids_and_text_commands(self):
        from app.bot.teams_text import format_pending_list

        class _Item:
            proposal_id = 6450
            subject = "Intro call"
            sender = "anjana@example.com"

        text = format_pending_list([_Item()])
        assert "#6450" in text
        assert "approve #N" in text
        assert "card buttons" not in text.lower()

    def test_pending_list_shows_invite_queue(self):
        """LT-D1: an accepted offer waiting on invite dispatch must show in
        `pending` — it was invisible, so a lost Teams push left it stranded."""
        from app.bot.teams_text import format_pending_list

        class _Draft:
            proposal_id = 6450
            subject = "Intro call"
            sender = "anjana@example.com"

        class _Invite:
            proposal_id = 6835
            subject = "[TEST] Intro chat next week? — LT-D1"
            sender = "anjana@example.com"

        text = format_pending_list([_Draft()], invite_items=[_Invite()])
        assert "#6835" in text
        assert "accepted" in text
        assert "approve #6835" in text
        assert "#6450" in text

        invites_only = format_pending_list([], invite_items=[_Invite()])
        assert "#6835" in invites_only
        assert "No drafts waiting" not in invites_only


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
        import asyncio

        import hermes_mcp_server as mcp_mod

        with patch(
            "app.teams.commands.handle_teams_command",
            return_value={"ok": True, "handled": True, "message": "One\nTwo"},
        ):
            # Tools are async now: their bodies run on a worker thread so a
            # blocking call cannot freeze the MCP event loop.
            payload = json.loads(
                asyncio.run(mcp_mod.lexi_handle_teams_command("pending"))
            )
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


class TestTypedApproveDispatchesInvite:
    """LT-D1: `approve #N` typed for a pending_invite proposal must send the
    calendar invite — it used to dead-end with 'No draft is pending approval'."""

    def _invite_item(self):
        class _Item:
            proposal_id = 6835
            subject = "[TEST] Intro chat next week? — LT-D1"
            sender = "anjana@example.com"

        return _Item()

    def test_approve_routes_to_invite_dispatch(self):
        from app.teams import commands

        class _Result:
            ok = True
            holds_released = 2
            errors: list = []

            def to_dict(self):
                return {"ok": True}

        calls = {}

        def fake_invite(proposal_id, selected_slot, authorized_by, **kwargs):
            calls["proposal_id"] = proposal_id
            calls["selected_slot"] = selected_slot
            return _Result()

        import app.agents.comms_agent as comms

        with (
            patch.object(commands, "find_pending_item", return_value=None),
            patch.object(commands, "_find_invite_item", return_value=self._invite_item()),
            patch.object(comms, "execute_lexi_invite", side_effect=fake_invite),
        ):
            out = commands.handle_teams_command("approve #6835")
        assert out["ok"] is True
        assert calls["proposal_id"] == 6835
        assert calls["selected_slot"] == ""  # stored recipient_selected_slot wins
        assert "invite sent" in out["message"].lower()
        assert "2 unused hold" in out["message"]

    def test_invite_failure_reports_and_blocks_nothing(self):
        from app.teams import commands

        class _Result:
            ok = False
            holds_released = 0
            errors = ["conflict at confirm"]

            def to_dict(self):
                return {"ok": False}

        import app.agents.comms_agent as comms

        with (
            patch.object(commands, "find_pending_item", return_value=None),
            patch.object(commands, "_find_invite_item", return_value=self._invite_item()),
            patch.object(comms, "execute_lexi_invite", return_value=_Result()),
        ):
            out = commands.handle_teams_command("approve #6835")
        assert out["ok"] is False
        assert "conflict at confirm" in out["message"]


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

    def test_exception_after_send_admits_the_send(self):
        """A crash AFTER the send (e.g. a DB lock during hold placement) must
        never be reported as 'could not execute' — that invites a re-approve
        and a duplicate email."""
        from app.teams import commands

        with (
            patch.object(commands, "find_pending_item", return_value=self._item()),
            patch.object(
                commands,
                "_fetch_bundle",
                return_value={"subject": "Intro call", "sender": "a@b.com"},
            ),
            patch.object(
                commands,
                "execute_lexi_approval",
                side_effect=RuntimeError("database is locked"),
            ),
            patch.object(
                commands, "_fetch_proposal_status", return_value="offer_sent"
            ),
        ):
            out = commands.handle_teams_command("approve #42")
        assert out["ok"] is False
        assert "WAS sent" in out["message"]
        assert "not" in out["message"].lower()
        assert "Could not execute" not in out["message"]

    def test_exception_before_send_reports_nothing_happened(self):
        from app.teams import commands

        with (
            patch.object(commands, "find_pending_item", return_value=self._item()),
            patch.object(
                commands,
                "_fetch_bundle",
                return_value={"subject": "Intro call", "sender": "a@b.com"},
            ),
            patch.object(
                commands,
                "execute_lexi_approval",
                side_effect=RuntimeError("database is locked"),
            ),
            patch.object(
                commands, "_fetch_proposal_status", return_value="pending_approval"
            ),
        ):
            out = commands.handle_teams_command("approve #42")
        assert out["ok"] is False
        assert "Could not execute" in out["message"]

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


class TestApproveRetriesFailedSend:
    """Live H-4: a failed send escalates to needs_kory with the draft intact —
    Kory's approve retry must re-dispatch, not be refused."""

    def test_typed_approve_routes_needs_kory_to_run_approval(self):
        from app.teams import commands

        ran = {}

        def fake_run_approval(**kwargs):
            ran.update(kwargs)
            return {"ok": True, "handled": True, "message": "Sent.", "proposal_id": 6861}

        with (
            patch.object(commands, "find_pending_item", return_value=None),
            patch.object(commands, "_find_invite_item", return_value=None),
            patch.object(commands, "_fetch_proposal_status", return_value="needs_kory"),
            patch.object(commands, "_run_approval", side_effect=fake_run_approval),
        ):
            out = commands.handle_teams_command("approve #6861")
        assert out["ok"] is True
        assert ran["proposal_id"] == 6861
        assert ran["decision"] == "approved"

    def test_executor_accepts_needs_kory_with_draft(self):
        from app.agents import comms_agent as ca
        from app.storage.lexi_db import get_lexi_connection

        with get_lexi_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO email_threads (thread_id, subject, sender, raw_body) "
                "VALUES ('lt-h4-retry', '[TEST] H4 retry', 'anjana@example.com', 'body')"
            )
            conn.execute("DELETE FROM proposals WHERE id = 99003")
            conn.execute(
                "INSERT INTO proposals (id, thread_id, status, proposed_slots, drafted_reply) "
                "VALUES (99003, 'lt-h4-retry', 'needs_kory', '[]', 'Hi Anjana — times below.')"
            )
            conn.commit()
        try:
            with (
                patch.object(ca, "_send_drafted_reply", return_value=(True, None)),
                patch.object(ca, "_place_holds_isolated", return_value=(0, None)),
                patch.object(ca, "is_hold_reminder_proposal", create=True) as _unused,
                patch(
                    "app.scheduling.hold_reminder.is_hold_reminder_proposal",
                    return_value=False,
                ),
            ):
                result = ca.execute_lexi_approval(99003, "approved", "", "kory")
            assert result.ok is True
            assert result.email_sent is True
            assert result.status == "offer_sent"
        finally:
            with get_lexi_connection() as conn:
                conn.execute("DELETE FROM approvals WHERE proposal_id = 99003")
                conn.execute("DELETE FROM audit_log WHERE reference_id = '99003'")
                conn.execute("DELETE FROM proposals WHERE id = 99003")
                conn.execute("DELETE FROM email_threads WHERE thread_id = 'lt-h4-retry'")
                conn.commit()


class TestInvitePromptIsDecisionShaped:
    """Live H-10 feedback: the invite prompt replayed the entire offer email —
    it must show the picked time, extra attendees, and the exact commands."""

    def test_prompt_shows_pick_not_full_email(self):
        import asyncio

        from app.bot import teams_publisher as tp
        from app.storage.lexi_db import get_lexi_connection

        with get_lexi_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO email_threads (thread_id, subject, sender, raw_body, recipient_timezone) "
                "VALUES ('lt-h10-p', '[TEST] Quick catch-up? — LT-H4', 'anjana@example.com', "
                "'full offer email body that must NOT be replayed', 'America/New_York')"
            )
            conn.execute("DELETE FROM proposals WHERE id = 99005")
            conn.execute(
                "INSERT INTO proposals (id, thread_id, status, proposed_slots, drafted_reply, recipient_selected_slot) "
                "VALUES (99005, 'lt-h10-p', 'pending_invite', '[]', 'the whole drafted offer email', "
                "'{\"start\": \"2026-08-24T10:00:00-06:00\", \"end\": \"2026-08-24T10:30:00-06:00\", "
                "\"extra_attendees\": [\"anjana.kummetha@iconicfounders.com\"]}')"
            )
            conn.commit()

        sent = {}

        async def fake_push(text, *, proposal_id=None, **kwargs):
            sent["text"] = text

        try:
            with (
                patch.object(tp, "push_approval_text_to_teams", side_effect=fake_push),
                patch.object(tp, "_mark_teams_push_sent"),
            ):
                asyncio.run(tp.push_invite_prompt_for_proposal_id(99005))
            text = sent["text"]
            assert "Send calendar invite? — #99005" in text
            assert "Monday, August 24" in text
            assert "12:00" in text  # ET-first rendering for a NY recipient
            assert "anjana.kummetha@iconicfounders.com" in text
            assert "approve #99005" in text
            assert "the whole drafted offer email" not in text
        finally:
            with get_lexi_connection() as conn:
                conn.execute("DELETE FROM proposals WHERE id = 99005")
                conn.execute("DELETE FROM email_threads WHERE thread_id = 'lt-h10-p'")
                conn.commit()


class TestBareSendSeesInvites:
    def test_bare_send_resolves_single_pending_invite(self):
        """Live D-4: one pending invite + zero drafts left `send` unresolved."""
        from app.bot import teams_text as tt

        class _Invite:
            proposal_id = 7041

        with (
            patch.object(tt, "get_lexi_pending_queue", return_value=[]),
            patch(
                "app.agents.comms_agent.get_lexi_invite_queue",
                return_value=[_Invite()],
            ),
        ):
            out = tt.parse_teams_command("send")
        assert out == {"action": "approve", "proposal_id": 7041, "option": 1}

    def test_bare_send_with_nothing_pending_says_so(self):
        from app.bot import teams_text as tt

        with (
            patch.object(tt, "get_lexi_pending_queue", return_value=[]),
            patch("app.agents.comms_agent.get_lexi_invite_queue", return_value=[]),
        ):
            out = tt.parse_teams_command("send")
        assert out["action"] == "unresolved"
        assert "Nothing is waiting" in out["message"]
