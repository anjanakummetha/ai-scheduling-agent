"""Anything Lexi tells Kory to type must actually work.

The approval surface advertised `approve draft N` — in the help text, in the
footer under every `pending` list, in the "more than one draft is waiting"
prompt, and in the stuck-proposal nudge — while the parser only accepted
`approve N`. Typing exactly what Lexi asked for returned "Not a Lexi command."

That is the worst kind of defect on this surface: it looks like Lexi ignoring
him, on the one command that sends email, and the instructions telling him to do
it were coming from Lexi herself.

So the copy and the parser are checked against each other. A new command phrase
in the help text with no matching pattern fails here.
"""

from __future__ import annotations

import re

import pytest

from app.bot.teams_text import TEAMS_HELP_TEXT, parse_teams_command

# Every phrasing Lexi puts in front of Kory, with the action it must produce.
ADVERTISED = [
    ("pending", "pending"),
    ("approve draft 1", "approve"),
    ("approve 1", "approve"),
    ("approve #1", "approve"),
    ("approve draft 1 option 2", "approve"),
    ("send draft 2", "approve"),
    ("reject draft 1 — not a fit", "reject"),
    ("reject 1 — not a fit", "reject"),
    ("reject draft 2", "reject"),
    ("show draft 1", "show_draft"),
    ("show 1", "show_draft"),
    ("cancel meeting #4021", "cancel_meeting"),
    ("inbound", "inbound"),
    ("inbox review", "inbox_review"),
    ("unanswered", "unanswered"),
    ("today", "today"),
    ("prebrief", "prebrief"),
    ("help", "help"),
]


@pytest.mark.parametrize("text, expected_action", ADVERTISED)
def test_the_phrasing_lexi_advertises_is_the_phrasing_she_accepts(
    text: str, expected_action: str
):
    command = parse_teams_command(text)
    assert command is not None, (
        f"Lexi tells Kory to type {text!r} and then does not recognise it"
    )
    assert command.get("action") == expected_action, command


def test_every_backticked_command_in_the_help_text_parses():
    """The help text is the contract. Anything in backticks there, with its
    placeholder filled in, has to reach an action."""
    advertised = set()
    for snippet in re.findall(r"`([^`]+)`", TEAMS_HELP_TEXT):
        phrase = snippet.strip()
        if not phrase or phrase.startswith("@"):
            continue
        # `approve draft N` / `reject draft N — reason` are templates.
        filled = phrase.replace(" N", " 1").replace("#N", "#1")
        filled = filled.replace("— reason", "— not a fit")
        advertised.add(filled)

    unparsed = sorted(p for p in advertised if parse_teams_command(p) is None)
    assert not unparsed, (
        "The help text offers commands the parser does not accept:\n  "
        + "\n  ".join(unparsed)
    )


def test_the_recovery_advice_in_a_refusal_is_itself_a_working_command():
    """A refusal that suggests a command Lexi cannot parse strands him."""
    for suggestion in ("pending", "approve draft 1", "reject draft 1 — not a fit"):
        assert parse_teams_command(suggestion) is not None, suggestion
