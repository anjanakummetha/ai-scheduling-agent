"""Kory's branded signature on mail sent from his own mailbox.

Kory-channel mail went out as plain text with a bare "Let's Win, / Kory" while
Lexi-channel mail carried a full branded block. These pin the block he actually
signs with: sign-off, podcast line linking to the real site, logo, and contact
details — on both fresh sends and replies.
"""

from unittest.mock import patch

from app.scheduling.kory_html_signature import (
    _strip_kory_plain_signoff,
    build_kory_html_email,
    build_kory_html_signature_block,
    kory_html_email_package,
)

BODY = "Hi Anjana,\n\nTuesday at 2:00 works on my end.\n\nLet's Win,\nKory"


def test_signature_carries_every_line_kory_signs_with():
    html = build_kory_html_signature_block(use_cid=True)
    assert "Let&#x27;s Win!" in html or "Let's Win!" in html
    assert "See amazing founders who sold their businesses on my podcast The Turn" in html
    assert "Kory Mitchell - CEO" in html
    assert "Iconic Founders Group" in html
    assert "Denver, Colorado" in html
    assert "M: 720-561-0611" in html


def test_the_two_links_go_to_different_places():
    """The podcast line points at the podcast; the company line at the company.

    Both pointed at iconicfounders.com first time round, which sent anyone
    following the podcast line to the wrong site.
    """
    html = build_kory_html_signature_block(use_cid=True)
    assert html.count('href="https://www.theturnpodcast.com/"') == 1
    assert html.count('href="https://www.iconicfounders.com/"') == 1
    assert "example.com" not in html


def test_podcast_line_is_italic_and_links_the_podcast_site():
    html = build_kory_html_signature_block(use_cid=True)
    start = html.index("<em>")
    end = html.index("</em>")
    podcast = html[start:end]
    assert "The Turn Podcast</a>" in podcast
    assert "https://www.theturnpodcast.com/" in podcast
    assert "iconicfounders.com" not in podcast, "podcast line must not link the company site"
    # The line ends on the link — nothing trails it.
    assert "and all podcast channels" not in podcast
    assert podcast.rstrip().endswith("The Turn Podcast</a>")


def test_company_line_still_links_the_company():
    html = build_kory_html_signature_block(use_cid=True)
    contact_start = html.index("Kory Mitchell - CEO")
    contact = html[contact_start:]
    assert 'href="https://www.iconicfounders.com/"' in contact
    assert "Iconic Founders Group</a>" in contact


def test_logo_is_referenced_by_cid_and_scaled_on_width_only():
    """A forced square distorts the 1024x678 asset — width-only keeps it true."""
    html = build_kory_html_signature_block(use_cid=True)
    assert 'src="cid:ifg-logo.png"' in html
    assert "height:auto" in html
    assert "height:150px" not in html


def test_plain_signoff_is_replaced_not_duplicated():
    html = build_kory_html_email(BODY, use_cid=True)
    # The body's own "Let's Win,\nKory" must not survive alongside the block.
    assert "Let&#x27;s Win,<br>Kory" not in html
    assert html.count("Kory Mitchell - CEO") == 1
    assert "Tuesday at 2:00 works on my end." in html


def test_strips_every_common_signoff_spelling():
    for closing in ["Let's Win,\nKory", "Lets Win!\nKory", "Thanks,\nKory Mitchell",
                    "Best,\nKory", "Regards,\nKory"]:
        stripped = _strip_kory_plain_signoff(f"Body text here.\n\n{closing}")
        assert stripped == "Body text here.", closing


def test_body_without_a_signoff_is_left_alone():
    assert _strip_kory_plain_signoff("Just the body.") == "Just the body."


def test_package_requires_draft_send_when_logo_is_embedded():
    """Gmail drops data: URIs, so an embedded logo forces draft->attach->send."""
    html, attachments, needs_draft = kory_html_email_package(BODY)
    assert needs_draft is True
    assert len(attachments) == 1
    assert attachments[0]["contentId"] == "ifg-logo.png"
    assert attachments[0]["isInline"] is True
    assert 'src="cid:ifg-logo.png"' in html


def test_hosted_logo_sends_directly_without_an_attachment(monkeypatch):
    """A hosted URL needs no attachment, so the send skips the draft round-trip."""
    monkeypatch.setenv("LEXI_SIGNATURE_LOGO_URL", "https://cdn.example.org/ifg-logo.png")
    html, attachments, needs_draft = kory_html_email_package(BODY)
    assert needs_draft is False and attachments == []
    assert "cid:" not in html
    assert 'src="https://cdn.example.org/ifg-logo.png"' in html


