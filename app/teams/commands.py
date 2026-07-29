"""Synchronous Teams text/command handling for Hermes MCP (Hermes-only Teams)."""

from __future__ import annotations

from typing import Any

from app.agents.comms_agent import execute_lexi_approval, get_lexi_pending_queue
from app.agents.inbound_reply import begin_draft_reply, decline_reply, get_inbound_reply_queue
from app.bot.teams_text import (
    TEAMS_HELP_TEXT,
    find_pending_item,
    format_approval_notification,
    format_inbound_reply_list,
    format_pending_list,
    parse_teams_command,
    resolve_slot_for_option,
)
from app.bot.teams_labels import action_confirmation_message, email_thread_label
from app.config import settings
from app.utils.teams_cards import (
    CARD_ACTION_APPROVAL,
    CARD_ACTION_INVITE,
    CARD_ACTION_REOFFER,
    CARD_ACTION_SAVE_DRAFT,
    INPUT_DRAFT_ID,
)


def handle_teams_card_submit(value: dict[str, Any], *, authorized_by: str = "kory") -> dict[str, Any]:
    """Process Adaptive Card submit payloads (editable draft + Send/Discard/Save)."""
    action = str(value.get("action") or "").strip()
    if action not in {CARD_ACTION_APPROVAL, CARD_ACTION_SAVE_DRAFT, CARD_ACTION_INVITE, CARD_ACTION_REOFFER}:
        return {
            "ok": False,
            "handled": False,
            "message": f"Unknown card action: {action or '(missing)'}",
        }

    try:
        proposal_id = int(value.get("proposal_id"))
    except (TypeError, ValueError):
        return {
            "ok": False,
            "handled": True,
            "message": "Invalid proposal in card submission.",
        }

    bundle = _fetch_bundle(proposal_id)
    draft_body = str(value.get(INPUT_DRAFT_ID) or value.get("drafted_reply") or "").strip()

    if action == CARD_ACTION_SAVE_DRAFT:
        if not draft_body:
            return {
                "ok": False,
                "handled": True,
                "message": "Draft cannot be empty.",
                "proposal_id": proposal_id,
            }
        from app.agents.inbound_reply import update_proposal_draft

        save_result = update_proposal_draft(proposal_id, draft_body)
        if not save_result.get("ok"):
            return {
                "ok": False,
                "handled": True,
                "message": save_result.get("error", "Could not save draft."),
                "proposal_id": proposal_id,
            }
        label = email_thread_label(
            subject=bundle.get("subject") if bundle else None,
            sender=bundle.get("sender") if bundle else None,
        )
        return {
            "ok": True,
            "handled": True,
            "message": f"Saved draft for **{label}**. Tap Send when ready.",
            "proposal_id": proposal_id,
        }

    if action == CARD_ACTION_REOFFER:
        from app.agents.inbound_reply import begin_reoffer_schedule

        result = begin_reoffer_schedule(proposal_id)
        if result.get("ok"):
            return {
                "ok": True,
                "handled": True,
                "message": result.get("message", "New times drafted."),
                "proposal_id": proposal_id,
            }
        return {
            "ok": False,
            "handled": True,
            "message": result.get("error", "Could not draft new times."),
            "proposal_id": proposal_id,
        }

    if action == CARD_ACTION_INVITE:
        decision = str(value.get("decision") or "approved").strip().lower()
        selected_slot = str(value.get("selected_slot") or "").strip()
        if decision != "approved":
            return {
                "ok": True,
                "handled": True,
                "message": "Invite not sent — holds remain on calendar.",
                "proposal_id": proposal_id,
            }
        from app.agents.comms_agent import execute_lexi_invite

        try:
            result = execute_lexi_invite(
                proposal_id,
                selected_slot,
                authorized_by,
                decision_source="teams_card",
            )
        except Exception as exc:
            return {
                "ok": False,
                "handled": True,
                "message": f"Could not send invite: {exc}",
                "proposal_id": proposal_id,
            }
        if result.ok:
            suffix = " (dry run)" if settings.lexi_dry_run else ""
            return {
                "ok": True,
                "handled": True,
                "message": f"Calendar invite sent{suffix}.",
                "proposal_id": proposal_id,
                "execution": result.to_dict(),
            }
        errors = ", ".join(result.errors or []) or "unknown error"
        return {
            "ok": False,
            "handled": True,
            "message": f"Invite failed: {errors}",
            "proposal_id": proposal_id,
            "execution": result.to_dict(),
        }

    decision = str(value.get("decision") or "").strip().lower()
    if decision not in {"approved", "modified", "rejected"}:
        return {
            "ok": False,
            "handled": True,
            "message": "Missing decision in card submission.",
            "proposal_id": proposal_id,
        }

    if decision != "rejected" and draft_body:
        from app.agents.inbound_reply import update_proposal_draft

        save_result = update_proposal_draft(proposal_id, draft_body)
        if not save_result.get("ok"):
            return {
                "ok": False,
                "handled": True,
                "message": save_result.get("error", "Could not save edited draft."),
                "proposal_id": proposal_id,
            }

    selected_slot = str(value.get("selected_slot") or "").strip()
    if decision != "rejected":
        item = find_pending_item(proposal_id)
        if item and (item.proposed_slots or item.holds) and not selected_slot:
            selected_slot = resolve_slot_for_option(item, 1)

    return _run_approval(
        proposal_id=proposal_id,
        decision=decision,
        selected_slot=selected_slot,
        authorized_by=authorized_by,
        decision_source="teams_card",
    )


