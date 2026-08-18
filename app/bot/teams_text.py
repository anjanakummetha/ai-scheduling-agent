"""Teams text summaries and chat-based approve/reject commands."""

from __future__ import annotations

import re
from typing import Any

from app.agents.comms_agent import LexiQueueItem, get_lexi_pending_queue
from app.bot.teams_labels import (
    parse_human_teams_command,
    unresolved_message,
)

# How Kory refers to a draft. Lexi tells him to type "approve draft 1" — in the
# help text, in the footer of every `pending` list, in the "more than one draft
# is waiting" prompt, and in the stuck-proposal nudge — but the pattern only
# accepted "approve 1". The advertised phrasing fell through to "Not a Lexi
# command" on the primary approval surface.
#
# tests/test_advertised_commands_parse.py now asserts that everything Lexi tells
# him to type actually parses, so the copy and the parser cannot drift apart
# again.
_REF = r"(?:draft\s+|email\s+|number\s+|no\.?\s+)?#?(\d+)"

_APPROVE_RE = re.compile(
    rf"^(?:approve|yes|send)\s+{_REF}(?:\s+option\s+(\d+))?$",
    re.IGNORECASE,
)
_SEND_ONLY_RE = re.compile(r"^send$", re.IGNORECASE)
_REJECT_RE = re.compile(
    rf"^(?:reject|no|discard)\s+{_REF}(?:\s*[—\-:]\s*(.+))?$",
    re.IGNORECASE,
)
_CANCEL_MEETING_RE = re.compile(
    r"^cancel(?:\s+(?:the\s+)?(?:meeting|invite|call))?\s+(?:for\s+)?#?(\d+)"
    r"(?:\s*[—\-:]\s*(.+))?$",
    re.IGNORECASE,
)
_PENDING_RE = re.compile(r"^(?:pending|queue|status)$", re.IGNORECASE)
_DRAFT_YES_RE = re.compile(
    r"^(?:draft|reply)\s+#?(\d+)(?:\s+yes)?$",
    re.IGNORECASE,
)
_DRAFT_NO_RE = re.compile(
    r"^(?:draft|reply|skip)\s+#?(\d+)\s+(?:no|skip)$",
    re.IGNORECASE,
)
_INBOUND_RE = re.compile(r"^(?:inbound|new|emails)$", re.IGNORECASE)
_INBOX_REVIEW_RE = re.compile(r"^inbox\s+review$", re.IGNORECASE)
_UNANSWERED_RE = re.compile(r"^(?:unanswered|unanswered\s+emails?)$", re.IGNORECASE)
_TODAY_RE = re.compile(r"^(?:today|calendar\s+today|today'?s?\s+calendar)$", re.IGNORECASE)
_PREBRIEF_RE = re.compile(r"^(?:prebrief|pre-?meeting(?:\s+brief)?s?|pre-?call briefs?)$", re.IGNORECASE)
# Same shortcuts followed by a person: "prebrief Ramzi Dagher", "prebrief on Jane Doe".
_PREBRIEF_PERSON_RE = re.compile(
    r"^(?:prebrief|pre-?meeting brief|pre-?call brief)\s+(?:me\s+)?(?:on|for|about)?\s*(?P<who>.+)$",
    re.IGNORECASE,
)
_BRIEFING_RE = re.compile(r"^(?:brief|briefing|ceo\s+brief|morning\s+brief)$", re.IGNORECASE)
_OUTREACH_LIST_RE = re.compile(r"^outreach(?:\s+list)?$", re.IGNORECASE)
_OUTREACH_GET_RE = re.compile(r"^outreach\s+(camp-[a-z0-9]+)$", re.IGNORECASE)
_APPROVE_OUTREACH_RE = re.compile(r"^approve\s+outreach\s+(camp-[a-z0-9]+)$", re.IGNORECASE)
_SEND_OUTREACH_RE = re.compile(r"^send\s+outreach\s+(camp-[a-z0-9]+)$", re.IGNORECASE)
_HELP_RE = re.compile(r"^(?:help|\?)$", re.IGNORECASE)
# A bare confirmation with no proposal number ("YES", "ok close it") — arrives
# with no context because escalations are proactive pushes (live D6). Routed to
# a disambiguating answer that names the open escalation, never to an action.
_BARE_ACK_RE = re.compile(
    r"^(?:yes|yep|yeah|ok|okay|confirmed?|sounds good|sure)(?:[,.!\s]+(?:close|confirm|do)(?:\s+(?:it|that|out))?)?[.!\s]*$",
    re.IGNORECASE,
)
_SHOW_DRAFT_RE = re.compile(
    rf"^(?:show|view|display)(?:\s+me)?(?:\s+the)?\s+(?:draft(?:\s+(?:for|on))?\s+)?{_REF}$",
    re.IGNORECASE,
)


