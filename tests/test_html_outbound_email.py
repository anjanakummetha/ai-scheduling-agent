"""HTML outbound mail — the morning briefing is markup, not a person-to-person note."""

from __future__ import annotations

from unittest.mock import patch

from app.integrations.outlook_email import send_outbound_email

HTML = '<div style="font-family:Arial;"><h3>Key insights</h3><ul><li>One</li></ul></div>'


def _settings(mock):
    mock.lexi_dry_run = False
    mock.lexi_write_mode = "kory"
    mock.sandbox_email_loopback = False
    mock.sandbox_mailbox_email = ""
    mock.cc_kory_enabled = False
    mock.hubspot_bcc_enabled = False
    mock.lexi_require_kory_approval = True
    return mock


def _send(**kwargs):
    with patch("app.integrations.outlook_email.settings") as settings, patch(
        "app.safety.approval_gate.kory_outbound_email_blocked", return_value=False
    ), patch("app.safety.approval_gate.assert_outbound_send_authorized"):
        _settings(settings)
        with patch("app.integrations.outlook_email.execute_tool") as execute:
            execute.return_value = {"data": {"id": "msg-1"}, "log_id": "log-1"}
            send_outbound_email(
                to_email="kory.mitchell@iconicfounders.com",
                subject="CEO Daily Briefing",
                approved_send=True,
                **kwargs,
            )
    return execute.call_args.args[1]


def test_html_body_is_sent_as_html():
    """Only the Lexi channel set is_html, so this arrived as visible markup."""
    args = _send(body=HTML, html_body=True)
    assert args["is_html"] is True
    assert args["body"] == HTML, "markup must be sent through untouched"


def test_html_body_skips_the_plain_text_signoff():
    """finalize_outbound_email_body appends "Let's Win, / Kory" — wrong for a briefing."""
    args = _send(body=HTML, html_body=True)
    assert "Let's Win" not in args["body"]
    assert args["body"].startswith("<div")


def test_plain_text_sending_is_unchanged_by_default():
    args = _send(body="Hi Anju,\n\nHere are three times.")
    assert args["is_html"] is False
    assert "Hi Anju" in args["body"]
