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
