"""IFG HTML email signature for Lexi outbound mail."""

import os

from app.scheduling.lexi_html_signature import (
    build_lexi_html_email,
    build_lexi_html_signature_block,
    build_lexi_inline_logo_attachment,
    lexi_html_signature_enabled,
)


def test_html_signature_enabled_by_default() -> None:
    old = os.environ.pop("LEXI_HTML_SIGNATURE_ENABLED", None)
    try:
        assert lexi_html_signature_enabled() is True
    finally:
        if old is not None:
            os.environ["LEXI_HTML_SIGNATURE_ENABLED"] = old


def test_build_lexi_html_email_has_logo_by_default(monkeypatch) -> None:
    # Logo embeds by default (inline CID attachment); identity is Lexi Knightly / Executive Assistant.
    monkeypatch.delenv("LEXI_SIGNATURE_EMBED_LOGO", raising=False)
    monkeypatch.delenv("LEXI_SIGNATURE_LOGO_URL", raising=False)
    html = build_lexi_html_email("Hi,\n\nThursday at 2pm works.")
    assert "Lexi Knightly" in html
    assert "Executive Assistant" in html
    assert "Assistant to Kory Mitchell" not in html
    assert "Iconic Founders Group" in html
    assert "lexi@iconicfounders.com" in html
    assert "Thank you," in html
    assert "<table" in html
    assert "cid:ifg-logo.png" in html


def test_logo_img_never_forces_square_height(monkeypatch) -> None:
    # The 1024x678 asset must scale on width only — a forced height distorts it.
    monkeypatch.delenv("LEXI_SIGNATURE_EMBED_LOGO", raising=False)
    block = build_lexi_html_signature_block(use_cid=True)
    assert "<img" in block
    assert 'height="' not in block
    assert "height:auto" in block


def test_inline_logo_attachment_enabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("LEXI_SIGNATURE_EMBED_LOGO", raising=False)
    attachment = build_lexi_inline_logo_attachment()
    assert attachment is not None
    assert attachment["contentId"] == "ifg-logo.png"
    assert attachment["isInline"] is True
    assert attachment["contentBytes"]


def test_inline_logo_attachment_disabled_by_env(monkeypatch) -> None:
    monkeypatch.setenv("LEXI_SIGNATURE_EMBED_LOGO", "false")
    monkeypatch.delenv("LEXI_SIGNATURE_LOGO_URL", raising=False)
    assert build_lexi_inline_logo_attachment() is None


def test_signature_block_single_column_when_logo_disabled(monkeypatch) -> None:
    monkeypatch.setenv("LEXI_SIGNATURE_EMBED_LOGO", "false")
    monkeypatch.delenv("LEXI_SIGNATURE_LOGO_URL", raising=False)
    block = build_lexi_html_signature_block(use_cid=True)
    assert "cid:ifg-logo.png" not in block
    assert "border-left:1px solid" not in block
    assert "Lexi Knightly" in block
    assert "Executive Assistant" in block


def test_signature_block_two_column_by_default(monkeypatch) -> None:
    monkeypatch.delenv("LEXI_SIGNATURE_EMBED_LOGO", raising=False)
    block = build_lexi_html_signature_block(use_cid=True)
    assert "border-left:1px solid" in block
    assert "cid:ifg-logo.png" in block
