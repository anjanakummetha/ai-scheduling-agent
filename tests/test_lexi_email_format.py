"""Lexi assistant sign-off formatting."""

from app.scheduling.email_format import finalize_lexi_email_body
from app.safety.operation_verify import re_search_lexi_signoff, verify_draft_reply


def test_lexi_signoff_block() -> None:
    body = finalize_lexi_email_body("Hi,\n\nA few times that work:\n\n• Tuesday 2pm MT")
    assert body.endswith(
        "Thank you,\nLexi Knightly\nExecutive Assistant\nlexi@iconicfounders.com"
    )
    # A bare "Hi," opener is intentionally dropped by _dedupe_lexi_opening; the
    # scheduling content and bullet are preserved.
    assert "A few times that work:" in body
    assert "• Tuesday 2pm MT" in body


def test_lexi_replaces_old_best_closing() -> None:
    body = finalize_lexi_email_body(
        "Hi,\n\nThanks.\n\nBest,\nLexi"
    )
    assert "Best," not in body
    assert re_search_lexi_signoff(body)


def test_lexi_verify() -> None:
    body = finalize_lexi_email_body("Quick note.")
    result = verify_draft_reply(body, voice_mode="lexi")
    assert result.ok


def test_lexi_dedupes_double_signoff() -> None:
    from app.scheduling.lexi_voice import LEXI_SIGNOFF_BLOCK

    doubled = f"Hi,\n\nThanks.\n\n{LEXI_SIGNOFF_BLOCK}\n\n{LEXI_SIGNOFF_BLOCK}"
    body = finalize_lexi_email_body(doubled)
    assert body.count(LEXI_SIGNOFF_BLOCK) == 1
    assert body.count("lexi@iconicfounders.com") == 1


def test_lexi_strips_outlook_rich_signature_before_append() -> None:
    body = finalize_lexi_email_body(
        "Hi,\n\nThursday works.\n\nSee amazing founders at iconicfounders.com\n"
        "Kory Mitchell - CEO\nDenver, Colorado"
    )
    assert "See amazing founders" not in body
    assert body.endswith("lexi@iconicfounders.com")
