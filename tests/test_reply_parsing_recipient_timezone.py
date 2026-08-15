"""Reply parsing in the recipient's timezone — the zones the offer email showed.

Offer emails render recipient-local first with MT in parentheses, so real
counterparts reply in THEIR zone ("Thursday at 2 works" from Boston means
2 PM Eastern). These pin the 2026-08-15 fixes:

* extract_inbound_time_candidates(default_tz=...) — unlabeled times parse in
  the sender's stored zone; explicit labels still win.
* match_recipient_slot_choice(recipient_tz=...) — day/hour tokens matched in
  the rendered zones, never in UTC (a Monday 6 PM MT dinner slot is Tuesday
  in UTC and used to be matchable by "Tuesday works").
* Audit-B9 parser gaps: bare "at 2" (meridiem inferred, was a 9 AM phantom),
  "next Tuesday" (was the nearest Tuesday), "1/2 hour call" (was January 2).
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from app.scheduling.inbound_availability import extract_inbound_time_candidates
from app.scheduling.recipient_slot import match_recipient_slot_choice

MT = ZoneInfo("America/Denver")
REF = datetime(2026, 8, 14, 9, 0, tzinfo=MT)  # a Friday morning


def _starts(cands):
    return [c["start"] for c in cands]


# --- extract_inbound_time_candidates with default_tz ---


def test_unlabeled_time_parses_in_senders_zone():
    cands = extract_inbound_time_candidates(
        "Thursday at 2 PM works for me.", reference=REF, default_tz="America/New_York"
    )
    assert _starts(cands) == ["2026-08-20T12:00:00-06:00"]  # 2 PM ET = noon MT


def test_explicit_label_beats_default_tz():
    cands = extract_inbound_time_candidates(
        "Thursday at 2 PM MT works for me.", reference=REF, default_tz="America/New_York"
    )
    assert _starts(cands) == ["2026-08-20T14:00:00-06:00"]


def test_no_default_tz_keeps_mt():
    cands = extract_inbound_time_candidates(
        "Thursday at 2 PM works for me.", reference=REF
    )
    assert _starts(cands) == ["2026-08-20T14:00:00-06:00"]


def test_junk_default_tz_falls_back_to_mt():
    cands = extract_inbound_time_candidates(
        "Thursday at 2 PM works.", reference=REF, default_tz="Not/AZone"
    )
    assert _starts(cands) == ["2026-08-20T14:00:00-06:00"]


def test_early_eastern_ask_survives_plausibility():
    # "6 AM ET" is 4 AM MT — plausible to the WRITER; validators, not the
    # parser, decide whether Kory can take it.
    cands = extract_inbound_time_candidates(
        "We could do Monday at 6 AM ET.", reference=REF
    )
    assert _starts(cands) == ["2026-08-17T04:00:00-06:00"]


def test_continuation_time_inherits_senders_zone():
    cands = extract_inbound_time_candidates(
        "Monday August 17 at 10:30 AM or 3:30 PM.",
        reference=REF,
        default_tz="America/New_York",
    )
    assert _starts(cands) == [
        "2026-08-17T08:30:00-06:00",  # 10:30 ET
        "2026-08-17T13:30:00-06:00",  # 3:30 ET
    ]


def test_continuation_trailing_label_wins():
    cands = extract_inbound_time_candidates(
        "Monday August 17 at 10:30 AM MT or 3:30 PM ET.", reference=REF
    )
    assert _starts(cands) == [
        "2026-08-17T10:30:00-06:00",
        "2026-08-17T13:30:00-06:00",
    ]


# --- audit B9 parser gaps ---


def test_bare_hour_is_afternoon_not_nine_am_phantom():
    cands = extract_inbound_time_candidates(
        "Wednesday at 2 works for me.", reference=REF
    )
    assert _starts(cands) == ["2026-08-19T14:00:00-06:00"]


def test_bare_morning_hour_reads_am():
    cands = extract_inbound_time_candidates("Could we meet Tuesday at 10?", reference=REF)
    assert _starts(cands) == ["2026-08-18T10:00:00-06:00"]


def test_next_weekday_means_next_week():
    # Said on Friday Aug 14: "next Tuesday" is Aug 18 (next week), and
    # crucially NOT re-read once "this" Tuesday would be closer. From a
    # Monday reference, "next Tuesday" must skip the very next day.
    monday = datetime(2026, 8, 10, 9, 0, tzinfo=MT)
    cands = extract_inbound_time_candidates(
        "Could we do next Tuesday at 3 pm?", reference=monday
    )
    assert _starts(cands) == ["2026-08-18T15:00:00-06:00"]


def test_half_hour_fraction_is_not_january_second():
    cands = extract_inbound_time_candidates(
        "Let's do a 1/2 hour call at 3pm on Wednesday.", reference=REF
    )
    assert all(not c["start"].startswith("2027-01-02") for c in cands)
    assert "2026-08-19T15:00:00-06:00" in _starts(cands)


# --- match_recipient_slot_choice in rendered zones ---

# Kory offers Tue 2 PM MT and Tue 4 PM MT to an Eastern counterpart. The email
# showed "4:00 PM ET (2:00 PM MT)" and "6:00 PM ET (4:00 PM MT)".
TWO_TUESDAY_SLOTS = [
    {"start": "2026-08-18T14:00:00-06:00", "end": "2026-08-18T14:30:00-06:00"},
    {"start": "2026-08-18T16:00:00-06:00", "end": "2026-08-18T16:30:00-06:00"},
]


def test_eastern_reply_hour_picks_the_slot_they_saw():
    # "Tuesday at 4 works" from the ET recipient = 4 PM ET = the 2 PM MT slot.
    chosen = match_recipient_slot_choice(
        "Tuesday at 4 works for me.",
        TWO_TUESDAY_SLOTS,
        recipient_tz="America/New_York",
    )
    assert chosen == TWO_TUESDAY_SLOTS[0]


def test_mt_reply_hour_still_matches_without_recipient_tz():
    chosen = match_recipient_slot_choice(
        "Tuesday at 4 works for me.", TWO_TUESDAY_SLOTS
    )
    assert chosen == TWO_TUESDAY_SLOTS[1]


def test_recipient_zone_wins_over_mt_on_hour_collision():
    # Slots at 2 PM MT (= 4 PM ET) and 4 PM MT: "Tuesday at 4" fits slot A in
    # ET and slot B in MT. The email led with ET, so ET resolves it → slot A.
    chosen = match_recipient_slot_choice(
        "Tuesday at 4 sounds good.",
        TWO_TUESDAY_SLOTS,
        recipient_tz="America/New_York",
    )
    assert chosen == TWO_TUESDAY_SLOTS[0]


DINNER_SLOT = [
    # Monday 6 PM MT = Tuesday 00:00 UTC — the raw-UTC weekday bug's shape.
    {"start": "2026-08-17T18:00:00-06:00", "end": "2026-08-17T19:30:00-06:00"},
]


def test_evening_slot_matches_its_local_weekday():
    chosen = match_recipient_slot_choice("Monday works for dinner.", DINNER_SLOT)
    assert chosen == DINNER_SLOT[0]


def test_evening_slot_does_not_match_utc_weekday():
    assert (
        match_recipient_slot_choice("Tuesday works for dinner.", DINNER_SLOT) is None
    )


def test_eastern_hour_affirmation_matches_correct_slot():
    # Email said "6:00 PM ET (4:00 PM MT)" — recipient echoes their local hour.
    chosen = match_recipient_slot_choice(
        "6:00 pm works for me, thanks!",
        TWO_TUESDAY_SLOTS,
        recipient_tz="America/New_York",
    )
    assert chosen == TWO_TUESDAY_SLOTS[1]


# --- real inbox patterns (verbatim from Kory's audited inbox) ---


def test_calendar_invitation_subject_parses_correct_hour():
    # Live 24h-audit artifact: this exact subject once parsed as 2:00 AM.
    ref = datetime(2026, 6, 30, 9, 0, tzinfo=MT)
    cands = extract_inbound_time_candidates(
        "Invitation: Asher/Daybreak @ Thu Jul 2, 2026 9:30am - 10am (MDT) "
        "(kory.mitchell@iconicfounders.com)",
        reference=ref,
    )
    assert _starts(cands) == ["2026-07-02T09:30:00-06:00"]


def test_ramzi_lets_go_with_pattern():
    ref = datetime(2026, 6, 30, 9, 0, tzinfo=MT)
    cands = extract_inbound_time_candidates(
        "Let's go with Tuesday 7/7 at 3 pm.", reference=ref
    )
    assert _starts(cands) == ["2026-07-07T15:00:00-06:00"]


def test_past_meeting_reference_is_not_a_candidate():
    # Audit B9: "great talking Monday at 3" made next Monday 3pm bookable.
    ref = datetime(2026, 6, 30, 9, 0, tzinfo=MT)
    cands = extract_inbound_time_candidates(
        "great talking Monday at 3, how about Friday at 2?", reference=ref
    )
    assert _starts(cands) == ["2026-07-03T14:00:00-06:00"]


def test_thanks_sentence_boundary_keeps_real_ask():
    ref = datetime(2026, 6, 30, 9, 0, tzinfo=MT)
    cands = extract_inbound_time_candidates(
        "Thanks for the intro! Could we do Monday at 10?", reference=ref
    )
    assert _starts(cands) == ["2026-07-06T10:00:00-06:00"]


def test_wrapped_gmail_attribution_is_not_mined():
    # Audit B9: Lexi could mine her OWN quoted offer as the counterpart's
    # proposal when Gmail wrapped the attribution across two lines.
    ref = datetime(2026, 7, 25, 9, 0, tzinfo=MT)
    body = (
        "Sounds good!\n\n"
        "On Thu, Jul 24, 2026 at 3:30 PM Lexi Knightly\n"
        "<lexi@iconicfounders.com> wrote:\n"
        "> • Monday, August 17 at 10:30 AM MT\n"
    )
    assert extract_inbound_time_candidates(body, reference=ref) == []
