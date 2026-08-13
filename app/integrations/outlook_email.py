"""Outlook email helpers backed by Composio tools (read Kory / write sandbox)."""

from __future__ import annotations

import html
import re
from typing import Any, Literal

import logging

from app.config import settings
from app.integrations.composio_client import (
    ComposioNotConfiguredError,
    ConnectionRole,
    execute_read_tool,
    execute_tool,
    execute_write_tool,
)
from app.integrations.outlook_profile import get_write_mailbox_email

logger = logging.getLogger(__name__)

SendChannel = Literal["kory", "lexi"]


def _sandbox_loopback_recipient(requested: str, *, send_channel: str = "kory") -> str:
    """In sandbox pilot, send to operator mailbox instead of external recipients."""
    if (send_channel or "kory").strip().lower() == "lexi":
        return requested.strip()
    if settings.lexi_write_mode != "sandbox" or not settings.sandbox_email_loopback:
        return requested.strip()

    configured = (settings.sandbox_mailbox_email or "").strip().lower()
    connected = (get_write_mailbox_email() or "").strip().lower()

    # Deliver to the Composio-connected mailbox so messages are visible in that account.
    # Cross-sending to a different @outlook.com login often never hits Inbox.
    if connected:
        if configured and configured != connected:
            logger.warning(
                "SANDBOX_MAILBOX_EMAIL=%s but Composio write connection is %s — "
                "loopback delivers to connected account. Reconnect Composio or update .env.",
                configured,
                connected,
            )
        return connected

    if configured:
        return configured
    return requested.strip()


def sandbox_mailbox_mismatch() -> dict[str, str | bool]:
    """True when .env mailbox does not match the Composio write connection."""
    configured = (settings.sandbox_mailbox_email or "").strip().lower()
    connected = (get_write_mailbox_email() or "").strip().lower()
    return {
        "configured": configured,
        "connected": connected,
        "mismatch": bool(configured and connected and configured != connected),
    }


def get_message(
    message_id: str,
    *,
    role: str = "read",
) -> tuple[dict[str, Any], str | None]:
    """Fetch a message. `role` must match the mailbox that produced the id —
    Graph ids are mailbox-scoped, so Kory's connection 404s on Lexi's ids."""
    from app.integrations.composio_client import execute_tool

    result = execute_tool(
        "OUTLOOK_GET_MESSAGE",
        {
            "message_id": message_id,
            "user_id": "me",
            "select": [
                "id",
                "subject",
                "from",
                "toRecipients",
                "ccRecipients",
                "bccRecipients",
                "receivedDateTime",
                "body",
                "bodyPreview",
                "internetMessageHeaders",
                "conversationId",
                # Stable across mailboxes — the only reliable way to find the
                # same email in Lexi's copy of a thread.
                "internetMessageId",
            ],
        },
        role=role,
    )
    return _coerce_data(result["data"]), result.get("log_id")


def _message_sender_email(message: dict[str, Any]) -> str:
    sender = message.get("from") or {}
    if isinstance(sender, dict):
        addr = (sender.get("emailAddress") or {}).get("address")
        return str(addr or "").strip().lower()
    return ""


def _message_to_emails(message: dict[str, Any]) -> list[str]:
    emails: list[str] = []
    for item in extract_recipient_list(message).get("to_recipients") or []:
        if isinstance(item, dict):
            addr = (item.get("emailAddress") or {}).get("address")
            if addr:
                emails.append(str(addr).strip().lower())
    return emails


def _is_kory_sender_email(email: str) -> bool:
    addr = (email or "").strip().lower()
    if not addr:
        return False
    return addr in {e.lower() for e in settings.kory_sender_emails}


def _pick_lexi_delegation_anchor(
    messages: list[dict[str, Any]],
    *,
    intended_recipient: str | None = None,
) -> dict[str, Any] | None:
    """Pick Kory's delegation message (Kory → external recipient) for reply-all."""
    intended = (intended_recipient or "").strip().lower()
    kory_delegation: list[dict[str, Any]] = []

    for message in messages:
        sender = _message_sender_email(message)
        if not _is_kory_sender_email(sender):
            continue
        if sender == (settings.lexi_mailbox_email or "").strip().lower():
            continue
        to_emails = _message_to_emails(message)
        if not to_emails:
            continue
        if intended and intended not in to_emails:
            continue
        if not intended and all(_is_kory_sender_email(addr) for addr in to_emails):
            continue
        kory_delegation.append(message)

    if kory_delegation:
        kory_delegation.sort(
            key=lambda item: str(item.get("receivedDateTime") or ""),
            reverse=True,
        )
        return kory_delegation[0]

    for message in messages:
        sender = _message_sender_email(message)
        if sender and not _is_kory_sender_email(sender):
            return message
    return messages[0] if messages else None


def _conversation_key(conversation_id: str) -> str:
    """Mailbox-independent part of a Graph conversationId.

    Graph prefixes conversationId per mailbox, so Kory's copy and Lexi's copy of
    the same thread compare unequal even though the trailing segment matches.
    Comparing full ids here made the anchor lookup find nothing and every
    delegation send fail with "thread not found in Lexi mailbox".
    """
    cid = (conversation_id or "").strip()
    return cid[-24:] if len(cid) > 24 else cid


def _scope_to_thread(
    messages: list[dict[str, Any]],
    *,
    conversation_id: str,
    internet_message_id: str = "",
) -> list[dict[str, Any]]:
    """Narrow a cross-mailbox listing to one thread.

    Composio ignores the `filter` argument, so this has to happen client-side or
    an unrelated message becomes the reply anchor.
    """
    wanted_internet = (internet_message_id or "").strip()
    if wanted_internet:
        exact = [
            m
            for m in messages
            if str(m.get("internetMessageId") or "").strip() == wanted_internet
        ]
        if exact:
            return exact  # same RFC-5322 Message-ID: the very same email

    key = _conversation_key(conversation_id)
    if not key:
        return []
    kept: list[dict[str, Any]] = []
    for message in messages:
        actual = _conversation_key(str(message.get("conversationId") or ""))
        # Drop only proven mismatches; a payload without conversationId can't be
        # disproved, and the anchor picker still checks sender and recipient.
        if actual and actual != key:
            continue
        kept.append(message)
    return kept


