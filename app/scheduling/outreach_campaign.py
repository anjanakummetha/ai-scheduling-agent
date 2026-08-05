"""Mass outreach campaigns — draft in Outlook, approve later in Teams; no auto-send."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from app.config import settings
from app.storage.lexi_db import get_lexi_connection

# Built-in campaign kits Kory can reuse / customize.
CAMPAIGN_TEMPLATES: dict[str, dict[str, str]] = {
    "ypo_the_turn": {
        "label": "YPO Construction Network — The Turn podcast intros",
        "subject": "Quick ask from a fellow YPOer — The Turn podcast",
        "body": (
            "{first_name},\n\n"
            "{opener}\n\n"
            "I am a lifelong specialty contractor and fellow YPOer (Rocky Mountains). "
            "I took a small family business and built a national environmental services "
            "platform (EIS Holdings). We sold the business in 2021, and I left the CEO "
            "role in early 2024. Since that time, I launched Iconic Founders Group, which "
            "supports blue-collar industry founders through the transaction/exit process. "
            "I decided to launch a podcast, 'the Turn', that focuses solely on founders "
            "across the trades (specifically: specialty contractors or adjacent businesses "
            "within construction) who have gone through a transaction process. I am asking "
            "YPOers for referrals to great folks you may know who have gone through selling "
            "their business, who may be open to talking about the experience with me - "
            "good or bad. I appreciate the help.\n\n"
            "I would also love it if you would subscribe here.\n\n"
            "And, for more information on me, I recently appeared on Moneywise and shared "
            "my story. You can find it here.\n\n"
            "Grateful for any intros.\n\n"
            "Kory"
        ),
        "default_opener": (
            "I got your info from the YPO Construction Network directory."
        ),
    },
    "generic": {
        "label": "Generic outreach",
        "subject": "Quick note — {first_name}",
        "body": (
            "Hi {first_name},\n\n"
            "{opener}\n\n"
            "{goal_paragraph}\n\n"
            "Would you be open to a brief conversation?\n\n"
            "Best,\n"
            "Kory"
        ),
        "default_opener": "I wanted to reach out.",
    },
}


def _ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS outreach_campaigns (
            campaign_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            goal TEXT,
            template_key TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'drafting',
            audience_json TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT,
            approved_at TEXT,
            notes TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS outreach_drafts (
            draft_id TEXT PRIMARY KEY,
            campaign_id TEXT NOT NULL,
            recipient_email TEXT NOT NULL,
            recipient_name TEXT,
            company TEXT,
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            research_notes TEXT,
            outlook_draft_id TEXT,
            status TEXT NOT NULL DEFAULT 'staged',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            sent_at TEXT,
            FOREIGN KEY (campaign_id) REFERENCES outreach_campaigns(campaign_id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_outreach_drafts_campaign
        ON outreach_drafts (campaign_id)
        """
    )


def campaigns_paused() -> bool:
    """Whether the whole campaign feature is parked.

    Campaigns are shelved until someone picks the feature back up: mass outreach
    is a second risk surface with its own approval path, and nothing downstream
    needs it right now. This is the outer switch — sends have their own kill
    switch below, but a paused feature never gets far enough to consult it.

    Read from the environment at call time, matching outreach_sends_blocked(),
    so flipping LEXI_OUTREACH_CAMPAIGNS_ENABLED=true and restarting is the whole
    of turning it back on.
    """
    import os

    return os.getenv("LEXI_OUTREACH_CAMPAIGNS_ENABLED", "false").lower() not in {
        "1",
        "true",
        "yes",
    }


def _paused_response(did_not: str) -> dict[str, Any]:
    """Refusal shaped like every other campaign return, so callers need no new branch."""
    return {
        "ok": False,
        "paused": True,
        "campaign_id": None,
        "draft_count": 0,
        "sent": 0,
        "sends_blocked": True,
        "error": "Outreach campaigns are paused (LEXI_OUTREACH_CAMPAIGNS_ENABLED=false).",
        "kory_message": (
            f"Outreach campaigns are switched off at the moment, so I haven't {did_not}. "
            "It's parked rather than broken — we can turn it back on whenever you want it."
        ),
    }