def handle_teams_command(text: str, *, authorized_by: str = "kory") -> dict[str, Any]:
    """Parse a Teams chat line and run the matching Lexi action.

    Hermes calls this when Kory sends approve/reject/draft commands or when
    Adaptive Card ImBack posts a command to the chat.
    """
    command = parse_teams_command(text)
    if not command:
        return {
            "ok": False,
            "handled": False,
            "message": "Not a Lexi command. Hermes may reply conversationally.",
        }

    action = command.get("action")

    if action == "unresolved":
        return {
            "ok": False,
            "handled": True,
            "message": command.get("message") or "Could not match that email.",
        }

    if action == "help":
        return {"ok": True, "handled": True, "message": TEAMS_HELP_TEXT}

    if action == "pending":
        items = get_lexi_pending_queue()
        return {
            "ok": True,
            "handled": True,
            "message": format_pending_list(items),
            "pending_count": len(items),
        }

    if action == "inbound":
        items = get_inbound_reply_queue()
        return {
            "ok": True,
            "handled": True,
            "message": format_inbound_reply_list(items),
            "inbound_count": len(items),
        }

    if action == "inbox_review":
        from app.assistant.inbox_review import build_inbox_review

        review = build_inbox_review(hours=48)
        return {
            "ok": True,
            "handled": True,
            "message": review.get("kory_message", "Inbox review complete."),
            "action_count": review.get("action_count", 0),
        }

    if action == "unanswered":
        from app.assistant.briefings import build_unanswered_brief

        brief = build_unanswered_brief()
        return {"ok": True, "handled": True, "message": brief.get("kory_message", "")}

    if action == "today":
        from app.assistant.briefings import build_today_calendar_brief

        brief = build_today_calendar_brief()
        return {"ok": True, "handled": True, "message": brief.get("kory_message", "")}

    if action == "prebrief":
        from app.assistant.precall_brief import build_precall_briefs_for_today

        brief = build_precall_briefs_for_today()
        return {"ok": True, "handled": True, "message": brief.get("kory_message", "")}

    if action == "prebrief_person":
        from app.assistant.precall_brief import build_precall_brief

        who = str(command.get("who") or "").strip()
        is_email = "@" in who
        brief = build_precall_brief(
            name="" if is_email else who,
            email=who if is_email else "",
        )
        return {
            "ok": bool(brief.get("ok")),
            "handled": True,
            "message": brief.get("kory_message") or f"Couldn't build a brief for {who}.",
        }

    if action == "daily_briefing":
        # Retired — the dashboard owns the morning package. Answer with a
        # pointer rather than an unknown-command reply.
        return {
            "ok": True,
            "handled": True,
            "message": (
                "Your morning briefing lives on the dashboard now — it has the full "
                "package and stays current through the day.\n\n"
                "I can still give you **today** (calendar), **unanswered** (emails "
                "needing a reply), or **prebrief** (who you're meeting)."
            ),
        }

    if action == "outreach_list":
        from app.assistant.actions import list_outreach_campaigns_action

        result = list_outreach_campaigns_action()
        return {
            "ok": True,
            "handled": True,
            "message": result.get("kory_message", "No campaigns."),
        }

    if action == "outreach_get":
        from app.assistant.actions import get_outreach_campaign_action

        result = get_outreach_campaign_action(campaign_id=str(command.get("campaign_id") or ""))
        return {
            "ok": bool(result.get("ok")),
            "handled": True,
            "message": result.get("kory_message") or result.get("error") or "Not found.",
        }

    if action == "outreach_approve":
        from app.assistant.actions import approve_outreach_campaign_action

        result = approve_outreach_campaign_action(
            campaign_id=str(command.get("campaign_id") or ""),
            confirm=True,
        )
        return {
            "ok": bool(result.get("ok")),
            "handled": True,
            "message": result.get("kory_message") or result.get("error") or "Done.",
        }

    if action == "outreach_send":
        from app.assistant.actions import send_outreach_campaign_action

        result = send_outreach_campaign_action(
            campaign_id=str(command.get("campaign_id") or ""),
            confirm=True,
        )
        return {
            "ok": bool(result.get("ok")),
            "handled": True,
            "message": result.get("kory_message") or result.get("error") or "Send blocked.",
        }

    if action == "draft_no":
        proposal_id = int(command["proposal_id"])
        bundle = _fetch_bundle(proposal_id)
        result = decline_reply(proposal_id, reason="Declined via Teams")
        if result.get("ok"):
            return {
                "ok": True,
                "handled": True,
                "message": action_confirmation_message(
                    action="draft_no",
                    subject=bundle.get("subject") if bundle else None,
                    sender=bundle.get("sender") if bundle else None,
                ),
                "proposal_id": proposal_id,
            }
        return {
            "ok": False,
            "handled": True,
            "message": result.get("error", "Could not skip."),
            "proposal_id": proposal_id,
        }

    if action == "show_draft":
        proposal_id = int(command["proposal_id"])
        return _show_draft_message(proposal_id)

    if action == "draft_yes":
        proposal_id = int(command["proposal_id"])
        existing = find_pending_item(proposal_id)
        if existing:
            return _show_draft_message(
                proposal_id,
                prefix="Draft already ready — nothing sent.",
            )
        result = begin_draft_reply(proposal_id)
        if not result.get("ok"):
            return {
                "ok": False,
                "handled": True,
                "message": result.get("error", "Draft failed."),
                "proposal_id": proposal_id,
            }
        from app.bot.teams_format import format_draft_ready_text

        bundle = _fetch_bundle(proposal_id)
        draft = (result.get("drafted_reply") or "").strip()
        if not draft and bundle:
            draft = str(bundle.get("drafted_reply") or "").strip()
        lines = []
        if bundle and draft:
            lines.append(
                format_draft_ready_text(
                    subject=bundle.get("subject"),
                    sender=bundle.get("sender"),
                    draft=draft,
                    slots=_parse_slots(bundle.get("proposed_slots")),
                    voice_mode=str(bundle.get("voice_mode") or "kory"),
                )
            )
        else:
            lines.append(result.get("message", "Draft ready."))
        item = find_pending_item(proposal_id)
        if item:
            from app.bot.teams_publisher import schedule_teams_approval_push

            schedule_teams_approval_push(proposal_id)
        return {
            "ok": True,
            "handled": True,
            "message": "\n".join(lines),
            "proposal_id": proposal_id,
            "status": result.get("status"),
        }

    if action == "approve":
        proposal_id = int(command["proposal_id"])
        option = int(command.get("option") or 1)
        item = find_pending_item(proposal_id)
        if not item:
            bundle = _fetch_bundle(proposal_id)
            return {
                "ok": False,
                "handled": True,
                "message": action_confirmation_message(
                    action="approve",
                    subject=bundle.get("subject") if bundle else command.get("subject"),
                    sender=bundle.get("sender") if bundle else command.get("sender"),
                    success=False,
                    detail="No draft is pending approval for this email.",
                ),
                "proposal_id": proposal_id,
            }
        selected_slot = resolve_slot_for_option(item, option)
        return _run_approval(
            proposal_id=proposal_id,
            decision="approved",
            selected_slot=selected_slot,
            authorized_by=authorized_by,
            decision_source="hermes_teams_text",
        )

    if action == "reject":
        proposal_id = int(command["proposal_id"])
        bundle = _fetch_bundle(proposal_id)
        if not find_pending_item(proposal_id):
            return {
                "ok": False,
                "handled": True,
                "message": action_confirmation_message(
                    action="reject",
                    subject=bundle.get("subject") if bundle else command.get("subject"),
                    sender=bundle.get("sender") if bundle else command.get("sender"),
                    success=False,
                    detail="No draft is pending approval for this email.",
                ),
                "proposal_id": proposal_id,
            }
        return _run_approval(
            proposal_id=proposal_id,
            decision="rejected",
            selected_slot="",
            authorized_by=authorized_by,
            decision_source="hermes_teams_text",
        )

    return {"ok": False, "handled": False, "message": f"Unknown action: {action}"}