def format_pending_approval_digest(items: list) -> str:
    """Hermes/Teams-friendly queue summary with clear line breaks."""
    from app.bot.teams_format import display_sender, display_subject

    count = len(items)
    if count == 0:
        return "No drafts waiting for approval."
    header = (
        f"Hey Kory! You have **{count} pending scheduling request"
        f"{'s' if count != 1 else ''}** awaiting your approval:\n"
    )
    lines = [header]
    for item in items[:10]:
        subject = display_subject(getattr(item, "subject", None) or item.get("subject"))
        sender_raw = getattr(item, "sender", None) or item.get("sender") or "unknown"
        sender = display_sender(str(sender_raw))
        intent = getattr(item, "intent_classification", None) or item.get("intent_classification") or "unknown"
        from app.scheduling.meeting_type import resolve_meeting_type

        spec = resolve_meeting_type(
            intent=str(intent),
            subject=str(getattr(item, "subject", None) or item.get("subject") or ""),
            body=str(getattr(item, "raw_body", None) or item.get("raw_body") or ""),
        )
        type_label = spec.card_type_label()
        lines.append(f"📇 **{subject}**")
        lines.append(f"**From:** {sender} ({sender_raw})")
        lines.append(f"**Type:** {type_label}")
        body = (getattr(item, "raw_body", None) or item.get("raw_body") or "").strip()
        if body:
            preview = body.replace("\n", " ")
            if len(preview) > 220:
                preview = preview[:217] + "…"
            lines.append(f"> {preview}")
        lines.append("")
    return "\n".join(lines).rstrip()


def format_pending_list(
    items: list[LexiQueueItem],
    *,
    invite_items: list[LexiQueueItem] | None = None,
) -> str:
    invite_items = invite_items or []
    if not items and not invite_items:
        return "No drafts waiting to send."
    from app.bot.teams_format import display_subject, display_sender

    lines: list[str] = []
    if invite_items:
        lines.append("**Invites ready to send** (recipient picked a time)\n")
        for item in invite_items[:15]:
            lines.append(
                f"• **#{item.proposal_id} — {display_subject(item.subject)}** — "
                f"{display_sender(item.sender)} accepted — "
                f"say **approve #{item.proposal_id}** to send the invite"
            )
        lines.append("")
    if items:
        from app.bot.draft_numbering import record_pending_snapshot

        # Recording happens HERE, where the numbers are produced. At the call
        # site a new way of rendering the list would silently skip it and the
        # numbers would go back to meaning whatever the queue says later.
        record_pending_snapshot([item.proposal_id for item in items[:15]])
        lines.append("**Drafts ready**\n")
        for position, item in enumerate(items[:15], start=1):
            # The number Kory types is the position in this list, not the raw
            # proposal id — "approve draft 1" beats "approve #9868". Falls back
            # to the position so the list is never rendered with a raw id.
            ref = getattr(item, "draft_number", None) or position
            lines.append(
                f"• **draft {ref} — {display_subject(item.subject)}** — "
                f"from {display_sender(item.sender)}"
            )
        if len(items) > 15:
            lines.append(f"_…and {len(items) - 15} more._")
    lines.append(
        "\nSay **show draft N**, **approve draft N**, or **reject draft N — reason**."
    )
    return "\n".join(lines)


def format_approval_notification(item: LexiQueueItem) -> str:
    return _format_approval_text(item, include_draft=True)


def format_scheduling_guidance_notification(
    *,
    subject: str,
    sender: str,
    summary: str,
    intent: str = "",
) -> str:
    from app.bot.teams_format import display_sender, display_subject

    who = display_sender(sender)
    topic = display_subject(subject)
    line = (summary or "I couldn't find a slot in that window.").strip().rstrip(".")
    return f"**{topic}** ({who})\n\n{line}."


def format_reply_prompt_notification(item: dict) -> str:
    """Notify Kory about a new inbound email — ask before drafting."""
    from app.bot.teams_format import format_reply_prompt_card_text

    return format_reply_prompt_card_text(item)


