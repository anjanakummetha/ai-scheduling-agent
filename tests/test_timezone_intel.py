"""Tests for recipient timezone intelligence."""

import json

from app.scheduling.timezone_intel import detect_recipient_timezone


def test_domain_known_timezone():
    result = detect_recipient_timezone(sender_email="bill@newportadvisors.co")
    assert result.confidence == "known"
    assert result.timezone is not None
    assert "New_York" in str(result.timezone)


def test_unknown_without_signals_returns_unknown():
    result = detect_recipient_timezone(sender_email="person@unknown-startup.io", body="Thanks!")
    assert result.confidence == "unknown"
    assert result.source == "unknown"
    assert result.timezone is None


def test_denver_based_in_body():
    result = detect_recipient_timezone(
        sender_email="prospect@example.com",
        body="I'm with a Denver-based family office.",
    )
    assert result.confidence == "known"
    assert result.source == "body"
    assert "Denver" in str(result.timezone)


def test_body_eastern_timezone():
    result = detect_recipient_timezone(
        sender_email="x@unknown.io",
        body="I'm in Eastern time — can we meet next week?",
    )
    assert result.confidence == "known"
    assert result.source == "body"


def test_internal_unknown_defaults_to_mountain():
    result = detect_recipient_timezone(sender_email="anjana.kummetha@iconicfounders.com")
    assert result.confidence in {"inferred", "known"}
    assert "Denver" in str(result.timezone)


def test_internal_uses_timezone_from_prior_email(tmp_path, monkeypatch):
    import importlib

    db_path = tmp_path / "lexi.db"
    monkeypatch.setenv("LEXI_DATABASE_PATH", str(db_path))
    import app.config

    importlib.reload(app.config)
    import app.storage.lexi_db as lexi_db_mod

    importlib.reload(lexi_db_mod)

    from app.storage.lexi_db import get_lexi_connection
    from app.storage.recipient_profiles import ensure_recipient_profiles_table

    with get_lexi_connection() as conn:
        conn.execute(
            """
            CREATE TABLE email_threads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id TEXT NOT NULL UNIQUE,
                subject TEXT,
                sender TEXT,
                received_at TEXT,
                raw_body TEXT,
                internet_headers_json TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO email_threads (thread_id, sender, received_at, raw_body)
            VALUES (?, ?, ?, ?)
            """,
            (
                "old-thread",
                "anjana.kummetha@iconicfounders.com",
                "2026-06-01T10:00:00",
                "I'm in Eastern time — let's sync next week.",
            ),
        )
        ensure_recipient_profiles_table(conn)
        conn.commit()

    result = detect_recipient_timezone(
        sender_email="anjana.kummetha@iconicfounders.com",
        body="Can we meet?",
        exclude_thread_id="new-thread",
    )
    assert result.source == "prior_email_body"
    assert "New_York" in str(result.timezone)


def test_internal_other_colleague_defaults_to_mountain():
    result = detect_recipient_timezone(sender_email="other.person@iconicfounders.com")
    assert result.confidence == "inferred"
    assert result.source == "internal_default"
    assert result.timezone is not None


def test_external_unknown_asks_in_draft():
    from app.scheduling.email_format import build_scheduling_reply

    draft = build_scheduling_reply(
        recipient_first_name="Sam",
        slots=[{"start": "2026-06-30T16:00:00-06:00", "end": "2026-06-30T16:30:00-06:00"}],
        sender_email="sam@unknown-startup.io",
        recipient_body="Thanks!",
        voice_mode="lexi",
    )
    lowered = draft.lower()
    assert "timezone unknown" in lowered or "mountain time only" in lowered
    assert "mountain time" in lowered
    assert "4:00" in draft


def test_area_code_in_signature():
    result = detect_recipient_timezone(
        sender_email="founder@startup.io",
        body="Thanks!\n\nJane Doe\nO: (512) 270-4805\nAustin, TX",
    )
    assert result.source in {"signature", "area_code", "chain_area_code"}
    assert "Chicago" in str(result.timezone)


def test_area_code_212_eastern():
    result = detect_recipient_timezone(
        sender_email="investor@vc.com",
        body="Let's connect.\n\nJohn\nM: 212-555-0199",
    )
    assert result.source in {"area_code", "chain_area_code"}
    assert "New_York" in str(result.timezone)