def _fetch_bundle(proposal_id: int) -> dict[str, Any] | None:
    from app.storage.lexi_db import get_lexi_connection

    with get_lexi_connection() as conn:
        row = conn.execute(
            """
            SELECT p.drafted_reply, p.proposed_slots, p.voice_mode, e.subject, e.sender
            FROM proposals p
            INNER JOIN email_threads e ON e.thread_id = p.thread_id
            WHERE p.id = ?
            """,
            (proposal_id,),
        ).fetchone()
    return dict(row) if row else None


def _parse_slots(raw: Any) -> list | None:
    import json

    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else None
        except json.JSONDecodeError:
            return None
    return None


def _show_draft_message(proposal_id: int, *, prefix: str = "") -> dict[str, Any]:
    item = find_pending_item(proposal_id)
    if item:
        lines = []
        if prefix:
            lines.append(prefix)
        lines.append(format_approval_notification(item))
        lines.append("\n_Not sent._ Edit in chat or approve when ready.")
        return {
            "ok": True,
            "handled": True,
            "message": "\n".join(lines),
            "proposal_id": proposal_id,
            "status": "pending_approval",
        }
    from app.agents.inbound_reply import get_inbound_reply_queue

    for row in get_inbound_reply_queue():
        if int(row.get("proposal_id") or 0) == proposal_id:
            label = email_thread_label(
                subject=row.get("subject"),
                sender=row.get("sender"),
            )
            return {
                "ok": True,
                "handled": True,
                "message": (
                    f"**{label}** is still waiting for your draft decision.\n"
                    "Use the card buttons or ask me to draft a reply in chat."
                ),
                "proposal_id": proposal_id,
                "status": "awaiting_reply_prompt",
            }
    return {
        "ok": False,
        "handled": True,
        "message": "No draft found for that email. Try `pending` or `inbound`.",
        "proposal_id": proposal_id,
    }