def resolve_lexi_reply_message_id(
    kory_message_id: str,
    *,
    conversation_id: str | None = None,
    intended_recipient: str | None = None,
) -> str:
    """Map a Kory-mailbox Graph id to Lexi's reply-all anchor in the same thread."""
    if settings.lexi_dry_run:
        anchor = (kory_message_id or conversation_id or "dry-run").strip()
        return f"dry-run-lexi-{anchor[:32]}"

    from app.integrations.composio_client import execute_tool
    from app.integrations.outlook_thread import extract_list_messages

    cid = (conversation_id or "").strip()
    kory_internet_id = ""
    if (kory_message_id or "").strip():
        try:
            kory_msg, _ = get_message(kory_message_id)
            kory_internet_id = str(kory_msg.get("internetMessageId") or "").strip()
            if not cid:
                cid = str(kory_msg.get("conversationId") or "").strip()
        except Exception:  # fall back to conversation matching below
            logger.debug("Could not read Kory's copy for anchor resolution.", exc_info=True)
    if not cid and not kory_internet_id:
        raise RuntimeError(
            "Could not resolve Outlook conversation for Lexi reply. "
            "Ensure Lexi is CC'd on Kory's delegation email."
        )

    result = execute_tool(
        "OUTLOOK_LIST_MESSAGES",
        {
            "user_id": "me",
            "folder": "inbox",
            "top": 50,
            "orderby": ["receivedDateTime desc"],
            "select": [
                "id",
                "subject",
                "from",
                "toRecipients",
                "ccRecipients",
                "receivedDateTime",
                "conversationId",
                "internetMessageId",
            ],
            "filter": f"conversationId eq '{cid}'",
        },
        role="lexi",
    )
    messages = _scope_to_thread(
        extract_list_messages(result.get("data")),
        conversation_id=cid,
        internet_message_id=kory_internet_id,
    )
    if not messages:
        raise RuntimeError(
            "Thread not found in Lexi mailbox. Ensure Kory CC'd lexi@ on the reply."
        )
    anchor = _pick_lexi_delegation_anchor(messages, intended_recipient=intended_recipient)
    if not anchor:
        raise RuntimeError("Could not find a delegation anchor message in Lexi's mailbox.")
    return str(anchor["id"])


def _create_lexi_reply_all_draft(
    message_id: str,
    body: str,
    *,
    source_mail_folder_id: str = "inbox",
) -> tuple[str | None, str | None]:
    """Reply-all on Kory's delegation message with HTML body and signature."""
    from app.scheduling.lexi_html_signature import (
        lexi_html_email_package,
        lexi_html_signature_enabled,
    )

    result = execute_tool(
        "OUTLOOK_CREATE_REPLY_ALL_DRAFT",
        {
            "message_id": message_id,
            "mail_folder_id": source_mail_folder_id,
            "user_id": "me",
            "comment": "",
        },
        role="lexi",
    )
    draft_id = _extract_draft_message_id(result.get("data")) or _extract_id(
        _coerce_data(result.get("data"))
    )
    if not draft_id:
        return None, result.get("log_id")

    update_args: dict[str, Any] = {
        "user_id": "me",
        "mail_folder_id": "drafts",
        "message_id": draft_id,
    }
    if lexi_html_signature_enabled():
        html_body, inline_attachments, _ = lexi_html_email_package(body)
        update_args["body"] = {"contentType": "html", "content": html_body}
        execute_tool("OUTLOOK_UPDATE_USER_MAIL_FOLDER_MESSAGE", update_args, role="lexi")
        if inline_attachments:
            attachment = inline_attachments[0]
            execute_tool(
                "OUTLOOK_ADD_MAIL_ATTACHMENT",
                {
                    "user_id": "me",
                    "message_id": draft_id,
                    "odata_type": attachment["@odata.type"],
                    "name": attachment["name"],
                    "contentType": attachment["contentType"],
                    "content_bytes": attachment["contentBytes"],
                    "isInline": True,
                    "contentId": attachment["contentId"],
                },
                role="lexi",
            )
    else:
        update_args["body"] = {
            "contentType": "text",
            "content": body.replace("\n", "\r\n"),
        }
        execute_tool("OUTLOOK_UPDATE_USER_MAIL_FOLDER_MESSAGE", update_args, role="lexi")

    ensure_kory_cc_on_lexi_draft(draft_id)
    return draft_id, result.get("log_id")


def create_draft_reply(
    message_id: str,
    body: str,
    *,
    send_channel: SendChannel = "kory",
) -> tuple[str | None, str | None]:
    channel = (send_channel or "kory").strip().lower()
    if settings.lexi_dry_run:
        mode = "reply-all" if channel == "lexi" else "reply"
        logger.info(
            "[DRY RUN] Would create %s draft for thread/message %s", mode, message_id
        )
        print(
            f"\n[Lexi DRY RUN] Email NOT sent. Draft {mode} preview:\n"
            "─" * 60 + "\n"
            f"{body}\n"
            "─" * 60 + "\n"
            f"Kory CC: {', '.join(merge_kory_cc_addresses()) or '(none configured)'}\n",
            flush=True,
        )
        return f"dry-run-draft-{message_id[:24]}", "dry-run-no-log"

    if settings.lexi_write_mode == "sandbox" and settings.sandbox_email_loopback:
        subject = f"[Lexi pilot draft] Reply re message {message_id[:12]}"
        return send_outbound_email(
            to_email=settings.sandbox_mailbox_email or "",
            subject=subject,
            body=body,
            approved_send=True,
        )

    if channel == "lexi":
        return _create_lexi_reply_all_draft(message_id, body)

    from app.scheduling.kory_html_signature import kory_html_signature_enabled

    if kory_html_signature_enabled():
        drafted = _create_kory_html_reply_draft(message_id, body)
        if drafted is not None:
            return drafted

    result = execute_tool(
        "OUTLOOK_CREATE_DRAFT_REPLY",
        {
            "message_id": message_id,
            "user_id": "me",
            "comment": body.replace("\n", "\r\n"),
        },
        role="write",
    )
    data = _coerce_data(result["data"])
    return _extract_id(data), result.get("log_id")


