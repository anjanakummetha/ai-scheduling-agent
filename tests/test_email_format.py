"""Email body formatting for Kory outbound drafts."""

from app.scheduling.email_format import finalize_outbound_email_body


def test_sign_off_on_separate_lines() -> None:
    body = finalize_outbound_email_body("Hi Dan,\n\nThanks.\n\nLet's Win, Kory")
    assert body.endswith("Let's Win,\nKory")
    assert "Let's Win, Kory" not in body.replace("Let's Win,\nKory", "")


def test_paragraph_spacing() -> None:
    body = finalize_outbound_email_body(
        "Hi Jane,\nFirst point here.\nSecond point here.\n\nThanks."
    )
    assert "First point here.\n\nSecond point here." in body
    assert body.endswith("Let's Win,\nKory")


def test_adds_sign_off_when_missing() -> None:
    body = finalize_outbound_email_body("Hi there,\n\nQuick note.")
    assert body.endswith("Let's Win,\nKory")


def test_bare_kory_ending_not_doubled():
    """Live L-1: a draft ending in a bare "Kory" got the canonical block
    appended AFTER it — "...Kory\\n\\nLet's Win,\\nKory"."""
    from app.scheduling.email_format import finalize_outbound_email_body

    out = finalize_outbound_email_body(
        "Hi Anjana,\n\nCherry Creek works great.\n\nKory"
    )
    assert out.endswith("Let's Win,\nKory")
    assert out.count("Kory") == 1


def test_casual_closer_before_kory_stripped():
    from app.scheduling.email_format import finalize_outbound_email_body

    out = finalize_outbound_email_body("Hi there,\n\nSounds good.\n\nThanks,\nKory")
    assert out.endswith("Let's Win,\nKory")
    assert "Thanks,\nKory" not in out


def test_outbound_reply_does_not_thank_for_reaching_out():
    """Live O-2b #6820: a cold outbound availability email opened with 'Thanks
    for reaching out' — the recipient never wrote to us."""
    from app.scheduling.email_format import build_scheduling_reply

    slots = [
        {"start": "2026-08-17T16:00:00+00:00", "end": "2026-08-17T17:00:00+00:00"},
        {"start": "2026-08-24T16:00:00+00:00", "end": "2026-08-24T17:00:00+00:00"},
    ]
    lexi = build_scheduling_reply(
        recipient_first_name="Anjana",
        slots=slots,
        sender_email="anjanakummetha@gmail.com",
        voice_mode="lexi",
        outbound=True,
    )
    assert "Thanks for reaching out" not in lexi
    assert "Kory asked me to reach out" in lexi

    kory = build_scheduling_reply(
        recipient_first_name="Anjana",
        slots=slots,
        sender_email="anjanakummetha@gmail.com",
        voice_mode="kory",
        outbound=True,
    )
    assert "Thanks for reaching out" not in kory

    inbound = build_scheduling_reply(
        recipient_first_name="Anjana",
        slots=slots,
        sender_email="anjanakummetha@gmail.com",
        voice_mode="lexi",
    )
    assert "Thanks for reaching out" in inbound
