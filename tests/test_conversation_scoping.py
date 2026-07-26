"""Regression tests for Run-1 live defects.

Composio's OUTLOOK_LIST_MESSAGES ignores the `filter` argument, so it returns
the newest messages in the folder regardless of conversation. Unrelated mail
then leaked into thread context and was mistaken for delegation.
"""

from __future__ import annotations

from app.agents.delegation import detect_delegation
from app.integrations.outlook_thread import filter_by_conversation
from app.scheduling.email_format import sender_first_name


def _msg(mid: str, conv: str | None) -> dict:
    payload: dict = {"id": mid, "subject": "s"}
    if conv is not None:
        payload["conversationId"] = conv
    return payload


def test_filter_drops_messages_from_other_conversations():
    messages = [
        _msg("a", "conv-1"),
        _msg("b", "conv-2"),
        _msg("c", "conv-1"),
        _msg("d", "conv-3"),
    ]
    kept = filter_by_conversation(messages, "conv-1")
    assert [m["id"] for m in kept] == ["a", "c"]


def test_filter_keeps_messages_missing_conversation_id():
    """Absent field is unverifiable, not a mismatch — dropping it breaks threads."""
    messages = [_msg("a", None), _msg("b", "other")]
    kept = filter_by_conversation(messages, "conv-1")
    assert [m["id"] for m in kept] == ["a"]


def test_filter_requires_a_conversation_id():
    assert filter_by_conversation([_msg("a", "conv-1")], "") == []


def test_unrelated_thread_text_does_not_trigger_delegation():
    """The live failure: another thread's 'Looping in Lexi' leaked into a cold
    outreach email and made Lexi draft meeting times for a stranger."""
    contaminated = (
        "I got your info from the YPO Construction Network directory.\n\n"
        "[Prior messages in this email chain]\n"
        "--- Earlier in thread ---\n"
        "Looping in Lexi to find us a time.\n"
    )
    leaked = detect_delegation(
        subject="YPO request - Construction Network",
        body=contaminated,
        sender="kory.mitchell@iconicfounders.com",
    )
    own_text = detect_delegation(
        subject="Intro",
        body="Looping in Lexi to find us a time.",
        sender="kory.mitchell@iconicfounders.com",
    )
    # Kory's own words still delegate; borrowed quoted text must not.
    assert own_text.is_delegation is True
    assert leaked.is_delegation is False, (
        "quoted text from another thread must not count as delegation"
    )


def test_run_together_mailbox_does_not_become_a_first_name():
    assert sender_first_name("anjanakummetha@gmail.com") == "there"
    assert sender_first_name("jsmith2018@gmail.com") == "there"
    # Normal addresses still resolve.
    assert sender_first_name("anjana.kummetha@gmail.com") == "Anjana"
    assert sender_first_name("kory@ifg.vc") == "Kory"