def _create_kory_html_reply_draft(
    message_id: str,
    body: str,
) -> tuple[str | None, str | None] | None:
    """Reply draft from Kory's mailbox carrying his branded HTML signature.

    Graph's reply `comment` field is plain text only, so the branded block has to
    be written in a second step: create the draft, replace its body with HTML,
    attach the logo inline by CID, and let the caller send it. Kory-channel
    replies go to the sender only — never reply-all, which is the Lexi path.

    Returns None to fall back to the plain-text reply if any step fails; a
    missing signature is worth far less than a lost reply.
    """
    from app.scheduling.kory_html_signature import kory_html_email_package

    try:
        result = execute_tool(
            "OUTLOOK_CREATE_DRAFT_REPLY",
            {"message_id": message_id, "user_id": "me", "comment": ""},
            role="write",
        )
        draft_id = _extract_draft_message_id(result.get("data")) or _extract_id(
            _coerce_data(result.get("data"))
        )
        if not draft_id:
            return None

        html_body, inline_attachments, _ = kory_html_email_package(body)
        execute_tool(
            "OUTLOOK_UPDATE_USER_MAIL_FOLDER_MESSAGE",
            {
                "user_id": "me",
                "mail_folder_id": "drafts",
                "message_id": draft_id,
                "body": {"contentType": "html", "content": html_body},
            },
            role="write",
        )
        if inline_attachments:
            attachment = inline_attachments[0]
            execute_tool(
                "OUTLOOK_ADD_MAIL_ATTACHMENT",
                {
                    "user_id": "me",
                    "message_id": draft_id,
                    "odata_type": attachment["@odata.type"],
                    "name": attachment["name"],
                    "contentType": attachment["contentType"],
                    "content_bytes": attachment["contentBytes"],
                    "isInline": True,
                    "contentId": attachment["contentId"],
                },
                role="write",
            )
        return draft_id, result.get("log_id")
    except Exception as exc:  # noqa: BLE001 — never lose a reply over a signature
        logger.warning("Kory HTML reply draft failed (%s); falling back to plain text", exc)
        return None


def send_reply_in_thread(
    message_id: str,
    body: str,
    *,
    send_channel: SendChannel = "lexi",
    approved_send: bool = True,
    conversation_id: str | None = None,
    intended_recipient: str | None = None,
) -> tuple[str | None, str | None]:
    """Reply in the same Outlook thread (delegation / CC Lexi path)."""
    from app.safety.approval_gate import assert_outbound_send_authorized

    channel = (send_channel or "lexi").strip().lower()
    if channel not in {"kory", "lexi"}:
        channel = "lexi"
    assert_outbound_send_authorized(approved_send=approved_send, send_channel=channel)

    from app.scheduling.email_format import finalize_lexi_email_body, finalize_outbound_email_body

    if channel == "lexi":
        reply_body = finalize_lexi_email_body(body)
        target_message_id = resolve_lexi_reply_message_id(
            message_id,
            conversation_id=conversation_id,
            intended_recipient=intended_recipient,
        )
    else:
        reply_body = finalize_outbound_email_body(body)
        target_message_id = message_id

    draft_id, draft_log = create_draft_reply(
        target_message_id,
        reply_body,
        send_channel=channel,  # type: ignore[arg-type]
    )
    if not draft_id:
        return None, draft_log
    send_log = send_draft(draft_id, send_channel=channel)  # type: ignore[arg-type]
    return draft_id, send_log or draft_log


def send_draft(draft_message_id: str, *, send_channel: SendChannel = "kory") -> str | None:
    if settings.lexi_dry_run:
        logger.info("[DRY RUN] Would send draft %s (skipped)", draft_message_id)
        print(f"\n[Lexi DRY RUN] Would send draft id={draft_message_id} (not sent)\n", flush=True)
        return "dry-run-no-log"

    if draft_message_id.startswith("dry-run-"):
        return "dry-run-no-log"
    if settings.lexi_write_mode == "sandbox" and settings.sandbox_email_loopback:
        return "sandbox-already-sent"

    from app.integrations.composio_client import execute_tool

    channel = (send_channel or "kory").strip().lower()
    role = "lexi" if channel == "lexi" else "write"
    result = execute_tool(
        "OUTLOOK_SEND_DRAFT",
        {
            "message_id": draft_message_id,
            "user_id": "me",
        },
        role=role,  # type: ignore[arg-type]
    )
    return result.get("log_id")


OUTBOUND_SEND_TOOL_CANDIDATES = (
    "OUTLOOK_SEND_EMAIL",
    "OUTLOOK_SEND_MAIL",
    "MICROSOFT_OUTLOOK_SEND_EMAIL",
    "OUTLOOK_CREATE_DRAFT_EMAIL_AND_SEND",
)


def _extract_draft_message_id(data: Any) -> str | None:
    payload = _coerce_data(data)
    if payload.get("id"):
        return str(payload["id"])
    message = payload.get("message")
    if isinstance(message, dict) and message.get("id"):
        return str(message["id"])
    response = payload.get("response_data")
    if isinstance(response, dict) and response.get("id"):
        return str(response["id"])
    return None


def _send_lexi_html_via_draft(
    *,
    recipient: str,
    subject: str,
    html_body: str,
    inline_attachment: dict[str, Any],
    write_role: ConnectionRole,
) -> tuple[str | None, str | None]:
    """Create draft, attach inline logo (CID), send — required for Gmail-compatible images."""
    draft_result = execute_tool(
        "OUTLOOK_CREATE_DRAFT",
        _build_outlook_draft_arguments(
            recipient=recipient,
            subject=subject,
            body=html_body,
            is_html=True,
            # This path returns before send_outbound_email's BCC block, so without
            # this it sends no BCC at all — and it is the path production uses.
            bcc_emails=outbound_bcc_addresses(recipient),
        ),
        role=write_role,
    )
    message_id = _extract_draft_message_id(draft_result.get("data"))
    if not message_id:
        raise RuntimeError("OUTLOOK_CREATE_DRAFT did not return a message id.")

    execute_tool(
        "OUTLOOK_ADD_MAIL_ATTACHMENT",
        {
            "user_id": "me",
            "message_id": message_id,
            "odata_type": inline_attachment["@odata.type"],
            "name": inline_attachment["name"],
            "contentType": inline_attachment["contentType"],
            "content_bytes": inline_attachment["contentBytes"],
            "isInline": True,
            "contentId": inline_attachment["contentId"],
        },
        role=write_role,
    )
    send_result = execute_tool(
        "OUTLOOK_SEND_DRAFT",
        {"user_id": "me", "message_id": message_id},
        role=write_role,
    )
    from app.safety.operation_verify import verify_send_ack

    ack = verify_send_ack(message_id=message_id, status_code=202)
    if not ack.ok and send_result.get("error"):
        raise RuntimeError(str(send_result.get("error")))
    return message_id, send_result.get("log_id")