def test_signature_still_renders_without_a_logo():
    with patch("app.scheduling.kory_html_signature.resolve_lexi_signature_logo_src",
               return_value=None):
        html = build_kory_html_signature_block(use_cid=True)
    assert "<img" not in html
    assert "Kory Mitchell - CEO" in html
    assert "M: 720-561-0611" in html


def test_disable_flag_is_honoured(monkeypatch):
    from app.scheduling import kory_html_signature as mod

    monkeypatch.setenv("LEXI_KORY_HTML_SIGNATURE_ENABLED", "false")
    assert mod.kory_html_signature_enabled() is False
    monkeypatch.setenv("LEXI_KORY_HTML_SIGNATURE_ENABLED", "true")
    assert mod.kory_html_signature_enabled() is True


def test_kory_send_path_applies_the_signature(monkeypatch):
    """The wiring, not just the builder: a kory-channel send must go out as HTML.

    Settings are patched on the module rather than mutated globally: several
    tests call importlib.reload(app.config), which builds a fresh Settings while
    outlook_email keeps a reference to the old one — so mutating app.config
    .settings here would silently not reach the code under test.
    """
    from app.integrations import outlook_email

    # Pin the logo config rather than inheriting ambient env — a hosted URL or a
    # disabled logo both legitimately skip the attachment, and this test is about
    # the default embedded-CID route.
    monkeypatch.delenv("LEXI_SIGNATURE_LOGO_URL", raising=False)
    monkeypatch.delenv("LEXI_SIGNATURE_EMBED_LOGO", raising=False)

    captured = {}

    def fake_draft_send(*, recipient, subject, html_body, inline_attachment, write_role):
        captured.update(html=html_body, role=write_role, cid=inline_attachment["contentId"])
        return "msg-1", "log-1"

    with (
        patch.object(outlook_email, "settings") as settings,
        patch.object(outlook_email, "_send_lexi_html_via_draft", side_effect=fake_draft_send),
        patch("app.safety.approval_gate.kory_outbound_email_blocked", return_value=False),
        patch("app.safety.approval_gate.assert_outbound_send_authorized"),
    ):
        settings.lexi_dry_run = False
        settings.lexi_write_mode = "kory"
        settings.sandbox_email_loopback = False
        settings.sandbox_mailbox_email = ""
        settings.cc_kory_enabled = False
        settings.hubspot_bcc_enabled = False
        settings.kory_cc_email = "kory.mitchell@iconicfounders.com"
        settings.kory_sender_emails = ("kory@ifg.vc", "kory@iconicfounders.com")
        outlook_email.send_outbound_email(
            to_email="anjanakummetha@gmail.com",
            subject="Test",
            body=BODY,
            approved_send=True,
            send_channel="kory",
        )

    assert captured["role"] == "write"          # Kory's mailbox, not Lexi's
    assert captured["cid"] == "ifg-logo.png"
    assert "Kory Mitchell - CEO" in captured["html"]
    assert "www.iconicfounders.com" in captured["html"]


def test_mail_addressed_to_kory_himself_is_not_signed(monkeypatch):
    """A briefing sent TO Kory is a system notification, not something he sent."""
    from app.integrations import outlook_email

    with (
        patch.object(outlook_email, "settings") as settings,
        patch.object(outlook_email, "execute_tool") as execute,
        patch("app.safety.approval_gate.kory_outbound_email_blocked", return_value=False),
        patch("app.safety.approval_gate.assert_outbound_send_authorized"),
    ):
        settings.lexi_dry_run = False
        settings.lexi_write_mode = "kory"
        settings.sandbox_email_loopback = False
        settings.sandbox_mailbox_email = ""
        settings.cc_kory_enabled = False
        settings.hubspot_bcc_enabled = False
        settings.kory_cc_email = "kory.mitchell@iconicfounders.com"
        settings.kory_sender_emails = ("kory@ifg.vc", "kory@iconicfounders.com")
        execute.return_value = {"data": {"id": "msg-1"}, "log_id": "log-1"}
        outlook_email.send_outbound_email(
            to_email="kory.mitchell@iconicfounders.com",
            subject="CEO Daily Briefing",
            body="Here is your day.",
            approved_send=True,
            send_channel="kory",
        )

    args = execute.call_args.args[1]
    assert args["is_html"] is False
    assert "Kory Mitchell - CEO" not in args["body"]


def test_lexi_channel_is_untouched():
    """Lexi's own signature must not be replaced by Kory's."""
    from app.scheduling.lexi_html_signature import build_lexi_html_signature_block

    html = build_lexi_html_signature_block(use_cid=True)
    assert "Lexi Knightly" in html
    assert "Kory Mitchell - CEO" not in html
