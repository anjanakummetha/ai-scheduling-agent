"""What Lexi tells Kory to do about a proposal that has stalled.

_status_hint was the single least-covered live function in the scheduling code —
one line of ten ever executed by a test. It is also where two of the three
broken command phrasings lived (`send invite #N`, `retry scheduling #N`): Lexi
telling Kory to type something the parser did not accept, on a thread that was
already stuck. Nobody noticed because nothing exercised it.

Two properties, both of which have been violated:
  1. every status the sweeper can nudge about gets advice (not a generic line);
  2. every command in that advice actually parses.
"""

from __future__ import annotations

import re

import pytest

from app.bot.teams_text import parse_teams_command
from app.jobs.stuck_proposals import _STUCK_STATUSES, _status_hint
from app.scheduling.proposal_state import ProposalStatus

PROPOSAL_ID = 4021


@pytest.mark.parametrize("status", _STUCK_STATUSES)
def test_every_stuck_status_gets_advice_naming_the_proposal(status: str):
    hint = _status_hint(status, PROPOSAL_ID)
    assert hint.strip(), f"{status} produced no advice at all"
    assert str(PROPOSAL_ID) in hint or "pending" in hint, (
        f"{status} advice does not tell Kory which proposal or where to look: {hint!r}"
    )


@pytest.mark.parametrize("status", _STUCK_STATUSES)
def test_every_command_in_the_advice_parses(status: str):
    """The bug that lived here. Advice Lexi cannot act on strands the thread it
    is nudging about."""
    hint = _status_hint(status, PROPOSAL_ID)
    commands = [c.strip() for c in re.findall(r"\*\*([^*]+)\*\*", hint)]
    assert commands, f"{status} advice offers no command: {hint!r}"

    unparsed = []
    for command in commands:
        filled = command.replace(" N", " 1").replace("— reason", "— not a fit")
        if parse_teams_command(filled) is None:
            unparsed.append(filled)
    assert not unparsed, (
        f"{status} tells Kory to type commands Lexi does not recognise: {unparsed}"
    )


def test_the_advice_is_specific_to_the_status_not_one_generic_line():
    """A nudge that says the same thing for every state is noise Kory learns to
    ignore, which is how a stuck thread stays stuck."""
    hints = {status: _status_hint(status, PROPOSAL_ID) for status in _STUCK_STATUSES}
    assert len(set(hints.values())) >= 4, hints


def test_the_invite_case_points_at_the_step_that_books_it():
    """pending_invite means the counterpart already chose. The only thing left
    is dispatching the invite."""
    hint = _status_hint(ProposalStatus.PENDING_INVITE, PROPOSAL_ID)
    assert "send invite" in hint.lower(), hint
    assert parse_teams_command(f"send invite #{PROPOSAL_ID}") is not None


def test_the_reoffer_case_points_at_a_fresh_search():
    hint = _status_hint(ProposalStatus.PENDING_REOFFER, PROPOSAL_ID)
    assert "retry scheduling" in hint.lower(), hint
    assert parse_teams_command(f"retry scheduling #{PROPOSAL_ID}") is not None


def test_an_unknown_status_still_produces_usable_advice():
    """The sweeper's status list is derived from the state machine, so a new
    status arrives here before anyone writes a branch for it."""
    hint = _status_hint("some_future_status", PROPOSAL_ID)
    assert hint.strip()
    commands = [c.strip() for c in re.findall(r"\*\*([^*]+)\*\*", hint)]
    for command in commands:
        assert parse_teams_command(command) is not None, command
