"""The structured LLM plan upgrade: Hermes returns explicit dates and time
bounds, deterministic code validates and clamps them. The model does language;
the code does the arithmetic guarantees."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.scheduling.scheduling_plan import (
    SchedulingPlan,
    _llm_time_window,
    _llm_window_from_dates,
    _merge_llm_plan,
)
from app.scheduling.scheduling_window import SchedulingWindow, TimeOfDayWindow

MT = ZoneInfo("America/Denver")
NOW = datetime(2026, 8, 4, 9, 0, tzinfo=MT)
TODAY = date(2026, 8, 4)

SENDER_WINDOW = SchedulingWindow(
    start=date(2026, 8, 5), end=date(2026, 8, 9), source="body", label="this week"
)


class TestLlmWindowFromDates:
    def test_valid_dates_accepted_with_label(self):
        window = _llm_window_from_dates(
            {"window_start": "2026-08-05", "window_end": "2026-08-16", "window_label": "this week or next"},
            sender_window=SENDER_WINDOW,
            today=TODAY,
        )
        assert window is not None
        assert (window.start, window.end) == (date(2026, 8, 5), date(2026, 8, 16))
        assert window.label == "this week or next"
        assert window.source == "llm"

    def test_novel_phrasing_needs_no_regex_branch(self):
        # "the week after Labor Day" — no regex branch exists; dates suffice.
        window = _llm_window_from_dates(
            {"window_start": "2026-09-08", "window_end": "2026-09-13"},
            sender_window=None,
            today=TODAY,
        )
        assert window is not None
        assert window.label == "Sep 8–Sep 13"

    def test_past_start_clamped_to_today(self):
        window = _llm_window_from_dates(
            {"window_start": "2026-08-03", "window_end": "2026-08-09"},
            sender_window=SENDER_WINDOW,
            today=TODAY,
        )
        assert window is not None
        assert window.start == TODAY

    def test_entirely_past_window_rejected(self):
        assert (
            _llm_window_from_dates(
                {"window_start": "2026-07-01", "window_end": "2026-07-05"},
                sender_window=SENDER_WINDOW,
                today=TODAY,
            )
            is None
        )

    def test_inverted_range_rejected(self):
        assert (
            _llm_window_from_dates(
                {"window_start": "2026-08-20", "window_end": "2026-08-10"},
                sender_window=None,
                today=TODAY,
            )
            is None
        )

    def test_absurdly_wide_window_rejected(self):
        assert (
            _llm_window_from_dates(
                {"window_start": "2026-08-05", "window_end": "2026-12-01"},
                sender_window=None,
                today=TODAY,
            )
            is None
        )

    def test_ungrounded_single_day_rejected(self):
        # Sender named no timeframe; a hallucinated "tomorrow" must not
        # hard-narrow scheduling to one day.
        assert (
            _llm_window_from_dates(
                {"window_start": "2026-08-05", "window_end": "2026-08-05"},
                sender_window=None,
                today=TODAY,
            )
            is None
        )

    def test_grounded_single_day_kept(self):
        window = _llm_window_from_dates(
            {"window_start": "2026-08-07", "window_end": "2026-08-07"},
            sender_window=SENDER_WINDOW,
            today=TODAY,
        )
        assert window is not None

    def test_garbage_dates_rejected(self):
        assert (
            _llm_window_from_dates(
                {"window_start": "next tuesday", "window_end": "2026-08-16"},
                sender_window=None,
                today=TODAY,
            )
            is None
        )


class TestLlmTimeWindow:
    def test_early_morning_floor_applies(self):
        window = _llm_time_window({"earliest_hour": 6, "latest_hour": 11})
        assert window is not None
        assert window.start_hour == 7  # V-1 floor

    def test_seven_am_kept(self):
        window = _llm_time_window({"earliest_hour": 7, "latest_hour": 11})
        assert (window.start_hour, window.end_hour) == (7, 11)

    def test_afternoon_preference(self):
        window = _llm_time_window({"earliest_hour": 15, "latest_hour": None})
        assert (window.start_hour, window.end_hour) == (15, 17)

    def test_no_preference_returns_none(self):
        assert _llm_time_window({"earliest_hour": None, "latest_hour": None}) is None
        assert _llm_time_window({}) is None

    def test_inverted_discarded(self):
        assert _llm_time_window({"earliest_hour": 16, "latest_hour": 9}) is None

    def test_late_end_capped(self):
        window = _llm_time_window({"earliest_hour": 8, "latest_hour": 23})
        assert window.end_hour == 19


class TestMergeLlmPlan:
    def test_dates_beat_label_reparse(self):
        plan = SchedulingPlan(window=SENDER_WINDOW)
        merged = _merge_llm_plan(
            plan,
            {
                "task_type": "offer_times",
                "window_start": "2026-08-05",
                "window_end": "2026-08-16",
                "window_label": "this week or next",
                "earliest_hour": 7,
                "latest_hour": 11,
                "duration_minutes": 30,
                "meeting_format": "virtual",
            },
            subject="s",
            body="b",
            now=NOW,
        )
        assert merged.window.end == date(2026, 8, 16)
        assert merged.time_window is not None
        assert merged.time_window.start_hour == 7
        assert merged.meeting_format == "virtual"

    def test_invalid_dates_fall_back_to_label(self):
        plan = SchedulingPlan(window=None)
        merged = _merge_llm_plan(
            plan,
            {"window_start": "soon", "window_end": None, "window_label": "next week"},
            subject="s",
            body="b",
            now=NOW,
        )
        assert merged.window is not None
        assert merged.window.label == "next week"

    def test_nothing_usable_keeps_rule_window(self):
        plan = SchedulingPlan(window=SENDER_WINDOW)
        merged = _merge_llm_plan(
            plan, {"task_type": "offer_times"}, subject="s", body="b", now=NOW
        )
        assert merged.window is SENDER_WINDOW
        assert merged.time_window is None


class TestEngineHonorsPlanTimeWindow:
    def test_plan_time_window_overrides_regex(self):
        from app.scheduling import slot_engine as se

        plan = SchedulingPlan(
            window=None,
            source="open_horizon",
            time_window=TimeOfDayWindow(
                start_hour=15, start_minute=0, end_hour=17, end_minute=0, label="afternoons"
            ),
        )
        result = se.find_valid_slots(
            {"status": "available", "busy_events": [], "horizon_days": 7},
            intent="referral_or_intro",
            subject="quick call",
            body="early morning would be great",  # regex says mornings; plan must win
            plan=plan,
            reference_now=NOW,
        )
        for slot in result.slots:
            hour = datetime.fromisoformat(slot["start"]).hour
            assert hour >= 15, f"slot at {hour}:00 violates the plan's 15:00 floor"


class TestFallbackPlansCarryTimeWindow:
    def test_shift_and_open_horizon_preserve_time_window(self):
        from app.scheduling.window_fallback import _plan_without_window, _shift_plan_window

        tw = TimeOfDayWindow(start_hour=7, start_minute=0, end_hour=11, end_minute=0, label="early")
        plan = SchedulingPlan(window=SENDER_WINDOW, time_window=tw)
        assert _shift_plan_window(plan, week_offset=1).time_window is tw
        assert _plan_without_window(plan).time_window is tw