def format_inbound_reply_list(items: list[dict]) -> str:
    if not items:
        return "No emails waiting for a draft decision."
    from app.bot.teams_format import display_subject, display_sender

    lines = ["**New mail**\n"]
    for item in items[:15]:
        pid = item.get("proposal_id")
        label = f"#{pid} — " if pid else ""
        lines.append(
            f"• **{label}{display_subject(item.get('subject'))}** — "
            f"from {display_sender(item.get('sender'))}"
        )
    lines.append("\nSay **draft #N** to draft a reply, or **draft #N no** to skip.")
    return "\n".join(lines)


def _format_approval_text(item: LexiQueueItem, *, include_draft: bool) -> str:
    from app.bot.teams_format import display_sender, display_subject, format_draft_ready_text

    if include_draft and (item.drafted_reply or "").strip():
        return format_draft_ready_text(
            subject=item.subject,
            sender=item.sender,
            draft=item.drafted_reply or "",
            slots=item.proposed_slots or None,
            voice_mode=str(item.voice_mode or "kory"),
            proposal_id=item.proposal_id,
            scheduling_note=str(getattr(item, "scheduling_note", "") or ""),
        )
    return (
        f"**#{item.proposal_id} — {display_subject(item.subject)}**\n"
        f"From {display_sender(item.sender)}\n\n"
        "_Draft in progress — ask me to show it when ready._"
    )


def parse_teams_command(text: str) -> dict[str, Any] | None:
    """Parse a Teams chat line into an approval command, or None."""
    normalized = (text or "").strip()
    if not normalized:
        return None

    human = parse_human_teams_command(normalized)
    if human:
        if human.get("unresolved"):
            return {
                "action": "unresolved",
                "original_action": human["action"],
                "subject": human["subject"],
                "sender": human["sender"],
                "message": unresolved_message(
                    action=human["action"],
                    subject=human["subject"],
                    sender=human["sender"],
                ),
            }
        return human

    if _HELP_RE.match(normalized):
        return {"action": "help"}

    if _BARE_ACK_RE.match(normalized):
        return {"action": "bare_ack", "text": normalized}

    if _PENDING_RE.match(normalized):
        return {"action": "pending"}

    if _INBOUND_RE.match(normalized):
        return {"action": "inbound"}

    if _INBOX_REVIEW_RE.match(normalized):
        return {"action": "inbox_review"}

    if _UNANSWERED_RE.match(normalized):
        return {"action": "unanswered"}

    if _TODAY_RE.match(normalized):
        return {"action": "today"}

    if _PREBRIEF_RE.match(normalized):
        return {"action": "prebrief"}

    # "prebrief Ramzi Dagher" / "pre-call brief on Jane Doe"
    person_match = _PREBRIEF_PERSON_RE.match(normalized)
    if person_match:
        who = person_match.group("who").strip(" .?!")
        if who:
            return {"action": "prebrief_person", "who": who}

    if _BRIEFING_RE.match(normalized):
        return {"action": "daily_briefing"}

    if _OUTREACH_LIST_RE.match(normalized):
        return {"action": "outreach_list"}

    get_outreach = _OUTREACH_GET_RE.match(normalized)
    if get_outreach:
        return {"action": "outreach_get", "campaign_id": get_outreach.group(1)}

    approve_outreach = _APPROVE_OUTREACH_RE.match(normalized)
    if approve_outreach:
        return {
            "action": "outreach_approve",
            "campaign_id": approve_outreach.group(1),
        }

    send_outreach = _SEND_OUTREACH_RE.match(normalized)
    if send_outreach:
        return {
            "action": "outreach_send",
            "campaign_id": send_outreach.group(1),
        }

    if _SEND_ONLY_RE.match(normalized):
        from app.agents.comms_agent import get_lexi_invite_queue

        # Bare `send` must see invites too (live D-4: one pending_invite and
        # zero drafts left `send` unresolved and Hermes asked for a number).
        pending = list(get_lexi_pending_queue()) + list(get_lexi_invite_queue())
        if len(pending) == 1:
            return {
                "action": "approve",
                "proposal_id": pending[0].proposal_id,
                "option": 1,
            }
        if not pending:
            return {
                "action": "unresolved",
                "message": "Nothing is waiting to send right now.",
            }
        if len(pending) > 1:
            ids = ", ".join(f"#{p.proposal_id}" for p in pending[:10])
            return {
                "action": "unresolved",
                "message": (
                    f"More than one draft is waiting ({ids}) — say **approve draft N** "
                    "for the one you mean, or **pending** to review them."
                ),
            }

    draft_no = _DRAFT_NO_RE.match(normalized)
    if draft_no:
        return {
            "action": "draft_no",
            "proposal_id": int(draft_no.group(1)),
        }

    draft_yes = _DRAFT_YES_RE.match(normalized)
    if draft_yes:
        return {
            "action": "draft_yes",
            "proposal_id": int(draft_yes.group(1)),
        }

    show_draft = _SHOW_DRAFT_RE.match(normalized)
    if show_draft:
        reference = resolve_pending_reference(int(show_draft.group(1)))
        if reference.problem:
            return {"action": "unresolved", "message": reference.problem}
        return {
            "action": "show_draft",
            "proposal_id": reference.proposal_id,
        }

    approve = _APPROVE_RE.match(normalized)
    if approve:
        reference = resolve_pending_reference(int(approve.group(1)))
        if reference.problem:
            return {"action": "unresolved", "message": reference.problem}
        return {
            "action": "approve",
            "proposal_id": reference.proposal_id,
            "option": int(approve.group(2)) if approve.group(2) else 1,
        }

    reject = _REJECT_RE.match(normalized)
    if reject:
        reference = resolve_pending_reference(int(reject.group(1)))
        if reference.problem:
            return {"action": "unresolved", "message": reference.problem}
        return {
            "action": "reject",
            "proposal_id": reference.proposal_id,
            "reason": (reject.group(2) or "").strip(),
        }

    cancel = _CANCEL_MEETING_RE.match(normalized)
    if cancel:
        return {
            "action": "cancel_meeting",
            "proposal_id": int(cancel.group(1)),
            "reason": (cancel.group(2) or "").strip(),
        }

    return None


