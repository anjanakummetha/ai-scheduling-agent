"""Asana task creation for Lexi venue / meal booking reminders."""

from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import date, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from app.config import ASANA_BOARD_NAME, ASANA_PARENT_PROJECT_NAME, settings
from app.integrations.composio_client import ComposioNotConfiguredError, execute_asana_tool

logger = logging.getLogger(__name__)
MT = ZoneInfo("America/Denver")

ASANA_CREATE_TOOL = "ASANA_CREATE_A_TASK"
BOOKING_TASK_PREFIX = "Lexi Booking:"
VENUE_TASK_PREFIX = "Needs Reservation:"

MealKind = Literal["lunch", "dinner"]

VENUE_RESERVATION_INTENTS = frozenset({"lunch_request", "dinner_request", "happy_hour"})
INTENT_TO_MEAL: dict[str, MealKind] = {
    "lunch_request": "lunch",
    "dinner_request": "dinner",
}

_KORY_SIGNATURE_RE = re.compile(r"let['']s win,?\s*\n\s*kory\b", re.IGNORECASE)
_MEAL_PATTERNS: dict[MealKind, re.Pattern[str]] = {
    "dinner": re.compile(r"\bdinner\b", re.IGNORECASE),
    "lunch": re.compile(r"\blunch\b", re.IGNORECASE),
}
_RESERVATION_DRAFT_RE = re.compile(
    r"\b("
    r"reservation|reserve a table|book a table|book (?:us |me )?a table|"
    r"make a reservation|hold a table|restaurant|venue"
    r")\b",
    re.IGNORECASE,
)


def detect_kory_meal_mention(
    *,
    subject: str,
    body: str,
    sender: str,
) -> MealKind | None:
    """Return lunch/dinner when Kory authored or signed the email and mentions that meal."""
    text = f"{subject}\n{body}"
    meal: MealKind | None = None
    if _MEAL_PATTERNS["dinner"].search(text):
        meal = "dinner"
    elif _MEAL_PATTERNS["lunch"].search(text):
        meal = "lunch"
    if not meal or not is_kory_author(sender, body):
        return None
    return meal


def is_kory_author(sender: str, body: str) -> bool:
    if _KORY_SIGNATURE_RE.search(body):
        return True
    sender_lower = sender.lower()
    for email in settings.kory_sender_emails:
        if email in sender_lower:
            return True
    local = sender_lower.split("@", 1)[0]
    if "kory" in local:
        return True
    return False


def create_booking_reminder_task(
    *,
    meal: MealKind,
    meeting_subject: str,
    thread_id: str,
    sender: str,
    body_excerpt: str = "",
    approved: bool = False,
) -> dict[str, Any]:
    """Create a task on the Lexi Booking reminders Asana board."""
    from app.safety.approval_gate import assert_kory_approved_write

    assert_kory_approved_write(approved=approved, action="Asana reservation reminder")
    subject = (meeting_subject or "Email thread").strip()
    title = f"{BOOKING_TASK_PREFIX} {meal.title()} — {subject}"
    notes = (
        f"Lexi booking reminder — {ASANA_BOARD_NAME} ({ASANA_PARENT_PROJECT_NAME})\n"
        "----------------------------------------\n"
        f"Meal: {meal}\n"
        f"Thread: {thread_id}\n"
        f"Counterparty / sender: {sender.strip()}\n"
    )
    if body_excerpt.strip():
        notes += f"\nExcerpt:\n{body_excerpt.strip()[:1200]}\n"
    return _create_asana_task(title=title, notes=notes)


def meal_from_intent(intent: str | None) -> MealKind | None:
    return INTENT_TO_MEAL.get((intent or "").strip().lower())  # type: ignore[return-value]


