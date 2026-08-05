"""Outlook draft creation for Lexi HTML sends."""

from __future__ import annotations

from unittest.mock import patch

from app.integrations.outlook_email import (
    _build_outlook_draft_arguments,
    _send_lexi_html_via_draft,
    infer_outbound_send_channel,
)


def test_build_outlook_draft_arguments_omits_empty_cc():
    with patch("app.integrations.outlook_email.merge_kory_cc_addresses", return_value=[]):
        args = _build_outlook_draft_arguments(
            recipient="guest@example.com",
            subject="Hello",
            body="<p>Hi</p>",
            is_html=True,
        )
    assert "cc_recipients" not in args
    assert args["to_recipients"] == ["guest@example.com"]


def test_build_outlook_draft_arguments_includes_cc_when_configured():
    with patch(
        "app.integrations.outlook_email.merge_kory_cc_addresses",
        return_value=["kory@ifg.vc"],
    ):
        args = _build_outlook_draft_arguments(
            recipient="guest@example.com",
            subject="Hello",
            body="<p>Hi</p>",
            is_html=True,
        )
    assert args["cc_recipients"] == ["kory@ifg.vc"]


def test_send_lexi_html_draft_omits_empty_cc_recipients():
    calls: list[tuple[str, dict]] = []

    def mock_execute(tool: str, args: dict, *, role: str):
        calls.append((tool, args))
        if tool == "OUTLOOK_CREATE_DRAFT":
            return {"data": {"id": "draft-1"}, "log_id": "log-create"}
        return {"data": {"status_code": 202}, "log_id": "log-send"}

    with patch("app.integrations.outlook_email.execute_tool", side_effect=mock_execute):
        with patch("app.integrations.outlook_email.merge_kory_cc_addresses", return_value=[]):
            message_id, _ = _send_lexi_html_via_draft(
                recipient="guest@example.com",
                subject="TEST",
                html_body="<p>Hi</p>",
                inline_attachment={
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": "logo.png",
                    "contentType": "image/png",
                    "contentBytes": "abc",
                    "contentId": "logo",
                },
                write_role="lexi",
            )
    assert message_id == "draft-1"
    draft_tool, draft_args = calls[0]
    assert draft_tool == "OUTLOOK_CREATE_DRAFT"
    assert "cc_recipients" not in draft_args


def test_infer_outbound_send_channel_from_kory_signoff():
    body = "test sending emails working.\n\nLet's Win,\nKory"
    assert infer_outbound_send_channel(body) == "kory"


def test_infer_outbound_send_channel_explicit_lexi():
    body = "Let's Win,\nKory"
    assert infer_outbound_send_channel(body, explicit="lexi") == "lexi"


def test_infer_channel_from_bare_kory_signoff():
    """Live O-2a defect: Hermes drafts sign a bare 'Kory' before the finalizer
    stamps 'Let's Win,' — requiring the canonical block routed Kory-voice mail
    to the Lexi mailbox (identity mismatch + slips the kory-channel block)."""
    body = (
        "Hey Anjana,\n\nI'll be in town this Thursday (August 6) and would "
        "love to catch up.\n\nLooking forward to it!\n\nKory"
    )
    assert infer_outbound_send_channel(body) == "kory"


def test_infer_channel_from_casual_kory_signoff():
    assert infer_outbound_send_channel("Quick note.\n\nThanks,\nKory") == "kory"
    assert infer_outbound_send_channel("Quick note.\n\n- Kory Mitchell") == "kory"


def test_infer_channel_from_lexi_signoff():
    body = (
        "Hi Anjana,\n\nI'm Lexi, Kory's assistant. Kory asked me to share his "
        "availability.\n\nBest,\nLexi"
    )
    assert infer_outbound_send_channel(body) == "lexi"


def test_infer_channel_kory_mention_midline_is_not_a_signoff():
    body = "Hi,\n\nKory asked me to reach out about next week.\n\nBest,\nLexi"
    assert infer_outbound_send_channel(body) == "lexi"


def test_infer_channel_no_signoff_defaults_lexi():
    assert infer_outbound_send_channel("Hello there, quick question.") == "lexi"