def _run_approval(
    *,
    proposal_id: int,
    decision: str,
    selected_slot: str,
    authorized_by: str,
    decision_source: str,
) -> dict[str, Any]:
    bundle = _fetch_bundle(proposal_id)
    try:
        result = execute_lexi_approval(
            proposal_id,
            decision,
            selected_slot,
            authorized_by,
            decision_source=decision_source,
        )
    except Exception as exc:
        return {
            "ok": False,
            "handled": True,
            "message": action_confirmation_message(
                action=decision,
                subject=bundle.get("subject") if bundle else None,
                sender=bundle.get("sender") if bundle else None,
                success=False,
                detail=f"Could not execute: {exc}",
            ),
            "proposal_id": proposal_id,
        }

    if result.ok:
        suffix = " (dry run — nothing sent to Outlook)" if settings.lexi_dry_run else ""
        return {
            "ok": True,
            "handled": True,
            "message": action_confirmation_message(
                action=decision,
                subject=bundle.get("subject") if bundle else None,
                sender=bundle.get("sender") if bundle else None,
                success=True,
                detail=suffix,
            ),
            "proposal_id": proposal_id,
            "execution": result.to_dict(),
        }

    errors = ", ".join(result.errors or []) or "unknown error"
    return {
        "ok": False,
        "handled": True,
        "message": action_confirmation_message(
            action=decision,
            subject=bundle.get("subject") if bundle else None,
            sender=bundle.get("sender") if bundle else None,
            success=False,
            detail=errors,
        ),
        "proposal_id": proposal_id,
        "execution": result.to_dict(),
    }


def format_approval_card_followup(proposal_id: int) -> str | None:
    """Optional text summary after pushing an Adaptive Card."""
    item = find_pending_item(proposal_id)
    if item is None:
        return None
    return format_approval_notification(item)
