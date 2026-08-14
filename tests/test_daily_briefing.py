"""Lexi knowing what was in Kory's morning briefing.

He reads it at 4:45 AM then asks Lexi to act on it — "change those two tasks from
my briefing". The dashboard composes it and Lexi only sends it, so nothing kept a
copy and she had no idea what he meant.
"""

from app.assistant import actions
from app.storage.daily_briefing import (
    _today as _kory_today,
    get_briefing,
    html_to_text,
    latest_briefing,
    prune_briefings,
    save_briefing,
)

BODY = "Top priorities\n• Call Bruce Krinksy about the fractional CFO\n• Elevator landing page"


def _clear():
    from app.storage.daily_briefing import ensure_daily_briefing_table
    from app.storage.lexi_db import get_lexi_connection

    with get_lexi_connection() as conn:
        ensure_daily_briefing_table(conn)
        conn.execute("DELETE FROM daily_briefings")
        conn.commit()


def test_briefing_round_trips():
    _clear()
    out = save_briefing(subject="CEO Daily Briefing", body_text=BODY, message_id="m1")
    assert out["ok"] is True
    stored = get_briefing()
    assert stored["subject"] == "CEO Daily Briefing"
    assert "Bruce Krinksy" in stored["body_text"]


def test_resending_the_same_day_replaces_rather_than_duplicates():
    _clear()
    save_briefing(subject="A", body_text="first")
    save_briefing(subject="B", body_text="second")
    stored = get_briefing()
    assert stored["subject"] == "B"
    assert stored["body_text"] == "second"


def test_empty_body_is_not_stored():
    _clear()
    assert save_briefing(subject="X", body_text="   ")["ok"] is False
    assert get_briefing() is None


def test_action_returns_todays_briefing_verbatim():
    _clear()
    save_briefing(subject="CEO Daily Briefing", body_text=BODY)
    result = actions.todays_briefing_action()
    assert result["ok"] is True
    assert result["is_todays"] is True
    assert result["body"] == BODY
    assert "quote its wording" in result["kory_chat"]


def test_falls_back_to_the_most_recent_briefing_and_says_so():
    """A Monday question about Friday's briefing should still land — labelled.

    "Yesterday" is computed on Kory's clock, not the runner's: CI is UTC, and just
    after midnight there the previous UTC day is still today in Denver — which made
    this pass locally and fail in CI.
    """
    from datetime import date, timedelta

    _clear()
    kory_today = date.fromisoformat(_kory_today())
    yesterday = (kory_today - timedelta(days=1)).isoformat()
    save_briefing(subject="Friday", body_text="friday content", briefing_date=yesterday)
    result = actions.todays_briefing_action()
    assert result["ok"] is True
    assert result["is_todays"] is False
    assert "not today's" in result["kory_chat"]


def test_missing_briefing_says_so_instead_of_inventing_one():
    _clear()
    result = actions.todays_briefing_action()
    assert result["ok"] is False
    assert result["error_code"] == "no_briefing_stored"
    assert "paste the part" in result["kory_message"]


def test_old_briefings_are_pruned_but_recent_ones_kept():
    _clear()
    save_briefing(subject="ancient", body_text="old", briefing_date="2020-01-01")
    save_briefing(subject="today", body_text="new")
    assert prune_briefings(keep_days=30) == 1
    assert get_briefing("2020-01-01") is None
    assert get_briefing() is not None


def test_stale_briefings_fall_outside_the_lookback():
    _clear()
    save_briefing(subject="ancient", body_text="old", briefing_date="2020-01-01")
    assert latest_briefing(within_days=4) is None


def test_html_is_converted_to_readable_text():
    html = (
        "<div><h3>Today</h3><ul><li>Call Bruce</li><li>Review deck</li></ul>"
        "<p>Two meetings &amp; one hold.</p><style>.x{color:red}</style></div>"
    )
    text = html_to_text(html)
    assert "• Call Bruce" in text
    assert "Two meetings & one hold." in text
    assert "color:red" not in text
    assert "<" not in text


def test_a_specific_past_date_can_be_requested():
    _clear()
    save_briefing(subject="Tuesday", body_text="tuesday content", briefing_date="2026-08-11")
    result = actions.todays_briefing_action(briefing_date="2026-08-11")
    assert result["ok"] is True
    assert result["body"] == "tuesday content"
