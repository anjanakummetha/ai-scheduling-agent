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
    ("show #4021", "show_draft"),
    # The step that actually books the meeting. Advertised by the
    # pending_invite nudge and previously left to the model to interpret.
    ("send invite #4021", "approve"),
    ("send the invite for #4021", "approve"),
    ("retry scheduling #4021", "retry_scheduling"),
    ("retry scheduling for #4021 — offer Monday 10:30", "retry_scheduling"),
    ("draft #4021", "draft_yes"),
    ("draft #4021 no", "draft_no"),
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


# ---------------------------------------------------------------------------
# The durable guard
# ---------------------------------------------------------------------------

_TEMPLATE_FILLERS = [
    ("#{proposal_id}", "#4021"),
    ("#{item.proposal_id}", "#4021"),
    ("#{pid}", "#4021"),
    ("{ref}", "1"),
    ("#N", "#4021"),
    (" N", " 1"),
    ("<your times>", "offer Monday 10:30"),
    ("— reason", "— not a fit"),
    ("— <your times>", "— offer Monday 10:30"),
]

# Bold text that is emphasis, not a command Kory is being told to type.
_NOT_COMMANDS = {
    "not", "not sent", "nothing was sent", "enabled", "blocked", "approved",
    "individual", "flat", "association", "domain object", "book consensus",
    "the company's own website",
}


def _advertised_commands() -> set[str]:
    """Every **bold** phrase in app/ that reads like a command to type."""
    import pathlib

    app_dir = pathlib.Path(__file__).resolve().parent.parent / "app"
    found: set[str] = set()
    for path in app_dir.rglob("*.py"):
        for raw in re.findall(r"\*\*([a-z][^*\n]{2,60})\*\*", path.read_text()):
            phrase = raw.strip()
            if phrase.lower() in _NOT_COMMANDS:
                continue
            # Only f-string placeholders we know how to fill; anything else is
            # prose we cannot evaluate.
            for template, value in _TEMPLATE_FILLERS:
                phrase = phrase.replace(template, value)
            if "{" in phrase or "}" in phrase:
                continue
            found.add(phrase)
    return found


def test_every_command_lexi_advertises_anywhere_actually_parses():
    """Lexi tells Kory what to type in the help text, in the footer of the
    pending list, in escalation replies and in the stuck-proposal nudge. Every
    one of those has to reach an action.

    Three did not when this test was written — `approve draft N`,
    `send invite #N` and `retry scheduling for #N` — and one of them is the step
    that books the meeting. Typing exactly what Lexi asked for did nothing
    recognisable.
    """
    unparsed = sorted(p for p in _advertised_commands() if parse_teams_command(p) is None)
    assert not unparsed, (
        "Lexi tells Kory to type these and does not recognise them:\n  "
        + "\n  ".join(unparsed)
    )
