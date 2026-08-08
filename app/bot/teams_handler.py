"""Legacy Lexi Teams bot — deprecated; production uses Hermes-only Teams.

Hermes handles chat via lexi_handle_teams_command MCP tool. Proactive cards use
teams_publisher + TEAMS_CONVERSATION_ID. This handler remains for optional debug only.
"""

from __future__ import annotations

import asyncio
import logging

from botbuilder.core import ActivityHandler, TurnContext

from app.agents.comms_agent import execute_lexi_approval, get_lexi_pending_queue
from app.bot.teams_conversation_store import capture_conversation_reference
from app.bot.teams_publisher import schedule_push_all_pending_cards
from app.bot.teams_security import _resolve_sender_identity, is_teams_sender_allowed
from app.agents.inbound_reply import begin_draft_reply, decline_reply, get_inbound_reply_queue
from app.bot.teams_publisher import schedule_teams_approval_push
from app.bot.teams_text import (
    TEAMS_HELP_TEXT,
    find_pending_item,
    format_approval_notification,
    format_inbound_reply_list,
    format_pending_list,
    parse_teams_command,
    resolve_slot_for_option,
)
from app.bot.teams_labels import action_confirmation_message
from app.config import settings

logger = logging.getLogger(__name__)

APPROVAL_ACTION = "execute_lexi_approval"
UNAUTHORIZED_MESSAGE = (
    "Unauthorized user. Lexi is restricted to approved executives only. "
    "If you believe this is an error, contact your administrator."
)