_ORG_EMAIL_DOMAINS = {"iconicfounders.com", "ifg.vc"}


def _kory_cc_addresses() -> list[str]:
    """Kory's real primary CC address (settings.kory_cc_email).

    Raw address only — no enable-gate here. Lexi's outbound path applies the
    cc_kory_enabled toggle in merge_kory_cc_addresses; other callers (e.g. Heidi
    escalation) gate on their own flags.
    """
    addr = (settings.kory_cc_email or "").strip().lower()
    return [addr] if addr and "@" in addr else []


def _is_external_recipient(email: str) -> bool:
    """True when the address is outside Kory's org (an outsider)."""
    domain = email.split("@")[-1].strip().lower()
    return bool(domain) and domain not in _ORG_EMAIL_DOMAINS


def kory_thread_addresses() -> set[str]:
    """All addresses that count as 'Kory is on this thread'."""
    addrs = {(settings.kory_cc_email or "").strip().lower()}
    addrs |= {e.strip().lower() for e in settings.kory_sender_emails}
    return {a for a in addrs if a and "@" in a}


def kory_on_thread(recipients: dict[str, Any]) -> bool:
    """True when a Kory address is among the To/CC of a message (normalized dict)."""
    kory = kory_thread_addresses()
    if not kory:
        return False
    present: set[str] = set()
    for key in ("to_recipients", "cc_recipients"):
        for item in recipients.get(key) or []:
            addr = ((item.get("emailAddress") or {}).get("address") or "").strip().lower()
            if addr:
                present.add(addr)
    return bool(kory & present)


def _get_lexi_message(message_id: str) -> dict[str, Any]:
    """Read a message/draft from Lexi's mailbox (for the on-thread CC check)."""
    result = execute_tool(
        "OUTLOOK_GET_MESSAGE",
        {"message_id": message_id, "user_id": "me",
         "select": ["id", "toRecipients", "ccRecipients"]},
        role="lexi",
    )
    return _coerce_data(result.get("data")) or {}


def hubspot_bcc_addresses(to_emails: list[str]) -> list[str]:
    """BCC the HubSpot logging address on outbound mail that reaches an outsider.

    Production-only (gated by hubspot_bcc_enabled); returns [] otherwise so tests
    and internal-only mail are never BCC'd.
    """
    if not settings.hubspot_bcc_enabled:
        return []
    addr = (settings.hubspot_bcc_address or "").strip().lower()
    if not addr or "@" not in addr:
        return []
    if any(_is_external_recipient(e) for e in to_emails if e and "@" in e):
        return [addr]
    return []


def _graph_recipient_list(emails: list[str]) -> list[dict[str, Any]]:
    return [
        {"emailAddress": {"address": addr}}
        for addr in emails
        if addr and "@" in addr
    ]


def _plain_email_list(emails: list[str]) -> list[str]:
    """Composio Outlook tools expect recipient lists as plain email strings."""
    return [addr.strip().lower() for addr in emails if addr and "@" in addr]


def outbound_bcc_addresses(recipient: str) -> list[str]:
    """Every BCC an outbound message should carry: sandbox loopback + HubSpot logging.

    Shared so the two send paths cannot drift. They already had: the HTML-signature
    draft path skipped the BCC entirely, so with the signature enabled — which it is
    in production — nothing Lexi sent was ever BCC'd to HubSpot.
    """
    bcc: list[str] = []
    configured_target = (settings.sandbox_mailbox_email or "").strip().lower()
    if (
        settings.lexi_write_mode == "sandbox"
        and configured_target
        and recipient.strip().lower() != configured_target
    ):
        bcc.append(configured_target)
    for addr in hubspot_bcc_addresses([recipient]):
        if addr not in bcc:
            bcc.append(addr)
    return bcc


def _build_outlook_draft_arguments(
    *,
    recipient: str,
    subject: str,
    body: str,
    is_html: bool,
    cc_emails: list[str] | None = None,
    bcc_emails: list[str] | None = None,
) -> dict[str, Any]:
    """Composio OUTLOOK_CREATE_DRAFT payload — plain-string recipients; omit empty CC/BCC."""
    to_list = _plain_email_list([recipient])
    if not to_list:
        raise ValueError("recipient must be a valid email address.")
    args: dict[str, Any] = {
        "user_id": "me",
        "subject": subject,
        "body": body,
        "is_html": is_html,
        "to_recipients": to_list,
    }
    cc_list = _plain_email_list(merge_kory_cc_addresses(cc_emails))
    if cc_list:
        args["cc_recipients"] = cc_list
    bcc_list = _plain_email_list(bcc_emails or [])
    if bcc_list:
        args["bcc_recipients"] = bcc_list
    return args


def merge_kory_cc_addresses(existing: list[str] | None = None) -> list[str]:
    """Merge Kory's CC into a Lexi-outbound CC list (deduped, lowercased).

    The cc_kory_enabled toggle applies here (Lexi's outbound mail), so it's off in
    sandbox tests but on in production.
    """
    kory = _kory_cc_addresses() if settings.cc_kory_enabled else []
    seen: set[str] = set()
    merged: list[str] = []
    for addr in list(existing or []) + kory:
        normalized = addr.strip().lower()
        if normalized and "@" in normalized and normalized not in seen:
            seen.add(normalized)
            merged.append(normalized)
    return merged