def reservation_needed_for_proposal(
    *,
    intent: str | None,
    drafted_reply: str = "",
    subject: str = "",
    body: str = "",
    sender: str = "",
) -> bool:
    """True when Kory likely needs to book a venue (meal, happy hour, or explicit reservation)."""
    intent_key = (intent or "").strip().lower()
    if intent_key in VENUE_RESERVATION_INTENTS:
        return True
    if detect_kory_meal_mention(subject=subject, body=body, sender=sender):
        return True
    draft = (drafted_reply or "").strip()
    if draft and _RESERVATION_DRAFT_RE.search(draft):
        return True
    if draft and meal_from_draft_text(draft):
        return True
    return False


def meal_from_draft_text(text: str) -> MealKind | None:
    if _MEAL_PATTERNS["lunch"].search(text):
        return "lunch"
    if _MEAL_PATTERNS["dinner"].search(text):
        return "dinner"
    return None


def dispatch_reservation_reminder_for_proposal(
    *,
    intent: str | None,
    meeting_subject: str,
    thread_id: str,
    sender: str,
    drafted_reply: str = "",
    raw_body: str = "",
    time_slot: str = "",
    approved: bool = False,
) -> dict[str, Any] | None:
    """Create the right Asana task on Reservation Reminders, or None if not needed."""
    intent_key = (intent or "").strip().lower()
    if not reservation_needed_for_proposal(
        intent=intent_key,
        drafted_reply=drafted_reply,
        subject=meeting_subject,
        body=raw_body,
        sender=sender,
    ):
        return None

    meal = (
        meal_from_intent(intent_key)
        or detect_kory_meal_mention(subject=meeting_subject, body=raw_body, sender=sender)
        or meal_from_draft_text(drafted_reply)
        or meal_from_draft_text(raw_body)
    )

    if meal:
        excerpt = drafted_reply.strip() or raw_body.strip()
        if time_slot.strip():
            excerpt = f"Confirmed slot: {time_slot.strip()}\n\n{excerpt}".strip()
        return create_booking_reminder_task(
            meal=meal,
            meeting_subject=meeting_subject,
            thread_id=thread_id,
            sender=sender,
            body_excerpt=excerpt,
            approved=approved,
        )

    if intent_key == "happy_hour" or _RESERVATION_DRAFT_RE.search(drafted_reply or ""):
        return create_venue_reservation_task(
            meeting_subject=meeting_subject,
            time_slot=time_slot or "See approved email / calendar",
            participants=sender,
            approved=approved,
        )

    return None


def create_venue_reservation_task(
    meeting_subject: str,
    time_slot: str,
    participants: str,
    *,
    approved: bool = False,
) -> dict[str, Any]:
    """Create an Asana action task after a confirmed calendar slot (venue logistics)."""
    from app.safety.approval_gate import assert_kory_approved_write

    assert_kory_approved_write(approved=approved, action="Asana venue reservation task")
    subject = (meeting_subject or "Meeting").strip()
    title = f"{VENUE_TASK_PREFIX} {subject}"
    notes = (
        "Lexi venue reservation request\n"
        "------------------------------\n"
        f"Board: {ASANA_BOARD_NAME}\n"
        f"Selected time slot: {time_slot.strip()}\n"
        f"Target participant(s): {participants.strip()}\n"
    )
    return _create_asana_task(title=title, notes=notes)