class LexiTeamsBot(ActivityHandler):
    """Routes Teams messages and card submissions into Lexi approval execution."""

    async def on_turn(self, turn_context: TurnContext) -> None:
        if not is_teams_sender_allowed(turn_context):
            sender = _resolve_sender_identity(turn_context) or "unknown"
            logger.warning(
                "Teams access denied for sender=%s (not in TEAMS_ALLOWED_USERS)",
                sender,
            )
            await turn_context.send_activity(UNAUTHORIZED_MESSAGE)
            return
        await super().on_turn(turn_context)

    async def on_message_activity(self, turn_context: TurnContext) -> None:
        ref = capture_conversation_reference(turn_context)
        value = turn_context.activity.value

        if isinstance(value, dict) and value.get("action") in {
            APPROVAL_ACTION,
            "update_proposal_draft",
        }:
            await self._handle_card_submit(turn_context, value)
            return

        text = (turn_context.activity.text or "").strip()
        command = parse_teams_command(text)
        if command:
            await self._handle_text_command(turn_context, command)
            return

        if not isinstance(value, dict):
            dry_note = (
                " (read-only mode: no calendar or email will be sent)"
                if settings.lexi_dry_run
                else ""
            )
            await turn_context.send_activity(
                f"Lexi is connected.{dry_note}\n\n{TEAMS_HELP_TEXT}"
            )
            if ref:
                schedule_push_all_pending_cards()
            return

        await turn_context.send_activity(
            "Unrecognized card action. Use the card buttons or describe the email in chat."
        )

    async def _handle_text_command(
        self,
        turn_context: TurnContext,
        command: dict,
    ) -> None:
        action = command.get("action")
        if action == "help":
            await turn_context.send_activity(TEAMS_HELP_TEXT)
            return

        if action == "pending":
            items = get_lexi_pending_queue()
            await turn_context.send_activity(format_pending_list(items))
            return

        if action == "inbound":
            await turn_context.send_activity(
                format_inbound_reply_list(get_inbound_reply_queue())
            )
            return

        if action == "unresolved":
            await turn_context.send_activity(command.get("message", "Could not match that email."))
            return

        authorized_by = _resolve_authorizer(turn_context)

        if action == "draft_no":
            proposal_id = int(command["proposal_id"])
            from app.teams.commands import _fetch_bundle

            bundle = _fetch_bundle(proposal_id)
            result = await asyncio.to_thread(
                decline_reply,
                proposal_id,
                reason="Declined via Teams",
            )
            if result.get("ok"):
                await turn_context.send_activity(
                    action_confirmation_message(
                        action="draft_no",
                        subject=bundle.get("subject") if bundle else command.get("subject"),
                        sender=bundle.get("sender") if bundle else command.get("sender"),
                    )
                )
            else:
                await turn_context.send_activity(result.get("error", "Could not skip."))
            return

        if action == "draft_yes":
            proposal_id = int(command["proposal_id"])
            result = await asyncio.to_thread(begin_draft_reply, proposal_id)
            if not result.get("ok"):
                await turn_context.send_activity(result.get("error", "Draft failed."))
                return
            draft = (result.get("drafted_reply") or "").strip()
            lines = [result.get("message", "Draft ready.")]
            if draft:
                excerpt = draft if len(draft) <= 600 else draft[:600] + "…"
                lines.append(f"\n**Draft preview:**\n{excerpt}")
            lines.append(
                "\n_Not sent._ Say what to change in chat, or use **Send** on the card when ready."
            )
            await turn_context.send_activity("\n".join(lines))
            item = find_pending_item(proposal_id)
            if item:
                schedule_teams_approval_push(proposal_id)
            return

        if action == "approve":
            proposal_id = int(command["proposal_id"])
            option = int(command.get("option") or 1)
            item = find_pending_item(proposal_id)
            if not item:
                from app.teams.commands import _fetch_bundle

                bundle = _fetch_bundle(proposal_id)
                await turn_context.send_activity(
                    action_confirmation_message(
                        action="approve",
                        subject=bundle.get("subject") if bundle else command.get("subject"),
                        sender=bundle.get("sender") if bundle else command.get("sender"),
                        success=False,
                        detail="No draft is pending approval for this email.",
                    )
                )
                return
            selected_slot = resolve_slot_for_option(item, option)
            await self._run_approval(
                turn_context,
                proposal_id=proposal_id,
                decision="approved",
                selected_slot=selected_slot,
                authorized_by=authorized_by,
                decision_source="teams_text",
            )
            return

        if action == "reject":
            proposal_id = int(command["proposal_id"])
            if not find_pending_item(proposal_id):
                from app.teams.commands import _fetch_bundle

                bundle = _fetch_bundle(proposal_id)
                await turn_context.send_activity(
                    action_confirmation_message(
                        action="reject",
                        subject=bundle.get("subject") if bundle else command.get("subject"),
                        sender=bundle.get("sender") if bundle else command.get("sender"),
                        success=False,
                        detail="No draft is pending approval for this email.",
                    )
                )
                return
            await self._run_approval(
                turn_context,
                proposal_id=proposal_id,
                decision="rejected",
                selected_slot="",
                authorized_by=authorized_by,
                decision_source="teams_text",
            )
            return

    async def _handle_card_submit(self, turn_context: TurnContext, value: dict) -> None:
        from app.teams.commands import handle_teams_card_submit

        authorized_by = _resolve_authorizer(turn_context)
        result = await asyncio.to_thread(
            handle_teams_card_submit,
            value,
            authorized_by=authorized_by,
        )
        if not result.get("handled"):
            await turn_context.send_activity(
                result.get("message", "Unrecognized card action.")
            )
            return
        await turn_context.send_activity(result.get("message", "Done."))

    async def _run_approval(
        self,
        turn_context: TurnContext,
        *,
        proposal_id: int,
        decision: str,
        selected_slot: str,
        authorized_by: str,
        decision_source: str,
    ) -> None:
        logger.info(
            "Teams approval proposal_id=%s decision=%s authorized_by=%s source=%s",
            proposal_id,
            decision,
            authorized_by,
            decision_source,
        )
        try:
            result = await asyncio.to_thread(
                execute_lexi_approval,
                proposal_id,
                decision,
                selected_slot,
                authorized_by,
                decision_source=decision_source,
            )
        except Exception as exc:
            logger.exception(
                "execute_lexi_approval failed for proposal_id=%s from Teams",
                proposal_id,
            )
            from app.teams.commands import _fetch_bundle

            bundle = _fetch_bundle(proposal_id)
            await turn_context.send_activity(
                action_confirmation_message(
                    action=decision,
                    subject=bundle.get("subject") if bundle else None,
                    sender=bundle.get("sender") if bundle else None,
                    success=False,
                    detail=f"Could not execute: {exc}",
                )
            )
            return

        from app.teams.commands import _fetch_bundle

        bundle = _fetch_bundle(proposal_id)
        # warnings carry Kory-facing remedy copy (E-6 clash guidance, hold
        # alerts) — append them or they are composed and never delivered.
        warnings = " ".join(result.warnings or [])
        if result.ok:
            suffix = " (dry run — nothing sent to Outlook)" if settings.lexi_dry_run else ""
            await turn_context.send_activity(
                action_confirmation_message(
                    action=decision,
                    subject=bundle.get("subject") if bundle else None,
                    sender=bundle.get("sender") if bundle else None,
                    success=True,
                    detail=(f"{suffix} ⚠️ {warnings}".strip() if warnings else suffix),
                )
            )
            return

        errors = ", ".join(result.errors or []) or "unknown error"
        await turn_context.send_activity(
            action_confirmation_message(
                action=decision,
                subject=bundle.get("subject") if bundle else None,
                sender=bundle.get("sender") if bundle else None,
                success=False,
                detail=f"{errors} {warnings}".strip(),
            )
        )


def _resolve_authorizer(turn_context: TurnContext) -> str:
    identity = _resolve_sender_identity(turn_context)
    return identity or "teams_unknown"