def ensure_kory_cc_on_lexi_draft(draft_id: str) -> None:
    """Add Kory's CC (reply-all may drop him) and the production HubSpot BCC.

    A Lexi reply-all draft is always a reply to an inbound external scheduling
    counterpart, so its recipients are outsiders — the HubSpot logging BCC applies
    when enabled (production only; no-op in testing).
    """
    if settings.lexi_dry_run or not draft_id or draft_id.startswith("dry-run"):
        return
    kory_cc = merge_kory_cc_addresses()
    # Skip the Kory CC if he's already a To/CC participant of this reply-all thread
    # (e.g. the delegation case where Kory CC'd Lexi). If the draft can't be read,
    # fall back to CC'ing him (better to keep him informed than to drop him).
    if kory_cc:
        try:
            if kory_on_thread(extract_recipient_list(_get_lexi_message(draft_id))):
                kory_cc = []
        except Exception:
            pass
    # hubspot_bcc_enabled is production-only; pass a placeholder external address so
    # the outsider check passes for this inherently-external reply-all context.
    hubspot_bcc = hubspot_bcc_addresses(["counterpart@external.invalid"])
    if not kory_cc and not hubspot_bcc:
        return
    update_args: dict[str, Any] = {
        "user_id": "me",
        "mail_folder_id": "drafts",
        "message_id": draft_id,
    }
    if kory_cc:
        update_args["cc_recipients"] = _graph_recipient_list(kory_cc)
    if hubspot_bcc:
        update_args["bcc_recipients"] = _graph_recipient_list(hubspot_bcc)
    execute_tool("OUTLOOK_UPDATE_USER_MAIL_FOLDER_MESSAGE", update_args, role="lexi")


def forward_message_from_lexi_mailbox(
    message_id: str,
    *,
    to_email: str,
    comment: str = "",
    cc_emails: list[str] | None = None,
) -> dict[str, Any]:
    """Forward a thread message from Lexi's mailbox."""
    if not message_id or not to_email:
        return {"forwarded": False, "reason": "missing message_id or recipient"}
    if settings.lexi_dry_run:
        cc_preview = merge_kory_cc_addresses(cc_emails)
        logger.info(
            "[DRY RUN] Would forward message %s to %s cc=%s",
            message_id,
            to_email,
            cc_preview,
        )
        return {
            "forwarded": False,
            "dry_run": True,
            "to": to_email,
            "cc": cc_preview,
            "message_id": message_id,
        }

    cc = merge_kory_cc_addresses(cc_emails)
    comment_text = (comment or "").replace("\n", "\r\n")
    argument_sets: list[dict[str, Any]] = [
        {
            "user_id": "me",
            "message_id": message_id,
            "to_recipients": [to_email],
            "comment": comment_text,
            **({"cc_recipients": cc} if cc else {}),
        },
        {
            "user_id": "me",
            "message_id": message_id,
            "to": to_email,
            "comment": comment_text,
            **({"cc_emails": cc} if cc else {}),
        },
    ]
    last_error: Exception | None = None
    for arguments in argument_sets:
        try:
            result = execute_tool("OUTLOOK_FORWARD_MESSAGE", arguments, role="lexi")
            data = _coerce_data(result.get("data"))
            forwarded_id = _extract_id(data)
            return {
                "forwarded": True,
                "to": to_email,
                "cc": cc,
                "message_id": forwarded_id or message_id,
                "log_id": result.get("log_id"),
            }
        except Exception as exc:
            last_error = exc
            continue
    return {
        "forwarded": False,
        "error": str(last_error) if last_error else "forward failed",
        "to": to_email,
        "cc": cc,
    }


def kory_cc_addresses() -> list[str]:
    """Public accessor for Kory CC list on Lexi-sent mail."""
    return _kory_cc_addresses()


def infer_outbound_send_channel(body: str, *, explicit: str = "") -> str:
    """Pick kory vs lexi mailbox for chat-initiated outbound mail."""
    channel = (explicit or "").strip().lower()
    if channel in {"kory", "lexi"}:
        return channel
    # Read the sign-off, not just the canonical "Let's Win,\nKory": Hermes
    # drafts sign a bare "Kory" BEFORE the finalizer stamps the canonical
    # block, so requiring "let's win" sent Kory-voice mail from the Lexi
    # mailbox — an identity mismatch that also slips past the kory-channel
    # outbound block (live O-2a defect). Whichever name signs LAST wins.
    tail = (body or "").rstrip()[-200:]
    kory_sig = lexi_sig = None
    for match in re.finditer(r"(?mi)^\s*[-–—]*\s*Kory(?:\s+Mitchell)?\s*[.!]?\s*$", tail):
        kory_sig = match
    for match in re.finditer(r"(?mi)^\s*[-–—]*\s*Lexi(?:\s+Knightly)?\s*[.!]?\s*$", tail):
        lexi_sig = match
    if kory_sig and (lexi_sig is None or kory_sig.start() > lexi_sig.start()):
        return "kory"
    if lexi_sig:
        return "lexi"
    default = (settings.lexi_default_send_channel or "lexi").strip().lower()
    return default if default in {"kory", "lexi"} else "lexi"


def create_outbound_draft(
    *,
    to_email: str,
    subject: str,
    body: str,
    approved: bool = False,
    send_channel: SendChannel = "kory",
    cc_emails: list[str] | None = None,
) -> tuple[str | None, str | None]:
    """Create an Outlook draft (does not send). Blocked in dry-run / UAT unless enabled."""
    import os
    import uuid

    channel = (send_channel or "kory").strip().lower()
    if channel not in {"kory", "lexi"}:
        channel = "kory"

    drafts_enabled = os.getenv("LEXI_OUTREACH_OUTLOOK_DRAFTS_ENABLED", "false").lower() in {
        "1",
        "true",
        "yes",
    }
    if settings.lexi_dry_run or not drafts_enabled or not approved:
        preview_id = f"dry-run-outreach-draft-{uuid.uuid4().hex[:10]}"
        logger.info(
            "[DRY RUN] Would create Outlook draft to=%s subject=%s id=%s",
            to_email,
            subject,
            preview_id,
        )
        print(
            f"\n[Lexi DRY RUN] Outlook draft NOT created.\n"
            f"  To: {to_email}\n"
            f"  Subject: {subject}\n"
            f"  Preview id: {preview_id}\n",
            flush=True,
        )
        return preview_id, "dry-run-no-log"

    recipient = (to_email or "").strip()
    if not recipient or "@" not in recipient:
        raise ValueError("to_email is required for outbound draft.")

    write_role: ConnectionRole = "lexi" if channel == "lexi" else "write"
    result = execute_tool(
        "OUTLOOK_CREATE_DRAFT",
        _build_outlook_draft_arguments(
            recipient=recipient,
            subject=subject,
            body=body.replace("\n", "\r\n"),
            is_html=False,
            cc_emails=cc_emails,
        ),
        role=write_role,
    )
    message_id = _extract_draft_message_id(result.get("data")) or _extract_id(
        _coerce_data(result.get("data"))
    )
    return message_id, result.get("log_id")