def outreach_sends_blocked() -> bool:
    """Hard block for UAT — no campaign sends until explicitly enabled later."""
    if campaigns_paused():
        return True
    if settings.lexi_dry_run:
        return True
    if settings.lexi_kory_outbound_blocked:
        return True
    if settings.lexi_kory_space_read_only:
        return True
    # Separate kill switch default false until Kory enables campaigns.
    import os

    return os.getenv("LEXI_OUTREACH_LIVE_SENDS_ENABLED", "false").lower() not in {
        "1",
        "true",
        "yes",
    }


def parse_pasted_contacts(text: str) -> list[dict[str, str]]:
    """Parse pasted audience: 'Name, email, company' or 'email' per line."""
    contacts: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.lower().startswith(("name,", "name\t", "#")):
            continue
        email_match = re.search(r"[\w.+-]+@[\w.-]+\.\w+", line)
        if not email_match:
            continue
        email = email_match.group(0).lower()
        if email in seen:
            continue
        seen.add(email)
        before = line[: email_match.start()].strip(" ,;\t-|")
        after = line[email_match.end() :].strip(" ,;\t-|")
        name = before or email.split("@", 1)[0].replace(".", " ").title()
        company = after
        # CSV style: Name, email, company
        parts = [p.strip() for p in re.split(r"[,;\t]", line) if p.strip()]
        if len(parts) >= 2 and "@" in parts[1]:
            name = parts[0]
            email = parts[1].lower()
            company = parts[2] if len(parts) >= 3 else company
        contacts.append(
            {
                "name": name,
                "email": email,
                "company": company,
                "notes": "",
            }
        )
    return contacts


def create_outreach_campaign(
    *,
    name: str,
    goal: str = "",
    template_key: str = "generic",
    contacts: list[dict[str, str]] | None = None,
    pasted_list: str = "",
    hubspot_limit: int = 0,
    hubspot_lifecycle: str = "",
    include_research: bool = False,
    custom_opener: str = "",
    custom_subject: str = "",
) -> dict[str, Any]:
    """Create campaign + compose personalized drafts (staged locally; Outlook drafts dry-run)."""
    if campaigns_paused():
        return _paused_response("staged anything")
    template_key = (template_key or "generic").strip().lower()
    if template_key not in CAMPAIGN_TEMPLATES:
        template_key = "generic"
    template = CAMPAIGN_TEMPLATES[template_key]

    audience = list(contacts or [])
    if pasted_list.strip():
        audience.extend(parse_pasted_contacts(pasted_list))
    if hubspot_limit > 0:
        audience.extend(_hubspot_audience(limit=hubspot_limit, lifecycle=hubspot_lifecycle))

    # Dedupe by email
    deduped: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in audience:
        email = (row.get("email") or "").strip().lower()
        if not email or email in seen:
            continue
        seen.add(email)
        deduped.append(
            {
                "name": (row.get("name") or email.split("@")[0]).strip(),
                "email": email,
                "company": (row.get("company") or "").strip(),
                "notes": (row.get("notes") or "").strip(),
            }
        )

    if not deduped:
        return {
            "ok": False,
            "error": "No contacts found. Paste a list or set hubspot_limit.",
        }

    campaign_id = f"camp-{uuid.uuid4().hex[:10]}"
    with get_lexi_connection() as conn:
        _ensure_tables(conn)
        conn.execute(
            """
            INSERT INTO outreach_campaigns (
                campaign_id, name, goal, template_key, status, audience_json, notes
            ) VALUES (?, ?, ?, ?, 'staged', ?, ?)
            """,
            (
                campaign_id,
                name.strip() or template["label"],
                goal.strip(),
                template_key,
                json.dumps(deduped),
                "Outlook drafts staged locally; live send blocked until approve + live flag.",
            ),
        )

        draft_rows: list[dict[str, Any]] = []
        for contact in deduped:
            composed = compose_outreach_email(
                contact=contact,
                template_key=template_key,
                goal=goal,
                custom_opener=custom_opener,
                custom_subject=custom_subject,
                include_research=include_research,
            )
            draft_id = f"od-{uuid.uuid4().hex[:12]}"
            outlook_id = _stage_outlook_draft(
                to_email=contact["email"],
                subject=composed["subject"],
                body=composed["body"],
            )
            conn.execute(
                """
                INSERT INTO outreach_drafts (
                    draft_id, campaign_id, recipient_email, recipient_name, company,
                    subject, body, research_notes, outlook_draft_id, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'staged')
                """,
                (
                    draft_id,
                    campaign_id,
                    contact["email"],
                    contact["name"],
                    contact.get("company") or "",
                    composed["subject"],
                    composed["body"],
                    composed.get("research_notes") or "",
                    outlook_id,
                ),
            )
            draft_rows.append(
                {
                    "draft_id": draft_id,
                    "email": contact["email"],
                    "name": contact["name"],
                    "subject": composed["subject"],
                    "outlook_draft_id": outlook_id,
                    "preview": composed["body"][:280],
                }
            )
        conn.commit()

    return {
        "ok": True,
        "campaign_id": campaign_id,
        "name": name.strip() or template["label"],
        "template_key": template_key,
        "status": "staged",
        "draft_count": len(draft_rows),
        "sends_blocked": outreach_sends_blocked(),
        "drafts": draft_rows,
        "kory_message": _campaign_summary_message(
            campaign_id=campaign_id,
            name=name.strip() or template["label"],
            draft_count=len(draft_rows),
            sample=draft_rows[:3],
        ),
    }


