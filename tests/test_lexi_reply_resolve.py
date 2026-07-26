"""Lexi delegation reply — resolve Kory message ids to Lexi mailbox."""

from __future__ import annotations

from unittest.mock import patch

from app.integrations.outlook_email import (
    _pick_lexi_delegation_anchor,
    create_draft_reply,
    resolve_lexi_reply_message_id,
)


def test_resolve_lexi_reply_message_id_picks_kory_delegation_anchor():
    kory_id = "kory-msg-id"
    lexi_wrong_id = "lexi-wrong-reply"
    kory_delegation_id = "kory-delegation-id"
    conv = "conv-123"
    fake_messages = [
        {
            "id": lexi_wrong_id,
            "subject": "Re: TEST",
            "from": {"emailAddress": {"address": "lexi@iconicfounders.com"}},
            "toRecipients": [{"emailAddress": {"address": "kory.mitchell@iconicfounders.com"}}],
            "receivedDateTime": "2026-06-25T09:49:00Z",
        },
        {
            "id": kory_delegation_id,
            "subject": "Re: TEST",
            "from": {"emailAddress": {"address": "kory.mitchell@iconicfounders.com"}},
            "toRecipients": [{"emailAddress": {"address": "anjana.kummetha@iconicfounders.com"}}],
            "ccRecipients": [{"emailAddress": {"address": "lexi@iconicfounders.com"}}],
            "receivedDateTime": "2026-06-23T20:51:00Z",
        },
    ]

    with patch("app.integrations.outlook_email.settings") as mock_settings:
        mock_settings.lexi_dry_run = False  # exercise the real resolve path, not the dry-run stub
        mock_settings.kory_sender_emails = ("kory.mitchell@iconicfounders.com",)
        mock_settings.lexi_mailbox_email = "lexi@iconicfounders.com"
        with patch("app.integrations.composio_client.execute_tool") as mock_exec:
            mock_exec.return_value = {"data": {"value": fake_messages}}
            resolved = resolve_lexi_reply_message_id(
                kory_id,
                conversation_id=conv,
                intended_recipient="anjana.kummetha@iconicfounders.com",
            )
        assert resolved == kory_delegation_id
        # Kory's copy is read first for its internetMessageId (stable across
        # mailboxes), then Lexi's inbox is listed for the anchor.
        assert mock_exec.call_count == 2
        first, second = mock_exec.call_args_list
        assert first.args[0] == "OUTLOOK_GET_MESSAGE"
        assert "internetMessageId" in first.args[1].get("select", [])
        assert second.args[0] == "OUTLOOK_LIST_MESSAGES"
        assert second.kwargs.get("role") == "lexi"
        assert conv in str(second.args[1].get("filter", ""))


def test_pick_lexi_delegation_anchor_prefers_kory_to_external():
    messages = [
        {
            "id": "lexi-bad",
            "from": {"emailAddress": {"address": "lexi@iconicfounders.com"}},
            "toRecipients": [{"emailAddress": {"address": "kory.mitchell@iconicfounders.com"}}],
            "receivedDateTime": "2026-06-25T09:49:00Z",
        },
        {
            "id": "kory-good",
            "from": {"emailAddress": {"address": "kory.mitchell@iconicfounders.com"}},
            "toRecipients": [{"emailAddress": {"address": "anjana.kummetha@iconicfounders.com"}}],
            "receivedDateTime": "2026-06-23T20:51:00Z",
        },
    ]
    with patch("app.integrations.outlook_email.settings") as mock_settings:
        mock_settings.lexi_dry_run = False  # exercise the real resolve path, not the dry-run stub
        mock_settings.kory_sender_emails = ("kory.mitchell@iconicfounders.com",)
        mock_settings.lexi_mailbox_email = "lexi@iconicfounders.com"
        anchor = _pick_lexi_delegation_anchor(
            messages,
            intended_recipient="anjana.kummetha@iconicfounders.com",
        )
    assert anchor is not None
    assert anchor["id"] == "kory-good"


def test_create_draft_reply_lexi_uses_reply_all_and_html_update():
    with patch("app.integrations.outlook_email.settings") as mock_settings:
        mock_settings.lexi_dry_run = False
        mock_settings.lexi_write_mode = "production"
        mock_settings.sandbox_email_loopback = False
        mock_settings.cc_kory_enabled = True
        mock_settings.kory_cc_email = "kory.mitchell@iconicfounders.com"
        mock_settings.hubspot_bcc_enabled = False
        with patch("app.integrations.outlook_email.execute_tool") as mock_exec:
            mock_exec.side_effect = [
                {"data": {"id": "draft-123"}, "log_id": "log-create"},
                {"data": {}, "log_id": "log-update"},
                # ensure_kory_cc reads the draft's recipients (Kory not on thread here).
                {"data": {"toRecipients": [{"emailAddress": {"address": "guest@example.com"}}],
                          "ccRecipients": []}, "log_id": "log-get"},
                {"data": {}, "log_id": "log-kory-cc"},
            ]
            with patch(
                "app.scheduling.lexi_html_signature.lexi_html_signature_enabled",
                return_value=True,
            ):
                with patch(
                    "app.scheduling.lexi_html_signature.lexi_html_email_package",
                    return_value=("<p>Hi</p>", [], False),
                ):
                    draft_id, log_id = create_draft_reply(
                        "anchor-msg",
                        "Hi Anju,\n\n• Slot one",
                        send_channel="lexi",
                    )
    assert draft_id == "draft-123"
    assert log_id == "log-create"
    # create reply-all + update body + read-recipients (thread check) + kory-cc update
    assert mock_exec.call_count == 4
    create_call = mock_exec.call_args_list[0]
    assert create_call.args[0] == "OUTLOOK_CREATE_REPLY_ALL_DRAFT"
    update_call = mock_exec.call_args_list[1]
    assert update_call.args[0] == "OUTLOOK_UPDATE_USER_MAIL_FOLDER_MESSAGE"
    body = update_call.args[1]["body"]
    assert body["contentType"] == "html"
    assert "Hi" in body["content"]
    get_call = mock_exec.call_args_list[2]
    assert get_call.args[0] == "OUTLOOK_GET_MESSAGE"  # thread-membership read
    cc_call = mock_exec.call_args_list[3]
    assert cc_call.args[0] == "OUTLOOK_UPDATE_USER_MAIL_FOLDER_MESSAGE"
    assert "cc_recipients" in cc_call.args[1]
