"""Live H-4: 'None of those quite work — could we do Monday, August 17 at
1:00 PM ET instead?' was matched to the Aug 10 slot and nearly booked a time
the recipient explicitly declined."""

from __future__ import annotations

from unittest.mock import patch

from app.scheduling.recipient_slot import (
    match_recipient_slot_choice,
    recipient_times_rejected,
)

SLOTS = [
    {"start": "2026-08-10T10:00:00-06:00", "end": "2026-08-10T10:30:00-06:00"},
    {"start": "2026-08-17T10:00:00-06:00", "end": "2026-08-17T10:30:00-06:00"},
    {"start": "2026-08-19T09:00:00-06:00", "end": "2026-08-19T09:30:00-06:00"},
]

LIVE_COUNTER = (
    "Hi Lexi,\n\nNone of those quite work for me. Could we do Monday, "
    "August 17 at 1:00 PM ET (11:00 AM MT) instead?\n\nThanks,\nAnjana"
)


def test_live_counter_proposal_is_not_a_slot_pick():
    assert match_recipient_slot_choice(LIVE_COUNTER, SLOTS) is None


def test_live_counter_proposal_reads_as_rejection():
    assert recipient_times_rejected(LIVE_COUNTER)
    assert recipient_times_rejected("Those times don't quite work for me.")
    assert recipient_times_rejected("None of them work, sorry.")


def test_bare_weekday_pick_still_works_when_unambiguous():
    slots = [SLOTS[0], SLOTS[2]]  # one Monday, one Wednesday
    chosen = match_recipient_slot_choice("Monday works great for me!", slots)
    assert chosen is SLOTS[0]


def test_bare_weekday_is_refused_when_two_slots_share_the_day():
    # Two Mondays offered — "Monday works" cannot disambiguate.
    assert match_recipient_slot_choice("Monday works for me!", SLOTS) is None


def test_exact_date_with_contradicting_time_is_not_a_pick():
    text = "Monday, August 10 at 3:00 PM works for me"  # slot is 10:00 MT / 12 ET
    assert match_recipient_slot_choice(text, [SLOTS[0]]) is None


def test_exact_date_with_matching_et_time_is_a_pick():
    text = "Monday, August 10 at 12:00 PM ET works for me"
    assert match_recipient_slot_choice(text, [SLOTS[0]]) is SLOTS[0]


def test_rejection_with_counter_time_routes_to_inbound_time_path():
    from app.agents import offer_reply as orp

    proposal = {
        "proposal_id": 6861,
        "sender": "anjana@example.com",
        "proposed_slots": (
            '[{"start": "2026-08-10T10:00:00-06:00", "end": "2026-08-10T10:30:00-06:00"}]'
        ),
    }
    raw = {
        "conversation_id": "conv-h4",
        "message_id": "m1",
        "sender": "anjana@example.com",
        "subject": "Re: [TEST] Quick catch-up? — LT-H4",
        "raw_body": LIVE_COUNTER,
    }
    with (
        patch.object(orp, "_find_offer_sent_proposal", return_value=proposal),
        patch.object(orp, "mark_recipient_reoffer_request") as reoffer,
    ):
        out = orp.try_handle_recipient_slot_reply(raw)
    assert out is not None
    assert out["action"] == "offer_reply_unparsed"
    reoffer.assert_not_called()


def test_blocked_counter_time_parks_proposal_for_reoffer():
    """H-4 follow-through: a busy counter-proposal must release holds and move
    the proposal to pending_reoffer so `retry scheduling for #N` works."""
    from app.agents import lexi_thread_followup as ltf

    proposal = {
        "proposal_id": 6861,
        "status": "offer_sent",
        "intent_classification": "referral_or_intro",
        "subject": "[TEST] Quick catch-up? — LT-H4",
        "sender": "anjana@example.com",
    }
    marked = {}
    with (
        patch(
            "app.scheduling.inbound_availability.body_looks_like_inbound_availability",
            return_value=True,
        ),
        patch(
            "app.scheduling.inbound_availability.extract_inbound_time_candidates",
            return_value=[
                {"start": "2026-08-17T11:00:00-06:00", "end": "2026-08-17T11:30:00-06:00"}
            ],
        ),
        patch(
            "app.scheduling.calendar_context.load_scheduling_calendar_context",
            return_value={"status": "available", "busy_events": []},
        ),
        patch(
            "app.scheduling.inbound_availability.validate_inbound_candidates",
            return_value=([], [{"start": "2026-08-17T11:00:00-06:00"}], ["busy at Monday 11:00 AM"]),
        ),
        patch(
            "app.scheduling.inbound_availability.find_compliant_slots_on_date",
            return_value=[],
        ),
        patch(
            "app.agents.comms_agent.mark_recipient_reoffer_request",
            side_effect=lambda pid, *, reply_body: marked.update(pid=pid),
        ),
        patch.object(ltf, "_notify_kory_followup"),
    ):
        out = ltf._try_inbound_time_suggestion(
            {"subject": "Re: [TEST] Quick catch-up? — LT-H4"},
            proposal,
            body="Could we do Monday, August 17 at 1:00 PM ET instead?",
        )
    assert out is not None and out["action"] == "inbound_time_blocked"
    assert marked["pid"] == 6861
    assert "retry scheduling for #6861" in out["message"]