def compose_outreach_email(
    *,
    contact: dict[str, str],
    template_key: str = "generic",
    goal: str = "",
    custom_opener: str = "",
    custom_subject: str = "",
    include_research: bool = False,
) -> dict[str, str]:
    template = CAMPAIGN_TEMPLATES.get(template_key) or CAMPAIGN_TEMPLATES["generic"]
    first = (contact.get("name") or "there").split()[0]
    company = (contact.get("company") or "").strip()
    research_notes = ""
    opener = custom_opener.strip()

    if include_research:
        research_notes = _light_research_note(contact)
        if research_notes and not opener:
            opener = research_notes.split("\n", 1)[0][:220]

    if not opener:
        if template_key == "ypo_the_turn":
            opener = template["default_opener"]
            if company:
                opener = (
                    f"I got your info from the YPO Construction Network directory "
                    f"— and noticed your work with {company}."
                )
        elif company:
            opener = f"I wanted to reach out regarding {company}."
        elif goal.strip():
            opener = f"I wanted to reach out about {goal.strip()}."
        else:
            opener = template["default_opener"]

    goal_paragraph = goal.strip() or (
        "I'd love to connect and see if there's a useful introduction either of us can make."
    )
    subject = (custom_subject or template["subject"]).format(
        first_name=first,
        company=company or "your work",
    )
    body = template["body"].format(
        first_name=first,
        opener=opener,
        goal_paragraph=goal_paragraph,
        company=company or "your company",
    )
    return {
        "subject": subject,
        "body": body,
        "research_notes": research_notes,
    }


def get_campaign(campaign_id: str) -> dict[str, Any] | None:
    with get_lexi_connection() as conn:
        _ensure_tables(conn)
        row = conn.execute(
            "SELECT * FROM outreach_campaigns WHERE campaign_id = ?",
            (campaign_id,),
        ).fetchone()
        if not row:
            return None
        drafts = conn.execute(
            """
            SELECT draft_id, recipient_email, recipient_name, company, subject, body,
                   outlook_draft_id, status, research_notes
            FROM outreach_drafts WHERE campaign_id = ?
            ORDER BY created_at
            """,
            (campaign_id,),
        ).fetchall()
    return {
        "campaign": dict(row),
        "drafts": [dict(d) for d in drafts],
        "sends_blocked": outreach_sends_blocked(),
    }


def list_campaigns(*, limit: int = 20) -> dict[str, Any]:
    with get_lexi_connection() as conn:
        _ensure_tables(conn)
        rows = conn.execute(
            """
            SELECT c.campaign_id, c.name, c.status, c.template_key, c.goal, c.created_at,
                   (SELECT COUNT(*) FROM outreach_drafts d WHERE d.campaign_id = c.campaign_id) AS draft_count
            FROM outreach_campaigns c
            ORDER BY c.created_at DESC
            LIMIT ?
            """,
            (max(1, min(limit, 50)),),
        ).fetchall()
    items = [dict(r) for r in rows]
    lines = ["**Outreach campaigns**\n"]
    if not items:
        lines.append("_None yet. Ask Hermes to start a campaign with a pasted list or HubSpot._")
    for item in items:
        lines.append(
            f"• `{item['campaign_id']}` — **{item['name']}** "
            f"({item['draft_count']} drafts, {item['status']})"
        )
    lines.append(
        "\n_Drafts are staged for Outlook. Say `approve outreach <id>` when ready "
        "(sends stay blocked until you enable live outreach)._"
    )
    return {"ok": True, "campaigns": items, "kory_message": "\n".join(lines)}


