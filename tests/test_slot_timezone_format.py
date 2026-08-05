"""Slot line formatting — MT-only when timezone unknown."""

from __future__ import annotations

from app.scheduling.email_format import (
    format_offer_slot_block,
    format_slot_for_email,
    lexi_unknown_timezone_note,
    should_note_mt_only_timezone,
    should_use_us_equivalent_slot_format,
)
from app.scheduling.hermes_compose import _enforce_offered_times_block


def test_should_not_use_us_equivalents_anymore():
    assert not should_use_us_equivalent_slot_format(
        sender_email="prospect@gmail.com",
        uncertain=True,
    )
    assert not should_use_us_equivalent_slot_format(
        sender_email="anjana.kummetha@iconicfounders.com",
        uncertain=False,
        tz_confidence="inferred",
        tz_source="internal_default",
        intent="referral_or_intro",
        meeting_format="virtual",
    )


def test_should_note_mt_only_for_unknown_external():
    assert should_note_mt_only_timezone(
        sender_email="prospect@gmail.com",
        uncertain=True,
    )
    assert not should_note_mt_only_timezone(
        sender_email="anjana.kummetha@iconicfounders.com",
        uncertain=False,
        tz_confidence="known",
        tz_source="body",
    )


def test_mt_only_slot_line_has_no_us_parentheticals():
    from app.config import settings
    from zoneinfo import ZoneInfo

    slot = {
        "start": "2026-07-07T15:00:00+00:00",
        "end": "2026-07-07T15:30:00+00:00",
    }
    mt = ZoneInfo(settings.scheduling_timezone)
    line = format_slot_for_email(slot, recipient_tz=mt)
    assert "MT" in line
    assert "ET" not in line
    assert "CT" not in line
    assert "PT" not in line


def test_lexi_unknown_timezone_note():
    note = lexi_unknown_timezone_note(voice_mode="lexi")
    assert "couldn't identify your time zone" in note
    assert "Mountain Time" in note


def test_enforce_offered_times_block_replaces_bullets():
    draft = (
        "Hi Anju,\n\n"
        "Here are times:\n\n"
        "• Tuesday, July 7 at 9:00 AM–9:30 AM MT\n"
        "• Friday, July 10 at 9:00 AM–9:30 AM MT\n\n"
        "Thank you,\nLexi"
    )
    slot_block = format_offer_slot_block(
        [
            {"start": "2026-07-07T15:00:00+00:00", "end": "2026-07-07T15:30:00+00:00"},
            {"start": "2026-07-10T15:00:00+00:00", "end": "2026-07-10T15:30:00+00:00"},
        ],
        recipient_tz=__import__("zoneinfo").ZoneInfo("America/Denver"),
    )
    fixed = _enforce_offered_times_block(draft, slot_block)
    assert "11:00 AM" not in fixed
    assert "• Tuesday" in fixed
    assert "MT" in fixed


def test_fallback_offer_template_is_voice_aware():
    """Timeout-fallback offers must not speak as Lexi on Kory-voice proposals
    (live defect: #6497 said "on Kory's end" then signed "Let's Win, Kory")."""
    from app.scheduling.hermes_compose import _template_fallback_offer

    slot_block = "• Wednesday, August 26 at 9:30 AM–10:30 AM MT"

    kory = _template_fallback_offer("Anjana", slot_block, "kory")
    assert "Kory's end" not in kory
    assert "I'm Lexi" not in kory
    assert "I have a few times that work:" in kory

    lexi = _template_fallback_offer("Anjana", slot_block, "lexi")
    assert "on Kory's end" in lexi
    assert "I'm Lexi, Kory's assistant" in lexi


def test_prior_thread_signature_outranks_newer_thread_headers(monkeypatch):
    """Live G-3 defect: the newest prior thread had no signature, so its Gmail
    Date header (-0600, the sender's physical location) answered before the
    older thread's explicit NY signature was ever scanned — the learned
    Eastern timezone was lost and the offer went out MT-only."""
    import json as json_mod

    from app.scheduling import timezone_intel as tzi

    threads = [
        {  # newest: bare follow-up, no signature, Denver-offset headers
            "thread_id": "t-new",
            "raw_body": "Hi Kory,\n\nCould we look at the week after next instead?\n\nAnjana",
            "internet_headers_json": json_mod.dumps(
                [{"name": "Date", "value": "Tue, 4 Aug 2026 21:20:00 -0600"}]
            ),
        },
        {  # older: carries the NY signature
            "thread_id": "t-old",
            "raw_body": (
                "Hi Kory,\n\nWould love to grab 30 minutes next week.\n\n"
                "Best,\nAnjana Kummetha\nNew York, NY | (212) 555-0100"
            ),
            "internet_headers_json": json_mod.dumps(
                [{"name": "Date", "value": "Tue, 4 Aug 2026 20:00:00 -0600"}]
            ),
        },
    ]
    monkeypatch.setattr(
        "app.storage.recipient_profiles.list_prior_email_threads",
        lambda sender_email, exclude_thread_id=None: threads,
    )
    result = tzi.detect_recipient_timezone(
        sender_email="anjanakummetha@gmail.com",
        body="Hi Kory,\n\nOne more thought.\n\nAnjana",
        allow_prior_threads=True,
    )
    assert result.source == "prior_email_signature"
    assert result.tz_name() == "America/New_York"
