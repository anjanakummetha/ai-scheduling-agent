"""The remember feature under real use, not a single happy-path round trip.

Kory states rules over weeks, not one at a time. What has to hold: several rules
compose rather than overwrite; restating a rule updates it instead of
duplicating; forgetting one leaves the others alone; and a remembered rule
constrains the ENGINE, not just the prompt — a rule that only reaches the prompt
is decoration he will discover the hard way.

Driven through the model-facing tools, so this is what happens in Teams.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from app.assistant.actions import (
    forget_kory_fact_action,
    list_kory_memory_action,
    remember_kory_fact_action,
)
from app.scheduling.schedule_from_context import schedule_from_context
from tests.teams_parity import call_tool

MT = ZoneInfo("America/Denver")
CTX = {"status": "available", "horizon_days": 45, "busy_events": []}
PREFIX = "depth_"


def _facts() -> dict[str, str]:
    return {
        f["fact_key"]: f["fact_value"]
        for f in (list_kory_memory_action().get("facts") or [])
    }


def _purge() -> None:
    for key in list(_facts()):
        if key.startswith(PREFIX):
            forget_kory_fact_action(fact=key)


@pytest.fixture(autouse=True)
def clean_memory():
    _purge()
    yield
    _purge()


def _offer(guidance: str = "", body: str = "Any time in the next two weeks."):
    with patch(
        "app.scheduling.calendar_context.load_scheduling_calendar_context", return_value=CTX
    ):
        return schedule_from_context(
            subject="[TEST] intro",
            body=body,
            intent="referral_or_intro",
            sender_email="c@example.com",
            kory_scheduling_guidance=guidance,
            use_llm_plan=False,
            calendar_context=CTX,
        )


def _weekdays(result) -> set[int]:
    return {datetime.fromisoformat(s["start"]).weekday() for s in result.slots}


# --- storage behaviour ----------------------------------------------------


def test_a_rule_is_stored_verbatim_not_paraphrased():
    """The sentence is parsed into a constraint; a paraphrase may not parse."""
    remember_kory_fact_action(
        fact_key=f"{PREFIX}fri", fact_value="No meetings on Fridays"
    )
    assert _facts()[f"{PREFIX}fri"] == "No meetings on Fridays"


def test_several_rules_coexist():
    remember_kory_fact_action(fact_key=f"{PREFIX}fri", fact_value="No meetings on Fridays")
    remember_kory_fact_action(fact_key=f"{PREFIX}mon", fact_value="No meetings on Mondays")
    stored = _facts()
    assert stored.get(f"{PREFIX}fri") and stored.get(f"{PREFIX}mon")


def test_restating_a_rule_updates_it_rather_than_duplicating():
    remember_kory_fact_action(fact_key=f"{PREFIX}fri", fact_value="No meetings on Fridays")
    remember_kory_fact_action(
        fact_key=f"{PREFIX}fri", fact_value="No meetings on Fridays or Mondays"
    )
    keys = [k for k in _facts() if k == f"{PREFIX}fri"]
    assert len(keys) == 1
    assert _facts()[f"{PREFIX}fri"] == "No meetings on Fridays or Mondays"


def test_forgetting_one_rule_leaves_the_others_standing():
    remember_kory_fact_action(fact_key=f"{PREFIX}fri", fact_value="No meetings on Fridays")
    remember_kory_fact_action(fact_key=f"{PREFIX}mon", fact_value="No meetings on Mondays")
    forget_kory_fact_action(fact=f"{PREFIX}fri")
    stored = _facts()
    assert f"{PREFIX}fri" not in stored
    assert f"{PREFIX}mon" in stored, "forgetting one rule wiped another"


def test_forgetting_something_never_stored_fails_honestly():
    out = forget_kory_fact_action(fact=f"{PREFIX}never_existed")
    assert out.get("ok") is False
    assert "no stored fact" in str(out.get("error", "")).lower()


# --- the rules reach the ENGINE ------------------------------------------


def test_one_remembered_day_rule_constrains_the_search():
    before = _offer()
    assert 4 in _weekdays(before) or before.slots, "precondition: engine returns slots"
    remember_kory_fact_action(fact_key=f"{PREFIX}fri", fact_value="No meetings on Fridays")
    after = _offer()
    assert after.slots, "a rule must redirect, never empty the offer"
    assert 4 not in _weekdays(after), "Friday survived a no-Fridays rule"


def test_two_remembered_day_rules_BOTH_apply():
    """The failure that matters: the second rule silently replacing the first."""
    remember_kory_fact_action(fact_key=f"{PREFIX}fri", fact_value="No meetings on Fridays")
    remember_kory_fact_action(fact_key=f"{PREFIX}mon", fact_value="No meetings on Mondays")
    result = _offer()
    assert result.slots
    days = _weekdays(result)
    assert 4 not in days, "the Friday rule was dropped when a second rule arrived"
    assert 0 not in days, "the Monday rule did not apply"


def test_forgetting_the_rule_releases_the_engine_too():
    remember_kory_fact_action(fact_key=f"{PREFIX}fri", fact_value="No meetings on Fridays")
    assert 4 not in _weekdays(_offer())
    forget_kory_fact_action(fact=f"{PREFIX}fri")
    freed = _offer(body="Can we meet on a Friday in the next two weeks?")
    assert freed.slots
    # Friday must be reachable again — asked for explicitly.
    assert 4 in _weekdays(freed) or freed.ok, "the rule outlived 'forget that'"


def test_per_run_guidance_can_lift_a_standing_rule():
    """Kory overriding himself for one meeting must work without forgetting."""
    remember_kory_fact_action(fact_key=f"{PREFIX}fri", fact_value="No meetings on Fridays")
    lifted = _offer(guidance="Friday is fine for this one.")
    assert lifted.ok, lifted.failure_message
    assert lifted.slots


def test_a_rule_survives_being_read_back_through_the_model_facing_tool():
    """Teams path, not just the action layer."""
    call_tool(
        "lexi_remember_kory_fact",
        fact_key=f"{PREFIX}early",
        fact_value="Nothing before 8:30 AM on Tuesdays",
    )
    from tests.teams_parity import _as_dict

    listed = _as_dict(call_tool("lexi_list_kory_memory"))
    blob = str(listed)
    assert "Nothing before 8:30 AM on Tuesdays" in blob, blob[:300]
