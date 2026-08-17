"""Kory approves by draft number, not by raw proposal id.

"approve #9868" is what he was typing. The id is a database key that leaks a
five-digit number into the one command he uses most. He asked for "approve
draft 1", renumbered as drafts clear.

The two forms must coexist: a Teams message from last week quotes a raw id, and
re-typing it must not silently hit a different draft. They can never collide —
a draft number is bounded by the queue length and ids are four digits and up —
so the rule is simply "inside the queue means position, outside means id".
"""

from __future__ import annotations

from unittest.mock import patch

from app.bot.teams_text import (
    format_pending_list,
    parse_teams_command,
    resolve_pending_ref,
)


class _Item:
    def __init__(self, proposal_id: int, position: int | None = None, subject: str = "Intro call"):
        self.proposal_id = proposal_id
        self.draft_number = position
        self.subject = subject
        self.sender = "anjana@example.com"


_QUEUE = [_Item(9868, 1), _Item(9871, 2, "Coffee with Curtis"), _Item(9880, 3)]


def _with_queue(items):
    return patch("app.bot.teams_text.get_lexi_pending_queue", return_value=items)


def test_draft_number_maps_to_the_proposal_in_that_position():
    with _with_queue(_QUEUE):
        assert resolve_pending_ref(1) == 9868
        assert resolve_pending_ref(2) == 9871
        assert resolve_pending_ref(3) == 9880


def test_a_raw_proposal_id_still_resolves_to_itself():
    """An older Teams message quotes #9871; re-typing it must still work."""
    with _with_queue(_QUEUE):
        assert resolve_pending_ref(9871) == 9871


def test_numbers_past_the_queue_are_treated_as_ids_not_positions():
    with _with_queue(_QUEUE):
        assert resolve_pending_ref(4) == 4  # nothing in position 4 — leave alone


def test_renumbering_follows_the_queue_as_drafts_clear():
    """Draft 1 is approved and leaves; what was draft 2 becomes draft 1."""
    with _with_queue(_QUEUE):
        assert resolve_pending_ref(1) == 9868
    remaining = [_Item(9871, 1, "Coffee with Curtis"), _Item(9880, 2)]
    with _with_queue(remaining):
        assert resolve_pending_ref(1) == 9871


def test_approve_command_resolves_the_draft_number():
    with _with_queue(_QUEUE):
        cmd = parse_teams_command("approve 2")
    assert cmd["action"] == "approve"
    assert cmd["proposal_id"] == 9871


def test_reject_and_show_draft_resolve_too():
    with _with_queue(_QUEUE):
        rej = parse_teams_command("reject 3 — not a fit")
        show = parse_teams_command("show draft 2")
    assert rej["proposal_id"] == 9880 and rej["reason"] == "not a fit"
    assert show["proposal_id"] == 9871


def test_a_db_failure_leaves_the_number_alone_rather_than_dropping_the_command():
    with patch(
        "app.bot.teams_text.get_lexi_pending_queue", side_effect=RuntimeError("db down")
    ):
        assert resolve_pending_ref(2) == 2


def test_pending_list_shows_draft_numbers_not_ids():
    text = format_pending_list(_QUEUE)
    assert "draft 1" in text and "draft 2" in text
    assert "9868" not in text and "9871" not in text
    assert "approve draft N" in text


def test_pending_list_numbers_by_position_when_the_field_is_unset():
    """The renderer must never fall back to printing a raw id."""
    text = format_pending_list([_Item(9868), _Item(9871)])
    assert "draft 1" in text and "draft 2" in text
    assert "9868" not in text