def kory_review_drafts_enabled() -> bool:
    import os

    return os.getenv("LEXI_KORY_REVIEW_DRAFTS_ENABLED", "true").lower() in {"1", "true", "yes"}


def create_kory_review_draft(
    *,
    to_email: str,
    subject: str,
    body: str,
    cc_emails: list[str] | None = None,
) -> dict[str, Any]:
    """Park an email in Kory's Outlook Drafts for him to review and send himself.

    Nothing leaves the mailbox: this only creates the draft. That is the whole
    point — the draft *is* the review step, so it carries no send approval.

    Deliberately not `create_outbound_draft`, which is the parked outreach-campaign
    path: it is gated on that feature's flag, sends plain text with no signature,
    and routes through _build_outlook_draft_arguments, which merges Kory's own CC
    in — correct for Lexi's outbound mail, wrong for a draft sitting in his own
    mailbox, where it would CC him on his own message.

    Returns the observed draft, read back from the mailbox. A draft nobody can
    find is not a saved draft.
    """
    recipient = (to_email or "").strip()
    if not recipient or "@" not in recipient:
        return {"ok": False, "error": "A valid recipient address is required."}
    if not (subject or "").strip():
        return {"ok": False, "error": "The draft needs a subject."}
    if not (body or "").strip():
        return {"ok": False, "error": "The draft needs a body."}

    if not kory_review_drafts_enabled():
        return {"ok": False, "error": "Saving drafts is disabled (LEXI_KORY_REVIEW_DRAFTS_ENABLED=false)."}

    if settings.lexi_dry_run:
        logger.info("[DRY RUN] Would save draft to=%s subject=%s", recipient, subject)
        return {
            "ok": True,
            "dry_run": True,
            "verified": False,
            "to": recipient,
            "subject": subject,
        }

    from app.scheduling.email_format import finalize_outbound_email_body
    from app.scheduling.kory_html_signature import (
        kory_html_email_package,
        kory_html_signature_enabled,
    )

    plain = finalize_outbound_email_body(body)
    if kory_html_signature_enabled():
        html_body, inline_attachments, _ = kory_html_email_package(plain)
    else:
        html_body, inline_attachments = plain, []

    cc_list = _plain_email_list(
        [a for a in (cc_emails or []) if a.strip().lower() != recipient.lower()]
    )
    args: dict[str, Any] = {
        "user_id": "me",
        "subject": subject,
        "body": html_body,
        "is_html": bool(kory_html_signature_enabled()),
        "to_recipients": _plain_email_list([recipient]),
    }
    if cc_list:
        args["cc_recipients"] = cc_list

    result = execute_tool("OUTLOOK_CREATE_DRAFT", args, role="write")
    draft_id = _extract_draft_message_id(result.get("data")) or _extract_id(
        _coerce_data(result.get("data"))
    )
    if not draft_id:
        return {
            "ok": False,
            "error": "Outlook did not return a draft id — nothing was saved.",
            "composio_log_id": result.get("log_id"),
        }

    if inline_attachments:
        attachment = inline_attachments[0]
        try:
            execute_tool(
                "OUTLOOK_ADD_MAIL_ATTACHMENT",
                {
                    "user_id": "me",
                    "message_id": draft_id,
                    "odata_type": attachment["@odata.type"],
                    "name": attachment["name"],
                    "contentType": attachment["contentType"],
                    "content_bytes": attachment["contentBytes"],
                    "isInline": True,
                    "contentId": attachment["contentId"],
                },
                role="write",
            )
        except Exception as exc:  # noqa: BLE001 — a logo is not worth losing the draft
            logger.warning("Draft logo attach failed (%s); draft kept without it", exc)

    observed = _read_back_draft(draft_id)
    return {
        "ok": True,
        "verified": bool(observed),
        "draft_id": draft_id,
        "to": recipient,
        "cc": cc_list,
        "subject": observed.get("subject") if observed else subject,
        "is_draft": observed.get("isDraft") if observed else None,
        "composio_log_id": result.get("log_id"),
        **({} if observed else {"warning": "Saved, but the draft could not be read back to confirm it."}),
    }