def _create_asana_task(*, title: str, notes: str) -> dict[str, Any]:
    if _should_simulate_asana():
        task_id = f"asana-sim-{uuid.uuid4().hex[:12]}"
        return {
            "ok": True,
            "task_id": task_id,
            "title": title,
            "notes": notes,
            "board": ASANA_BOARD_NAME,
            "simulated": True,
            "composio_log_id": None,
            "error": None,
        }

    if not settings.asana_project_gid:
        return {
            "ok": False,
            "task_id": None,
            "title": title,
            "notes": notes,
            "board": ASANA_BOARD_NAME,
            "simulated": False,
            "composio_log_id": None,
            "error": (
                f"ASANA_PROJECT_GID is not set — add the GID for board '{ASANA_BOARD_NAME}' in .env"
            ),
        }

    try:
        task_id, log_id = _create_task_via_composio(title=title, notes=notes)
        return {
            "ok": bool(task_id),
            "task_id": task_id,
            "title": title,
            "notes": notes,
            "board": ASANA_BOARD_NAME,
            "simulated": False,
            "composio_log_id": log_id,
            "error": None if task_id else "Composio returned no task id.",
        }
    except ComposioNotConfiguredError as exc:
        return {
            "ok": False,
            "task_id": None,
            "title": title,
            "notes": notes,
            "board": ASANA_BOARD_NAME,
            "simulated": False,
            "composio_log_id": None,
            "error": str(exc),
        }
    except Exception as exc:
        friendly = _friendly_asana_error(exc)
        if friendly:
            return {
                "ok": False,
                "task_id": None,
                "title": title,
                "notes": notes,
                "board": ASANA_BOARD_NAME,
                "simulated": False,
                "composio_log_id": None,
                "error": friendly,
            }
        if settings.demo_mode:
            task_id = f"asana-sim-{uuid.uuid4().hex[:12]}"
            return {
                "ok": True,
                "task_id": task_id,
                "title": title,
                "notes": notes,
                "board": ASANA_BOARD_NAME,
                "simulated": True,
                "composio_log_id": None,
                "error": f"composio_failed_simulated: {exc}",
            }
        return {
            "ok": False,
            "task_id": None,
            "title": title,
            "notes": notes,
            "board": ASANA_BOARD_NAME,
            "simulated": False,
            "composio_log_id": None,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _friendly_asana_error(exc: Exception) -> str | None:
    """Human-readable message when Asana board/section is missing — never crash the agent."""
    msg = str(exc).lower()
    if any(token in msg for token in ("404", "not found", "does not exist", "no longer accessible")):
        return (
            f"Asana board '{ASANA_BOARD_NAME}' was not found (it may have been deleted or renamed). "
            "Scheduling and email are unaffected. Update ASANA_PROJECT_GID in .env or recreate the board."
        )
    if "403" in msg or "forbidden" in msg:
        return (
            f"Asana access denied for board '{ASANA_BOARD_NAME}'. "
            "Reconnect Asana in Composio or check project permissions."
        )
    return None


def _should_simulate_asana() -> bool:
    if not settings.asana_enabled:
        return True
    if os.getenv("ASANA_SIMULATE", "").lower() in {"1", "true", "yes"}:
        return True
    if settings.demo_mode and not settings.composio_api_key:
        return True
    if not settings.asana_project_gid:
        return True
    return False


def _create_task_via_composio(*, title: str, notes: str) -> tuple[str | None, str | None]:
    project_gid = settings.asana_project_gid
    if not project_gid:
        raise RuntimeError("ASANA_PROJECT_GID is not configured.")

    arguments: dict[str, Any] = {
        "data": {
            "name": title,
            "notes": notes,
            "projects": [project_gid],
        }
    }
    result = execute_asana_tool(ASANA_CREATE_TOOL, arguments)
    if result.get("dry_run"):
        return f"asana-dry-run-{uuid.uuid4().hex[:12]}", result.get("log_id")
    task_id = _extract_task_id(result.get("data"))
    if not task_id:
        raise RuntimeError("Composio Asana create returned no task id.")
    _add_task_to_section_if_configured(task_id)
    return task_id, result.get("log_id")


def _add_task_to_section_if_configured(task_gid: str) -> None:
    section_gid = settings.asana_section_gid
    if not section_gid:
        return
    try:
        execute_asana_tool(
            "ASANA_ADD_TASK_TO_SECTION",
            {
                "task_gid": task_gid,
                "section_gid": section_gid,
            },
        )
    except Exception:
        pass


def _extract_task_id(data: Any) -> str | None:
    if isinstance(data, dict):
        for key in ("gid", "id", "task_id", "task_gid"):
            value = data.get(key)
            if value:
                return str(value)
        task = data.get("task") or data.get("data")
        if isinstance(task, dict):
            return _extract_task_id(task)
    return None


TaskBucket = Literal["overdue", "due_today", "upcoming", "all"]


def create_asana_task_from_chat(
    *,
    title: str,
    notes: str = "",
    due_on: str = "",
    approved: bool = False,
) -> dict[str, Any]:
    from app.safety.approval_gate import assert_kory_approved_write

    assert_kory_approved_write(approved=approved, action="Asana create task")
    result = _create_asana_task(title=title, notes=notes or title)
    if due_on.strip() and result.get("ok") and result.get("task_id"):
        update = update_asana_task(
            task_gid=str(result["task_id"]),
            due_on=due_on.strip(),
            approved=approved,
        )
        result["due_update"] = update
    return result


def complete_asana_task(*, task_gid: str, approved: bool = False) -> dict[str, Any]:
    from app.safety.approval_gate import assert_kory_approved_write

    assert_kory_approved_write(approved=approved, action="Asana complete task")
    if _should_simulate_asana():
        return {"ok": True, "task_gid": task_gid, "simulated": True, "dry_run": True}
    result = execute_asana_tool(
        "ASANA_UPDATE_A_TASK",
        {"task_gid": task_gid, "data": {"completed": True}},
    )
    return {
        "ok": True,
        "task_gid": task_gid,
        "dry_run": bool(result.get("dry_run")),
        "blocked_reason": (result.get("data") or {}).get("blocked_reason")
        if isinstance(result.get("data"), dict)
        else None,
    }


def normalize_due_on(value: str) -> str:
    """Keep a due date from landing in the past.

    Due dates arrive as whatever the model resolved "August 3" to, and it
    resolved it to the previous year — the task was saved due 2025-08-03, a
    date that had already passed. A bare month/day means the next one.
    """
    raw = (value or "").strip()
    if not raw:
        return ""
    try:
        parsed = date.fromisoformat(raw[:10])
    except ValueError:
        return raw  # unrecognised format: let Asana reject it rather than mangle it
    today = datetime.now(tz=MT).date()
    if parsed >= today:
        return parsed.isoformat()
    for bump in (1, 2):
        try:
            shifted = parsed.replace(year=parsed.year + bump)
        except ValueError:  # Feb 29
            shifted = parsed.replace(year=parsed.year + bump, day=28)
        if shifted >= today:
            logger.info("Due date %s is past; using %s.", raw, shifted.isoformat())
            return shifted.isoformat()
    return parsed.isoformat()


def update_asana_task(
    *,
    task_gid: str,
    title: str = "",
    notes: str = "",
    due_on: str = "",
    approved: bool = False,
) -> dict[str, Any]:
    """Update title/notes/due date — blocked unless live writes enabled."""
    from app.safety.approval_gate import assert_kory_approved_write

    assert_kory_approved_write(approved=approved, action="Asana update task")
    data: dict[str, Any] = {}
    if title.strip():
        data["name"] = title.strip()
    if notes.strip():
        data["notes"] = notes.strip()
    if due_on.strip():
        data["due_on"] = normalize_due_on(due_on)
    if not data:
        return {"ok": False, "error": "No fields to update."}
    if _should_simulate_asana():
        return {"ok": True, "task_gid": task_gid, "simulated": True, "dry_run": True, "data": data}
    result = execute_asana_tool(
        "ASANA_UPDATE_A_TASK",
        {"task_gid": task_gid, "data": data},
    )
    return {
        "ok": True,
        "task_gid": task_gid,
        "dry_run": bool(result.get("dry_run")),
        "updated": data,
    }


def delete_asana_task(*, task_gid: str, approved: bool = False) -> dict[str, Any]:
    from app.safety.approval_gate import assert_kory_approved_write

    assert_kory_approved_write(approved=approved, action="Asana delete task")
    if _should_simulate_asana():
        return {"ok": True, "task_gid": task_gid, "simulated": True, "dry_run": True}
    result = execute_asana_tool("ASANA_DELETE_TASK", {"task_gid": task_gid})
    return {"ok": True, "task_gid": task_gid, "dry_run": bool(result.get("dry_run"))}


def search_asana_tasks(*, query: str, limit: int = 15) -> dict[str, Any]:
    """Search tasks by name across configured / related projects (read-only)."""
    needle = query.strip().lower()
    if not needle:
        return {"ok": False, "tasks": [], "error": "query is required"}
    projects = list_asana_project_options()
    matches: list[dict[str, Any]] = []
    for project in projects.get("projects", []):
        listed = list_asana_tasks(
            bucket="all",
            limit=50,
            project_gid=str(project.get("gid") or ""),
            project_name=str(project.get("name") or ""),
        )
        for task in listed.get("tasks") or []:
            name = str(task.get("name") or "").lower()
            if needle in name:
                matches.append({**task, "project": project.get("name")})
    return {"ok": True, "query": query, "tasks": matches[:limit]}


def move_asana_task_to_section(
    *,
    task_gid: str,
    section_gid: str = "",
    section_name: str = "",
    approved: bool = False,
) -> dict[str, Any]:
    from app.safety.approval_gate import assert_kory_approved_write

    assert_kory_approved_write(approved=approved, action="Asana move task section")
    target = section_gid.strip() or settings.asana_section_gid or ""
    if not target and section_name.strip():
        return {
            "ok": False,
            "error": (
                f"Section '{section_name}' needs a configured GID "
                "(set ASANA_SECTION_GID or pass section_gid)."
            ),
            "dry_run": True,
        }
    if not target:
        return {"ok": False, "error": "section_gid is required", "dry_run": True}
    if _should_simulate_asana():
        return {
            "ok": True,
            "task_gid": task_gid,
            "section_gid": target,
            "simulated": True,
            "dry_run": True,
        }
    result = execute_asana_tool(
        "ASANA_ADD_TASK_TO_SECTION",
        {"task_gid": task_gid, "section_gid": target},
    )
    return {
        "ok": True,
        "task_gid": task_gid,
        "section_gid": target,
        "dry_run": bool(result.get("dry_run")),
    }


def comment_on_asana_task(
    *,
    task_gid: str,
    comment: str,
    approved: bool = False,
) -> dict[str, Any]:
    from app.safety.approval_gate import assert_kory_approved_write

    assert_kory_approved_write(approved=approved, action="Asana comment")
    text = comment.strip()
    if not text:
        return {"ok": False, "error": "comment is required"}
    if _should_simulate_asana():
        return {
            "ok": True,
            "task_gid": task_gid,
            "comment": text,
            "simulated": True,
            "dry_run": True,
        }
    result = execute_asana_tool(
        "ASANA_CREATE_TASK_COMMENT",
        {"task_gid": task_gid, "text": text},
    )
    return {
        "ok": True,
        "task_gid": task_gid,
        "comment": text,
        "dry_run": bool(result.get("dry_run")),
    }


def list_asana_project_options() -> dict[str, Any]:
    """Configured NON-IFG plus optional related project GIDs (Call List, etc.)."""
    import os

    projects: list[dict[str, str]] = []
    if settings.asana_project_gid:
        projects.append(
            {
                "gid": settings.asana_project_gid,
                "name": ASANA_PARENT_PROJECT_NAME,
            }
        )
    extra = os.getenv("ASANA_RELATED_PROJECT_GIDS", "").strip()
    # Format: name:gid,name:gid
    for part in extra.split(",") if extra else []:
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            name, gid = part.split(":", 1)
            projects.append({"gid": gid.strip(), "name": name.strip() or gid.strip()})
        else:
            projects.append({"gid": part, "name": part})
    # Deduplicate by gid
    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for row in projects:
        gid = row["gid"]
        if gid in seen:
            continue
        seen.add(gid)
        unique.append(row)
    return {"ok": True, "projects": unique}


def list_asana_tasks(
    *,
    bucket: TaskBucket = "all",
    limit: int = 25,
    project_gid: str = "",
    project_name: str = "",
) -> dict[str, Any]:
    """List tasks from Kory NON-IFG (or related) project — read-only."""
    if _should_simulate_asana() and not project_gid:
        return _simulated_asana_tasks(bucket=bucket)

    target_gid = project_gid.strip() or settings.asana_project_gid
    if not target_gid:
        return {"ok": False, "tasks": [], "error": "ASANA_PROJECT_GID not set"}

    try:
        result = execute_asana_tool(
            "ASANA_GET_TASKS_FROM_A_PROJECT",
            {"project_gid": target_gid, "limit": max(1, min(limit, 100))},
        )
    except Exception:
        try:
            result = execute_asana_tool(
                "ASANA_GET_MULTIPLE_TASKS",
                {"project": target_gid, "limit": max(1, min(limit, 100))},
            )
        except Exception as exc:
            return {"ok": False, "tasks": [], "error": str(exc)}

    tasks = _normalize_asana_tasks(result.get("data"))
    for task in tasks:
        if project_name:
            task["project"] = project_name
    filtered = _filter_tasks_by_bucket(tasks, bucket=bucket)
    return {
        "ok": True,
        "bucket": bucket,
        "project_gid": target_gid,
        "project_name": project_name or ASANA_PARENT_PROJECT_NAME,
        "tasks": filtered[:limit],
        "composio_log_id": result.get("log_id"),
        "dry_run": result.get("dry_run", False),
    }


def summarize_asana_for_briefing() -> str:
    overdue = list_asana_tasks(bucket="overdue", limit=5)
    today = list_asana_tasks(bucket="due_today", limit=5)
    lines = ["**Asana:**"]
    if overdue.get("tasks"):
        lines.append(f"Overdue ({len(overdue['tasks'])}):")
        for t in overdue["tasks"][:5]:
            lines.append(f"• {t.get('name')}")
    else:
        lines.append("Overdue: none in sample")
    if today.get("tasks"):
        lines.append(f"Due today ({len(today['tasks'])}):")
        for t in today["tasks"][:5]:
            lines.append(f"• {t.get('name')}")
    else:
        lines.append("Due today: none in sample")
    return "\n".join(lines)


def _normalize_asana_tasks(data: Any) -> list[dict[str, Any]]:
    rows: list[Any] = []
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        for key in ("data", "tasks", "value"):
            nested = data.get(key)
            if isinstance(nested, list):
                rows = nested
                break
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        out.append(
            {
                "gid": row.get("gid") or row.get("id"),
                "name": row.get("name") or row.get("title"),
                "due_on": row.get("due_on") or row.get("due_at"),
                "completed": bool(row.get("completed")),
                "notes": (row.get("notes") or "")[:300] or None,
            }
        )
    return out


def _filter_tasks_by_bucket(tasks: list[dict[str, Any]], *, bucket: TaskBucket) -> list[dict[str, Any]]:
    from datetime import date

    today = date.today().isoformat()
    active = [t for t in tasks if not t.get("completed")]
    if bucket == "all":
        return active
    if bucket == "due_today":
        return [t for t in active if (t.get("due_on") or "")[:10] == today]
    if bucket == "overdue":
        return [t for t in active if (t.get("due_on") or "")[:10] < today and t.get("due_on")]
    if bucket == "upcoming":
        return [t for t in active if (t.get("due_on") or "")[:10] > today]
    return active


def _simulated_asana_tasks(*, bucket: TaskBucket) -> dict[str, Any]:
    samples = [
        {
            "gid": "sim-1",
            "name": "Follow up investor intro",
            "due_on": "2026-07-15",
            "completed": False,
            "project": ASANA_PARENT_PROJECT_NAME,
        },
        {
            "gid": "sim-2",
            "name": "Book dinner reservation",
            "due_on": "2026-07-16",
            "completed": False,
            "project": ASANA_PARENT_PROJECT_NAME,
        },
        {
            "gid": "sim-3",
            "name": "Call List — reconnect Jane",
            "due_on": "2026-07-20",
            "completed": False,
            "project": "Call List",
        },
    ]
    return {
        "ok": True,
        "bucket": bucket,
        "tasks": _filter_tasks_by_bucket(samples, bucket=bucket),
        "simulated": True,
    }