def test_retry_gate_accepts_pending_reoffer():
    from app.agents import inbound_reply as ir

    with (
        patch.object(
            ir,
            "_fetch_proposal_bundle",
            return_value={"status": "pending_reoffer", "id": 6861},
        ),
        patch.object(ir, "process_proposal_schedule", return_value=True, create=True),
        patch(
            "app.agents.scheduler_agent.process_proposal_schedule", return_value=True
        ),
        patch("app.bot.teams_publisher.schedule_teams_approval_push"),
    ):
        out = ir.retry_scheduling_with_guidance(6861, "offer Monday 10:30 AM MT")
    assert "not awaiting scheduling guidance" not in str(out.get("error") or "")


def test_plain_rejection_still_reoffers():
    from app.agents import offer_reply as orp

    proposal = {
        "proposal_id": 6861,
        "sender": "anjana@example.com",
        "proposed_slots": (
            '[{"start": "2026-08-10T10:00:00-06:00", "end": "2026-08-10T10:30:00-06:00"}]'
        ),
    }
    raw = {
        "conversation_id": "conv-h4",
        "message_id": "m1",
        "sender": "anjana@example.com",
        "subject": "Re: [TEST] Quick catch-up? — LT-H4",
        "raw_body": "Sorry, none of those times work for me. What else do you have?",
    }
    with (
        patch.object(orp, "_find_offer_sent_proposal", return_value=proposal),
        patch.object(
            orp,
            "mark_recipient_reoffer_request",
            return_value={"ok": True, "status": "pending_reoffer"},
        ) as reoffer,
        patch("app.bot.teams_publisher.schedule_teams_reoffer_prompt_push"),
    ):
        out = orp.try_handle_recipient_slot_reply(raw)
    assert out is not None
    assert out["action"] == "recipient_reoffer_request"
    reoffer.assert_called_once()


def test_extractor_continuation_times_share_the_day():
    """H-4 retry: 'Monday August 17 at 10:30 AM MT and 3:30 PM MT' must yield
    BOTH times — the second was silently dropped."""
    import datetime
    import zoneinfo

    from app.scheduling.inbound_availability import extract_inbound_time_candidates

    ref = datetime.datetime(2026, 8, 5, 6, 0, tzinfo=zoneinfo.ZoneInfo("America/Denver"))
    got = extract_inbound_time_candidates(
        "Offer Monday August 17 at 10:30 AM MT and 3:30 PM MT only", reference=ref
    )
    starts = [c["start"] for c in got]
    assert "2026-08-17T10:30:00-06:00" in starts
    assert "2026-08-17T15:30:00-06:00" in starts


def test_single_guided_time_passes_gate_via_plan():
    """A single Kory-directed time must not be gate-blocked on the 2-slot
    minimum when the plan carries relaxing guidance."""
    from app.scheduling.pre_approval_gate import verify_before_kory_approval
    from app.scheduling.scheduling_plan import build_scheduling_plan

    slots = [{"start": "2026-08-17T10:30:00-06:00", "end": "2026-08-17T11:00:00-06:00"}]
    plan = build_scheduling_plan(
        subject="[TEST] LT-H4",
        body="Offer Monday August 17 at 10:30 AM MT only",
        intent="referral_or_intro",
        use_llm=False,
    )
    plan.kory_guidance = "Offer Monday August 17 at 10:30 AM MT only"
    report = verify_before_kory_approval(
        slots=slots,
        calendar_context={"status": "available", "busy_events": []},
        plan=plan,
        intent="referral_or_intro",
        subject="[TEST] LT-H4",
        body="",
    )
    assert not any("need at least" in c for c in report.checks), report.checks


def test_week_shift_reads_as_rejection():
    """Live H-5: 'could we look at the following week instead?' fell to the
    unparsed path and kept the stale holds."""
    assert recipient_times_rejected(
        "Thanks for these! Actually, that whole week is looking messy on my "
        "end now — could we look at the following week instead?"
    )
    assert recipient_times_rejected("How about next week instead?")
    # A genuine pick naming a day must NOT read as rejection.
    assert not recipient_times_rejected("Tuesday next week works great for me!")
    assert not recipient_times_rejected("Monday, August 17 at 12:30 PM ET works.")


def test_body_preview_skips_greeting_line():
    from app.agents.lexi_thread_followup import _body_preview

    body = "Hi Lexi,\n\nCould we look at the following week instead?\n\nThanks,\nAnjana"
    assert _body_preview(body) == "Could we look at the following week instead?"
    assert _body_preview("Just one line") == "Just one line"
