"""Asana regressions found testing live writes against Kory's real account."""

from __future__ import annotations

from datetime import date, timedelta

from app.integrations.asana_manager import normalize_due_on


def test_past_due_date_rolls_forward():
    """Live bug: "August 3" was saved as 2025-08-03, a date already past."""
    assert normalize_due_on("2025-08-03") == "2026-08-03"


def test_future_date_is_untouched():
    future = (date.today() + timedelta(days=30)).isoformat()
    assert normalize_due_on(future) == future


def test_today_is_kept():
    today = date.today().isoformat()
    assert normalize_due_on(today) == today


def test_unparseable_input_is_not_mangled():
    """Truncating to 10 chars turned "next friday" into "next frida"."""
    assert normalize_due_on("next friday") == "next friday"
    assert normalize_due_on("") == ""


def test_write_slugs_exist_in_composio_toolkit():
    """ASANA_DELETE_A_TASK and ASANA_CREATE_A_STORY_COMMENT do not exist;
    the real slugs are ASANA_DELETE_TASK and ASANA_CREATE_TASK_COMMENT."""
    import app.integrations.asana_manager as am
    from pathlib import Path

    source = Path(am.__file__).read_text()
    assert "ASANA_DELETE_A_TASK" not in source
    assert "ASANA_CREATE_A_STORY_COMMENT" not in source
    assert "ASANA_DELETE_TASK" in source
    assert "ASANA_CREATE_TASK_COMMENT" in source