def resolve_slot_for_option(item: LexiQueueItem, option: int) -> str:
    """Map 1-based option number to ISO slot start."""
    index = max(1, option) - 1
    holds = item.holds or []
    if index < len(holds):
        return str(holds[index].get("slot_start") or "")
    slots = item.proposed_slots or []
    if index < len(slots):
        return str(slots[index].get("start") or "")
    if slots:
        return str(slots[0].get("start") or "")
    return ""


def find_pending_item(proposal_id: int) -> LexiQueueItem | None:
    for item in get_lexi_pending_queue():
        if item.proposal_id == proposal_id:
            return item
    return None


def resolve_pending_ref(number: int) -> int:
    """Map what Kory typed to a proposal id.

    See app/bot/draft_numbering.py for why this resolves against the list he was
    SHOWN rather than a fresh read of the queue. This wrapper keeps the old
    signature for callers that only need the id; `resolve_pending_reference`
    carries the explanation when the number no longer means anything.
    """
    return resolve_pending_reference(number).proposal_id or number


def resolve_pending_reference(number: int):
    """The full answer: the proposal, or why the number no longer names one."""
    from app.bot.draft_numbering import DraftReference, resolve_draft_number

    try:
        live_ids = [item.proposal_id for item in get_lexi_pending_queue()]
    except Exception:  # a DB hiccup must not swallow the command
        return DraftReference(proposal_id=number)
    return resolve_draft_number(number, live_queue_ids=live_ids)


def find_pending_item_by_label(*, subject: str, sender: str) -> LexiQueueItem | None:
    from app.bot.teams_labels import resolve_proposal_id

    proposal_id = resolve_proposal_id(
        subject=subject,
        sender=sender,
        prefer_pending_approval=True,
    )
    if proposal_id is None:
        return None
    return find_pending_item(proposal_id)


TEAMS_HELP_TEXT = """**Lexi**
- Ask naturally: "draft a reply to Dan about payroll"
- `pending` — drafts ready to send (and invites ready to dispatch)
- `cancel meeting #N` — cancel a booked meeting (attendee gets the notice)
- `inbound` — mail waiting for your draft yes/no
- `inbox review` — last 48 hours of activity + what needs action
- `unanswered` — emails you may still need to reply to
- `today` — today's calendar
- `prebrief` — pre-meeting briefs (who introduced + context)
- `brief` — points you to the dashboard (the dashboard owns the morning briefing)
- `approve draft N` / `reject draft N — reason` / `show draft N` — typed commands are the approval surface
- Draft numbers are the small numbers in `pending` (1, 2, 3…) and renumber as drafts clear
- Approve only when you want it sent — nothing sends without it

Notifications: when you CC Lexi, when someone replies on a Lexi thread, or important scheduling mail."""