def test_uncertain_slot_format_includes_us_zones():
    from app.scheduling.email_format import format_slot_for_email_uncertain_us

    line = format_slot_for_email_uncertain_us(
        {"start": "2026-06-30T16:00:00-06:00", "end": "2026-06-30T16:30:00-06:00"}
    )
    assert "MT" in line
    assert "ET" in line and "CT" in line and "PT" in line
    assert "4:00 PM" in line
    assert "5:00 PM" in line


def test_internal_no_kory_tz_confirm_in_draft():
    from app.scheduling.email_format import build_scheduling_reply

    draft = build_scheduling_reply(
        recipient_first_name="Anju",
        slots=[{"start": "2026-06-30T16:00:00-06:00", "end": "2026-06-30T16:30:00-06:00"}],
        sender_email="anjana.kummetha@iconicfounders.com",
        recipient_body="Can we meet?",
        voice_mode="lexi",
    )
    assert "confirm recipient timezone" not in draft.lower()
    assert "what time zone are you in" not in draft.lower()


def test_date_header_offset():
    result = detect_recipient_timezone(
        sender_email="x@unknown.io",
        internet_headers=[
            {"name": "Date", "value": "Wed, 17 Jun 2026 14:30:00 -0400"},
        ],
    )
    assert result.confidence == "inferred"
    assert result.source == "header_date"
    assert "New_York" in str(result.timezone)


def test_received_header_not_used_for_timezone():
    """Received hops are relay servers — not trusted for sender TZ."""
    result = detect_recipient_timezone(
        sender_email="mike@constructioncpa.com",
        internet_headers=[
            {
                "name": "Received",
                "value": (
                    "from mail.sender.com (10.0.0.1) by mx.google.com; "
                    "Wed, 17 Jun 2026 13:30:00 -0500"
                ),
            },
            {
                "name": "Received",
                "value": "from outlook.office365.com; Wed, 17 Jun 2026 18:30:00 +0000",
            },
        ],
    )
    assert result.confidence == "unknown"
    assert result.source == "unknown"


def test_signature_city_timezone():
    result = detect_recipient_timezone(
        sender_email="mike@constructioncpa.com",
        body="Thanks!\n\nMike Smith\nChicago, IL 60601",
    )
    assert result.source == "signature"
    assert "Chicago" in str(result.timezone)


def test_quoted_kory_signature_does_not_infer_denver():
    body = (
        "Thanks for following up — any of those work.\n\n"
        "From: Kory Mitchell <kory@iconicfounders.com>\n"
        "Denver, Colorado\n"
    )
    result = detect_recipient_timezone(sender_email="cnbrymer@gmail.com", body=body)
    assert result.confidence == "unknown"
    assert result.source == "unknown"
    assert result.timezone is None


def test_domain_wins_over_quoted_colorado():
    body = (
        "Sounds good.\n\n"
        "From: Kory Mitchell <kory@iconicfounders.com>\n"
        "Denver, Colorado\n"
    )
    result = detect_recipient_timezone(sender_email="bill@newportadvisors.co", body=body)
    assert result.source == "domain"
    assert "New_York" in str(result.timezone)


def test_utc_date_header_internal_defaults_mt():
    """Exchange-normalized +0000 Date headers must not infer Europe/London."""
    result = detect_recipient_timezone(
        sender_email="other.person@iconicfounders.com",
        internet_headers=[
            {"name": "Date", "value": "Tue, 23 Jun 2026 16:38:37 +0000"},
        ],
    )
    assert result.confidence == "inferred"
    assert result.source == "internal_default"
    assert "Denver" in str(result.timezone)


def test_time_with_zone_abbreviation_beats_area_code():
    """"6 AM ET" is explicit; it was losing to a prior thread's area code."""
    from app.scheduling.timezone_intel import _timezone_from_body

    for text, zone in [
        ("Could we do 6 AM ET before my day starts?", "America/New_York"),
        ("How about 2:30pm EST?", "America/New_York"),
        ("I could do 9 a.m. PT", "America/Los_Angeles"),
        ("free after 3 PM CT", "America/Chicago"),
    ]:
        result = _timezone_from_body(text)
        assert result is not None, text
        assert str(result.timezone) == zone, text


def test_stated_city_without_state_is_recognized():
    from app.scheduling.timezone_intel import _timezone_from_body

    assert str(_timezone_from_body("I am in Boston next month.").timezone) == "America/New_York"
    assert str(_timezone_from_body("We're based in Chicago.").timezone) == "America/Chicago"
    assert _timezone_from_body("Boston Consulting Group reached out.") is None
