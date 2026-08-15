"""Kory's 2026-08-11 feedback: offer emails arrived with every bullet on one
line. Root cause: _ensure_paragraph_spacing merged short, unpunctuated lines
into the previous one — which is what every bullet is. The LLM-composed drafts
put the lead-in and bullets in ONE block, so the whole list folded flat."""

from __future__ import annotations

from app.scheduling.email_format import (
    _ensure_paragraph_spacing,
    finalize_lexi_email_body,
)

_LLM_STYLE = (
    "Hi Heidi,\n\n"
    "I'd love to find a time to connect — I have a few times for a 30-minute "
    "check-in:\n"
    "• Tuesday, August 18 at 1:00–1:30 PM MT\n"
    "• Wednesday, August 19 at 9:00–9:30 AM MT\n"
    "• Thursday, August 20 at 2:00–2:30 PM MT\n\n"
    "Let me know what works!\n"
)


def test_bullets_after_leadin_stay_on_their_own_lines():
    out = _ensure_paragraph_spacing(_LLM_STYLE)
    lines = out.splitlines()
    bullets = [ln for ln in lines if ln.startswith("•")]
    assert len(bullets) == 3, out
    for b in bullets:
        assert b.count("•") == 1, f"bullets folded together: {b!r}"


def test_dash_and_numbered_bullets_survive():
    text = "Options:\n- first choice here\n- second choice here\n1. numbered too\n"
    out = _ensure_paragraph_spacing(text)
    assert "\n- first choice here\n- second choice here\n1. numbered too" in out


def test_finalize_keeps_bullets_and_is_idempotent():
    once = finalize_lexi_email_body(_LLM_STYLE)
    twice = finalize_lexi_email_body(once)
    assert once.count("•") == 3
    assert len([ln for ln in once.splitlines() if ln.strip().startswith("•")]) == 3
    # The Lexi channel runs finalize twice (send path + HTML build) — the
    # second pass must not degrade the first.
    assert [ln for ln in twice.splitlines() if ln.strip().startswith("•")] == [
        ln for ln in once.splitlines() if ln.strip().startswith("•")
    ]


def test_html_render_gives_each_bullet_its_own_line():
    from app.scheduling.lexi_html_signature import _plain_to_html_paragraphs

    html = _plain_to_html_paragraphs(_ensure_paragraph_spacing(_LLM_STYLE))
    # Bullets render as <li> items or <br>-separated lines — never one flat run.
    assert html.count("<li>") == 3 or html.count("<br>") >= 3


def test_short_prose_lines_still_merge():
    # The original merge behavior for hard-wrapped prose must survive:
    # short continuation lines without terminal punctuation join up.
    text = "This came from a\nhard wrapped client\nwith more to say\n"
    out = _ensure_paragraph_spacing(text)
    assert "This came from a hard wrapped client with more to say" in out