def approve_outreach_campaign(*, campaign_id: str, approved_by: str = "kory") -> dict[str, Any]:
    """Mark campaign approved. Does NOT send while outreach sends are blocked."""
    if campaigns_paused():
        return _paused_response("marked that campaign approved")
    detail = get_campaign(campaign_id)
    if not detail:
        return {"ok": False, "error": f"Unknown campaign {campaign_id}"}

    with get_lexi_connection() as conn:
        _ensure_tables(conn)
        conn.execute(
            """
            UPDATE outreach_campaigns
            SET status = 'approved', approved_at = datetime('now'), updated_at = datetime('now'),
                notes = COALESCE(notes, '') || ?
            WHERE campaign_id = ?
            """,
            (f"\nApproved by {approved_by} at {datetime.now(timezone.utc).isoformat()}", campaign_id),
        )
        conn.execute(
            """
            UPDATE outreach_drafts SET status = 'approved'
            WHERE campaign_id = ? AND status = 'staged'
            """,
            (campaign_id,),
        )
        conn.commit()

    if outreach_sends_blocked():
        return {
            "ok": True,
            "campaign_id": campaign_id,
            "status": "approved",
            "sent": 0,
            "sends_blocked": True,
            "kory_message": (
                f"Campaign `{campaign_id}` marked **approved**. "
                f"{len(detail['drafts'])} draft(s) ready — **nothing was sent** "
                "(outreach live sends are disabled for UAT). "
                "When you enable sends later, say `send outreach <id>`."
            ),
        }

    # Live path reserved for later — still route through send helper.
    return send_outreach_campaign(campaign_id=campaign_id, approved=True)


def send_outreach_campaign(*, campaign_id: str, approved: bool = False, batch_size: int = 25) -> dict[str, Any]:
    """Send approved drafts in waves. Hard-blocked until LEXI_OUTREACH_LIVE_SENDS_ENABLED."""
    # Ahead of the approval gate: while the feature is parked the answer is no
    # regardless of who approved what, and "campaigns are off" is a truer reply
    # than an approval error.
    if campaigns_paused():
        return _paused_response("sent anything")

    from app.safety.approval_gate import assert_kory_approved_write

    assert_kory_approved_write(approved=approved, action="Outreach campaign send")
    detail = get_campaign(campaign_id)
    if not detail:
        return {"ok": False, "error": f"Unknown campaign {campaign_id}"}

    if outreach_sends_blocked():
        return {
            "ok": True,
            "campaign_id": campaign_id,
            "sent": 0,
            "dry_run": True,
            "sends_blocked": True,
            "kory_message": (
                f"Send blocked for `{campaign_id}` — "
                "set LEXI_OUTREACH_LIVE_SENDS_ENABLED=true and disable dry-run only when you are ready."
            ),
        }

    # Live send: dispatch staged drafts in a wave. Every send still routes through
    # send_outbound_email → execute_tool, so dry-run, Kory-outbound-block, the
    # recipient allowlist, and Kory-space-read-only all still apply per-message.
    from app.integrations.outlook_email import send_outbound_email

    with get_lexi_connection() as conn:
        _ensure_tables(conn)
        rows = conn.execute(
            """
            SELECT draft_id, recipient_email, subject, body
            FROM outreach_drafts
            WHERE campaign_id = ? AND status = 'approved'
            ORDER BY rowid LIMIT ?
            """,
            (campaign_id, int(batch_size)),
        ).fetchall()

    sent = 0
    failed = 0
    errors: list[dict[str, str]] = []
    for row in rows:
        to_email = str(row["recipient_email"])
        try:
            msg_id, _log = send_outbound_email(
                to_email=to_email,
                subject=str(row["subject"]),
                body=str(row["body"]),
                approved_send=True,
                send_channel="kory",
            )
            with get_lexi_connection() as conn:
                conn.execute(
                    "UPDATE outreach_drafts SET status = 'sent', outlook_draft_id = ? "
                    "WHERE draft_id = ?",
                    (msg_id or "", str(row["draft_id"])),
                )
                conn.commit()
            sent += 1
        except Exception as exc:  # one bad recipient shouldn't abort the wave
            failed += 1
            errors.append({"email": to_email, "error": str(exc)[:160]})

    remaining = _staged_draft_count(campaign_id)
    return {
        "ok": failed == 0,
        "campaign_id": campaign_id,
        "sent": sent,
        "failed": failed,
        "remaining_staged": remaining,
        "errors": errors[:10],
        "kory_message": (
            f"Outreach `{campaign_id}`: sent {sent}"
            + (f", {failed} failed" if failed else "")
            + (f", {remaining} still staged (run again for the next wave)." if remaining else ".")
        ),
    }