def _read_back_draft(draft_id: str) -> dict[str, Any] | None:
    """Confirm a draft exists in the mailbox. None means unverified, not absent."""
    try:
        result = execute_tool(
            "OUTLOOK_GET_MESSAGE", {"user_id": "me", "message_id": draft_id}, role="write"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Draft read-back failed for %s: %s", draft_id, exc)
        return None
    data = _coerce_data(result.get("data"))
    message = data.get("message") if isinstance(data.get("message"), dict) else data
    return message if isinstance(message, dict) and message.get("id") else None


def send_outbound_email(
    *,
    to_email: str,
    subject: str,
    body: str,
    approved_send: bool = False,
    send_channel: SendChannel = "kory",
    cc_emails: list[str] | None = None,
    html_body: bool = False,
) -> tuple[str | None, str | None]:
    """Send email via write mailbox (sandbox loopback in pilot) or Lexi mailbox.

    approved_send must be True unless LEXI_REQUIRE_KORY_APPROVAL=false (tests only).
    Production path: execute_lexi_approval → comms_agent, or Hermes confirm_send=true.

    html_body marks an already-formatted HTML payload. Only the Lexi channel used
    to send HTML, so anything else arrived as plain text with the markup visible.
    Scheduling mail composes plain text and must keep the default.
    """
    from app.safety.approval_gate import assert_outbound_send_authorized, kory_outbound_email_blocked

    channel = (send_channel or "kory").strip().lower()
    if channel not in {"kory", "lexi"}:
        channel = "kory"

    assert_outbound_send_authorized(approved_send=approved_send, send_channel=channel)
    if channel == "kory" and kory_outbound_email_blocked():
        raise PermissionError(
            "Kory outbound email is DISABLED. No messages will leave Kory's mailbox."
        )
    if channel == "lexi" and not settings.lexi_composio_connection_id:
        raise ComposioNotConfiguredError(
            "LEXI_COMPOSIO_CONNECTION_ID is missing — cannot send from Lexi mailbox."
        )
    recipient = _sandbox_loopback_recipient(to_email, send_channel=channel)
    if not recipient:
        raise ValueError("to_email is required for outbound send.")

    pilot_subject = subject
    if settings.lexi_write_mode == "sandbox" and settings.sandbox_email_loopback:
        if not pilot_subject.startswith("[Lexi pilot]"):
            pilot_subject = f"[Lexi pilot] {subject}"

    if settings.lexi_dry_run:
        cc_preview = cc_emails or (_kory_cc_addresses() if channel == "lexi" else [])
        logger.info(
            "[DRY RUN] Would send outbound email to %s cc=%s subject=%s",
            recipient,
            cc_preview,
            pilot_subject,
        )
        print(
            f"\n[Lexi DRY RUN] Outbound email NOT sent.\n"
            f"  To: {recipient}\n"
            f"  CC: {', '.join(cc_preview) if cc_preview else '(none)'}\n"
            f"  Subject: {pilot_subject}\n",
            flush=True,
        )
        return f"dry-run-outbound-{recipient[:16]}", "dry-run-no-log"

    configured_target = (settings.sandbox_mailbox_email or "").strip().lower()
    from app.scheduling.email_format import finalize_lexi_email_body, finalize_outbound_email_body

    if html_body:
        # Already-formatted HTML. The plain-text finalizers normalise whitespace
        # and append a "Let's Win, / Kory" sign-off, both of which would corrupt
        # markup that is not a person-to-person note.
        pilot_body = body
    elif channel == "lexi":
        pilot_body = finalize_lexi_email_body(body)
    else:
        pilot_body = finalize_outbound_email_body(body)
    if (
        settings.lexi_write_mode == "sandbox"
        and settings.sandbox_email_loopback
        and configured_target
        and recipient.lower() != configured_target
    ):
        pilot_body = (
            f"[Lexi pilot — configured target: {configured_target}]\n"
            f"[Delivered via Composio-connected mailbox: {recipient}]\n\n"
            f"{body}"
        )

    send_body = pilot_body
    is_html = bool(html_body)
    inline_attachments: list[dict[str, Any]] = []
    use_draft_inline_send = False
    if channel == "lexi":
        from app.scheduling.lexi_html_signature import (
            lexi_html_email_package,
            lexi_html_signature_enabled,
        )

        if lexi_html_signature_enabled():
            send_body, inline_attachments, use_draft_inline_send = lexi_html_email_package(pilot_body)
            is_html = True
    elif not html_body and recipient.strip().lower() not in kory_thread_addresses():
        # Kory's own mailbox gets his branded block too — podcast line, logo and
        # contact details, the same signature he signs with by hand.
        #
        # Two exclusions. Caller-supplied HTML is not a person-to-person note (the
        # morning briefing is markup, and appending a sign-off corrupts it). And
        # mail addressed to Kory himself is a system notification, not something
        # he is sending — signing his own briefing with his own contact card reads
        # as a bug.
        from app.scheduling.kory_html_signature import (
            kory_html_email_package,
            kory_html_signature_enabled,
        )

        if kory_html_signature_enabled():
            send_body, inline_attachments, use_draft_inline_send = kory_html_email_package(pilot_body)
            is_html = True

    write_role: ConnectionRole = "lexi" if channel == "lexi" else "write"

    if use_draft_inline_send and inline_attachments:
        return _send_lexi_html_via_draft(
            recipient=recipient,
            subject=pilot_subject,
            html_body=send_body,
            inline_attachment=inline_attachments[0],
            write_role=write_role,
        )

    arguments: dict[str, Any] = {
        "user_id": "me",
        "to": recipient,
        "subject": pilot_subject,
        "body": send_body,
        "is_html": is_html,
        "save_to_sent_items": True,
    }
    if channel == "lexi":
        merged_cc: list[str] = []
        seen_cc: set[str] = set()
        for addr in merge_kory_cc_addresses(cc_emails):
            if addr not in seen_cc and addr != recipient.lower():
                seen_cc.add(addr)
                merged_cc.append(addr)
        if merged_cc:
            arguments["cc_emails"] = merged_cc
    # Sandbox loopback + HubSpot logging BCC, shared with the draft path so the
    # two cannot drift apart again.
    bcc = outbound_bcc_addresses(recipient)
    if bcc:
        arguments["bcc_emails"] = bcc

    last_error: Exception | None = None
    for tool_slug in OUTBOUND_SEND_TOOL_CANDIDATES:
        try:
            result = execute_tool(tool_slug, arguments, role=write_role)
            data = _coerce_data(result.get("data"))
            message_id = _extract_id(data)
            status_code = data.get("status_code") if isinstance(data, dict) else None
            if not message_id and status_code in {200, 201, 202}:
                message_id = f"sent-{tool_slug.lower()}"
            if message_id or status_code in {200, 201, 202}:
                from app.safety.operation_verify import verify_send_ack

                ack = verify_send_ack(message_id=message_id, status_code=status_code)
                if not ack.ok:
                    raise RuntimeError("; ".join(ack.errors))
                return message_id, result.get("log_id")
        except Exception as exc:
            last_error = exc
            continue

    if last_error:
        raise last_error
    raise RuntimeError("No outbound Outlook send tool succeeded.")


def send_pilot_reply_for_proposal(
    *,
    original_subject: str | None,
    body: str,
    intended_recipient: str | None = None,
    send_channel: SendChannel = "kory",
) -> tuple[str | None, str | None]:
    """Send approved reply to the actual recipient (reply-to sender)."""
    subject = original_subject or "Scheduling reply"
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    recipient = (intended_recipient or "").strip()
    if not recipient:
        raise ValueError("intended_recipient is required for approved reply send.")
    return send_outbound_email(
        to_email=recipient,
        subject=subject,
        body=body,
        approved_send=True,
        send_channel=send_channel,
    )


def extract_recipient_list(message: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Normalize Outlook recipient fields for delegation detection."""
    result: dict[str, list[dict[str, Any]]] = {
        "to_recipients": [],
        "cc_recipients": [],
        "bcc_recipients": [],
    }
    key_map = {
        "toRecipients": "to_recipients",
        "ccRecipients": "cc_recipients",
        "bccRecipients": "bcc_recipients",
    }
    for graph_key, out_key in key_map.items():
        value = message.get(graph_key) or message.get(out_key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    result[out_key].append(item)
    return result


def merge_list_message_fields(
    full_message: dict[str, Any],
    list_item: dict[str, Any],
) -> dict[str, Any]:
    """Backfill fields the full GET_MESSAGE payload may omit from the list/poll item.

    Keeps conversationId (GET_MESSAGE omits it from select) and, as a safety net,
    the recipient lists — so delegation detection (CC lexi@) never misses a
    Kory-originated reply just because one fetch path returned trimmed recipients.
    """
    merged = dict(full_message)
    conv = list_item.get("conversationId") or list_item.get("conversation_id")
    if conv and not merged.get("conversationId"):
        merged["conversationId"] = conv
    for key in ("ccRecipients", "toRecipients", "bccRecipients"):
        if not merged.get(key) and list_item.get(key):
            merged[key] = list_item[key]
    return merged


def build_inbound_raw_email(
    *,
    message_id: str,
    normalized: dict[str, Any],
    recipients: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Lexi inbound dict with CC/BCC for delegation detection."""
    payload: dict[str, Any] = {
        "thread_id": message_id,
        "message_id": message_id,
        "conversation_id": normalized.get("conversation_id") or "",
        "subject": normalized["subject"],
        "sender": normalized["sender_email"],
        "received_at": normalized.get("received_at") or "",
        "raw_body": normalized["body"],
    }
    for key in (
        "recipient_timezone",
        "recipient_timezone_confidence",
        "recipient_timezone_source",
        "internet_message_headers",
    ):
        if normalized.get(key) is not None:
            payload[key] = normalized[key]
    if recipients:
        payload.update(recipients)
    return payload


def normalize_message(message: dict[str, Any], raw_payload: dict[str, Any]) -> dict[str, Any]:
    sender = message.get("from") or {}
    email_address = sender.get("emailAddress", {}) if isinstance(sender, dict) else {}
    body = message.get("body") or {}
    raw_body = body.get("content") if isinstance(body, dict) else message.get("bodyPreview", "")
    body_text = _plain_text(raw_body or "")
    if not body_text.strip() and message.get("bodyPreview"):
        body_text = str(message.get("bodyPreview"))
    apparent_sender_name = _extract_signature_name(body_text)

    from app.scheduling.timezone_intel import resolve_recipient_timezone_at_ingest, extract_internet_headers

    from app.storage.recipient_profiles import record_display_name

    try:
        record_display_name(
            email_address.get("address"),
            apparent_sender_name or email_address.get("name"),
        )
    except Exception:  # never block ingest on a nicety
        logger.debug("Could not record display name for inbound sender.", exc_info=True)

    headers = extract_internet_headers(message)
    tz_result = resolve_recipient_timezone_at_ingest(
        sender_email=email_address.get("address"),
        body=body_text,
        internet_headers=headers,
        received_at=message.get("receivedDateTime"),
    )

    return {
        "outlook_message_id": message.get("id") or raw_payload.get("message_id"),
        "conversation_id": message.get("conversationId") or message.get("conversation_id"),
        "sender_email": email_address.get("address", "unknown@example.com"),
        "sender_name": apparent_sender_name or email_address.get("name"),
        "mailbox_sender_name": email_address.get("name"),
        "subject": message.get("subject") or "(no subject)",
        "body": body_text,
        "received_at": message.get("receivedDateTime"),
        "internet_message_headers": headers,
        "recipient_timezone": tz_result.tz_name(),
        "recipient_timezone_confidence": tz_result.confidence,
        "recipient_timezone_source": tz_result.source,
        "raw_payload": raw_payload,
    }


def _extract_id(data: dict[str, Any]) -> str | None:
    if data.get("id"):
        return data["id"]
    if isinstance(data.get("message"), dict):
        return data["message"].get("id")
    if isinstance(data.get("draft"), dict):
        return data["draft"].get("id")
    return None


def _coerce_data(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        return data
    if hasattr(data, "model_dump"):
        return data.model_dump()
    return {"value": data}


def _extract_signature_name(body: str) -> str | None:
    text = _plain_text(body)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    signoffs = {"cheers", "best", "thanks", "thank you", "regards", "sincerely"}

    for index, line in enumerate(lines):
        normalized = line.lower().strip(" ,.!-")
        if normalized not in signoffs:
            continue
        for candidate in lines[index + 1 : index + 4]:
            name = _clean_name_candidate(candidate)
            if name:
                return name
    for candidate in reversed(lines[-5:]):
        if _looks_like_reply_artifact(candidate):
            continue
        if candidate.lower().strip(" ,.!-") in signoffs:
            continue
        name = _clean_name_candidate(candidate)
        if name:
            return name
    return None


def _plain_text(body: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", body, flags=re.IGNORECASE)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text)


def _clean_name_candidate(candidate: str) -> str | None:
    cleaned = candidate.strip().strip(",.!-")
    if not cleaned or "@" in cleaned or len(cleaned) > 40:
        return None
    if not re.fullmatch(r"[A-Za-z][A-Za-z .'-]{0,38}", cleaned):
        return None
    words = cleaned.split()
    if len(words) > 3:
        return None
    return " ".join(word.capitalize() for word in words)


def _looks_like_reply_artifact(line: str) -> bool:
    lowered = line.lower()
    return (
        lowered.startswith(("from:", "sent:", "to:", "subject:", "on "))
        or "wrote:" in lowered
        or lowered in {"hi kory", "hello kory", "hey kory", "dear kory"}
    )