def _staged_draft_count(campaign_id: str) -> int:
    """Approved-but-unsent drafts remaining for this campaign."""
    with get_lexi_connection() as conn:
        _ensure_tables(conn)
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM outreach_drafts WHERE campaign_id = ? AND status = 'approved'",
            (campaign_id,),
        ).fetchone()
    return int(row["c"] or 0)


def remove_outreach_recipient(*, campaign_id: str, email: str) -> dict[str, Any]:
    if campaigns_paused():
        return _paused_response("changed that campaign's recipients")
    email_l = email.strip().lower()
    with get_lexi_connection() as conn:
        _ensure_tables(conn)
        conn.execute(
            """
            UPDATE outreach_drafts SET status = 'removed'
            WHERE campaign_id = ? AND lower(recipient_email) = ?
            """,
            (campaign_id, email_l),
        )
        conn.commit()
    return {"ok": True, "campaign_id": campaign_id, "removed": email_l}


def _hubspot_audience(*, limit: int, lifecycle: str = "") -> list[dict[str, str]]:
    try:
        from app.integrations.hubspot_manager import find_contacts_for_outreach

        found = find_contacts_for_outreach(lifecycle=lifecycle, limit=limit)
        out: list[dict[str, str]] = []
        for contact in found.get("contacts") or []:
            email = (contact.get("email") or "").strip()
            if not email:
                continue
            out.append(
                {
                    "name": contact.get("name") or email.split("@")[0],
                    "email": email.lower(),
                    "company": str(contact.get("company") or ""),
                    "notes": str(contact.get("lifecyclestage") or ""),
                }
            )
        return out
    except Exception:
        return []


def _light_research_note(contact: dict[str, str]) -> str:
    """Optional low-spend research; failures are silent."""
    try:
        from app.integrations.person_research import research_person

        bundle = research_person(
            contact.get("name") or "",
            company=contact.get("company") or "",
            email=contact.get("email") or "",
            include_inbox=False,
            include_news=False,
        )
        summary = (bundle.get("web_summary") or "").strip()
        return summary[:400]
    except Exception:
        return ""


def _stage_outlook_draft(*, to_email: str, subject: str, body: str) -> str:
    """Create Outlook draft when allowed; otherwise local dry-run id (never sends)."""
    from app.integrations.outlook_email import create_outbound_draft

    draft_id, _ = create_outbound_draft(
        to_email=to_email,
        subject=subject,
        body=body,
        approved=False,  # UAT: never write Outlook drafts unless explicitly enabled later
    )
    return draft_id or f"local-draft-{uuid.uuid4().hex[:10]}"


def _campaign_summary_message(
    *,
    campaign_id: str,
    name: str,
    draft_count: int,
    sample: list[dict[str, Any]],
) -> str:
    lines = [
        f"**Outreach campaign staged:** {name}",
        f"ID: `{campaign_id}`",
        f"Drafts: **{draft_count}** (saved for Outlook drafts — **not sent**)",
        "",
        "Sample:",
    ]
    for row in sample:
        lines.append(f"• {row['name']} <{row['email']}> — {row['subject']}")
    lines.append("")
    lines.append(
        "Review drafts in Outlook when live draft-create is enabled. "
        "For now they're stored in Lexi. When ready later: "
        f"`approve outreach {campaign_id}` then `send outreach {campaign_id}`."
    )
    return "\n".join(lines)
