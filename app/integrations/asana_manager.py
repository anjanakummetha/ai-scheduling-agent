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

import rules as kory_rules

logger = logging.getLogger(__name__)
MT = ZoneInfo("America/Denver")

ASANA_CREATE_TOOL = "ASANA_CREATE_A_TASK"
BOOKING_TASK_PREFIX = "Lexi Booking:"
VENUE_TASK_PREFIX = "Needs Reservation:"

MealKind = Literal["lunch", "dinner"]

VENUE_RESERVATION_INTENTS = frozenset(
    {"lunch_request", "dinner_request", "happy_hour", "dinner", "lunch"}
)
INTENT_TO_MEAL: dict[str, MealKind] = {
    "lunch_request": "lunch",
    "lunch": "lunch",
    "dinner_request": "dinner",
    "dinner": "dinner",
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
    booth_note = str(
        kory_rules.MEETING_TYPES.get("happy_hour", {}).get("reservation_note")
        or "Request a bar booth."
    )
    notes = (
        "Lexi venue reservation request\n"
        "------------------------------\n"
        f"Board: {ASANA_BOARD_NAME}\n"
        f"Selected time slot: {time_slot.strip()}\n"
        f"Target participant(s): {participants.strip()}\n"
        f"Kory's standing ask: {booth_note}\n"
    )
    return _create_asana_task(title=title, notes=notes)


def _create_asana_task(*, title: str, notes: str, section: str = "") -> dict[str, Any]:
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
        task_id, log_id, placed_gid = _create_task_via_composio(
            title=title, notes=notes, section=section
        )
        return {
            "ok": bool(task_id),
            "task_id": task_id,
            "title": title,
            "notes": notes,
            # The board the task actually landed on. This used to report a
            # constant, so a task filed under Personal was announced as
            # Reservation Reminders.
            "board": _section_name_for_gid(placed_gid) or "(unfiled)",
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


def _create_task_via_composio(
    *, title: str, notes: str, section: str = ""
) -> tuple[str | None, str | None, str]:
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
        return f"asana-dry-run-{uuid.uuid4().hex[:12]}", result.get("log_id"), ""
    task_id = _extract_task_id(result.get("data"))
    if not task_id:
        raise RuntimeError("Composio Asana create returned no task id.")
    placed_gid = _add_task_to_section_if_configured(task_id, section)
    return task_id, result.get("log_id"), placed_gid


_SECTION_CACHE: dict[str, list[dict[str, str]]] = {}
_PROJECT_CACHE: list[dict[str, str]] = []


def list_project_sections(project_gid: str = "") -> list[dict[str, str]]:
    """Sections (boards) on a project — General, Personal, YPO, and the rest."""
    pid = (project_gid or settings.asana_project_gid or "").strip()
    if not pid:
        return []
    if pid in _SECTION_CACHE:
        return _SECTION_CACHE[pid]
    try:
        result = execute_asana_tool(
            "ASANA_GET_SECTIONS_IN_PROJECT",
            {"project_gid": pid, "limit": 100, "opt_fields": ["name"]},
        )
    except Exception:
        logger.debug("Could not list sections for project %s.", pid, exc_info=True)
        return []
    data = result.get("data")
    rows = data.get("data") if isinstance(data, dict) else data
    sections = [
        {"gid": str(r.get("gid") or ""), "name": str(r.get("name") or "")}
        for r in (rows or [])
        if isinstance(r, dict) and r.get("gid")
    ]
    _SECTION_CACHE[pid] = sections
    return sections


def resolve_section_gid(section: str, project_gid: str = "") -> str:
    """Map a board name Kory typed ("YPO", "personal") to its section gid."""
    raw = (section or "").strip()
    if not raw:
        return ""
    if raw.isdigit():
        return raw
    wanted = raw.casefold()
    sections = list_project_sections(project_gid)
    for row in sections:  # exact name first
        if row["name"].strip().casefold() == wanted:
            return row["gid"]
    for row in sections:  # then a containing match ("ypo" -> "YPO")
        name = row["name"].strip().casefold()
        if wanted in name or name in wanted:
            return row["gid"]
    return ""


def resolve_task_gid(task: str) -> str:
    """Accept a task gid or a task name.

    Asana needs a numeric id, but in chat the model only has the name it just
    used — passing that through produced "task: Not a Long: <name>", which then
    got relayed to Kory as commenting being unsupported.
    """
    raw = (task or "").strip()
    if not raw or raw.isdigit():
        return raw
    try:
        found = search_asana_tasks(query=raw, limit=5)
    except Exception:
        logger.debug("Task lookup failed for %r.", raw, exc_info=True)
        return raw
    tasks = found.get("tasks") or []
    wanted = raw.casefold()
    for row in tasks:  # prefer an exact name match
        if str(row.get("name") or "").strip().casefold() == wanted:
            return str(row.get("gid") or raw)
    if len(tasks) == 1:
        return str(tasks[0].get("gid") or raw)
    return raw


def _section_name_for_gid(section_gid: str) -> str:
    if not section_gid:
        return ""
    for row in list_project_sections():
        if row["gid"] == section_gid:
            return row["name"]
    return ""


def resolve_task_or_error(
    task: str, *, owner_ack: bool = False
) -> tuple[str, dict[str, Any] | None]:
    """Resolve a task name to a gid, or explain why it could not be resolved.

    An unresolved name used to reach Asana verbatim and come back as
    "task: Not a Long: <name>", which was then relayed to Kory as the feature
    being unsupported. Better to say which task is meant.
    """
    raw = (task or "").strip()
    if _should_simulate_asana():
        return raw, None  # nothing live to look a name up against
    try:
        candidates = (
            (search_asana_tasks(query=raw, limit=6, mine_only=False).get("tasks") or [])
            if raw
            else []
        )
    except Exception:
        candidates = []
    exact = [c for c in candidates if str(c.get("name") or "").strip().casefold() == raw.casefold()]
    match = exact[0] if exact else (candidates[0] if len(candidates) == 1 else None)
    if match:
        owner = str(match.get("assignee") or "").strip()
        if owner and "kory" not in owner.lower() and not owner_ack:
            # Kory may legitimately close a team task, but never silently.
            return "", {
                "ok": False,
                "error_code": "owner_confirmation_required",
                "error": (
                    f"{match.get('name')!r} is assigned to {owner}"
                    f"{' in ' + str(match.get('project')) if match.get('project') else ''}. "
                    "Confirm you want to change someone else's task."
                ),
                "assignee": owner,
                "task_gid": match.get("gid"),
            }
        return str(match.get("gid") or ""), None
    if raw.isdigit():
        return raw, None
    if not candidates:
        return "", {
            "ok": False,
            "error": f"No task found matching {raw!r}.",
            "candidates": [],
        }
    return "", {
        "ok": False,
        "error": (
            f"{raw!r} matches {len(candidates)} tasks — which one? "
            + "; ".join(str(c.get("name")) for c in candidates[:5])
        ),
        "candidates": [
            {"gid": c.get("gid"), "name": c.get("name"), "project": c.get("project")}
            for c in candidates[:5]
        ],
    }


def _add_task_to_section_if_configured(task_gid: str, section: str = "") -> str:
    section_gid = resolve_section_gid(section) if section.strip() else settings.asana_section_gid
    if not section_gid:
        return ""
    try:
        execute_asana_tool(
            "ASANA_ADD_TASK_TO_SECTION",
            {
                "task_gid": task_gid,
                "section_gid": section_gid,
            },
        )
        return section_gid
    except Exception:
        logger.debug("Could not file task %s under section %s.", task_gid, section_gid)
        return ""


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


TaskBucket = Literal["overdue", "due_today", "upcoming", "all", "completed"]

TASK_FIELDS = ["name", "completed", "due_on", "notes", "assignee", "assignee.name"]


def _list_tasks_across_projects(
    *, bucket: TaskBucket, limit: int, mine_only: bool = True
) -> dict[str, Any]:
    """Aggregate one bucket over every project Lexi can read."""
    collected: list[dict[str, Any]] = []
    for project in list_asana_project_options().get("projects", []):
        listed = list_asana_tasks(
            bucket=bucket,
            limit=100,
            project_gid=str(project.get("gid") or ""),
            project_name=str(project.get("name") or ""),
            mine_only=mine_only,
        )
        collected.extend(listed.get("tasks") or [])
    collected.sort(key=lambda t: str(t.get("due_on") or "9999-12-31"))
    return {
        "ok": True,
        "bucket": bucket,
        "project_name": "all projects",
        "mine_only": mine_only,
        "tasks": collected[:limit],
        "total_found": len(collected),
    }


def create_asana_task_from_chat(
    *,
    title: str,
    notes: str = "",
    due_on: str = "",
    section: str = "",
    approved: bool = False,
) -> dict[str, Any]:
    from app.safety.approval_gate import assert_kory_approved_write

    assert_kory_approved_write(approved=approved, action="Asana create task")
    result = _create_asana_task(title=title, notes=notes or title, section=section)
    if due_on.strip() and result.get("ok") and result.get("task_id"):
        update = update_asana_task(
            task_gid=str(result["task_id"]),
            due_on=due_on.strip(),
            approved=approved,
        )
        result["due_update"] = update
    return result


def complete_asana_task(*, task_gid: str, approved: bool = False, owner_ack: bool = False) -> dict[str, Any]:
    from app.safety.approval_gate import assert_kory_approved_write

    assert_kory_approved_write(approved=approved, action="Asana complete task")
    task_gid, _resolve_error = resolve_task_or_error(task_gid, owner_ack=owner_ack)
    if _resolve_error:
        return _resolve_error
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


# Anything staler than this is treated as a wrong-year resolution, not a
# deliberate past date.
_DUE_DATE_GRACE_DAYS = 30


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

    # Only a clearly stale date is a wrong-year artifact. A date that is barely
    # past is far more likely to be today read from a different timezone, or a
    # deliberate "due yesterday" — and silently moving either a full year out is
    # much worse than leaving it alone.
    if (today - parsed).days <= _DUE_DATE_GRACE_DAYS:
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
    owner_ack: bool = False,
) -> dict[str, Any]:
    """Update title/notes/due date — blocked unless live writes enabled."""
    from app.safety.approval_gate import assert_kory_approved_write

    assert_kory_approved_write(approved=approved, action="Asana update task")
    task_gid, _resolve_error = resolve_task_or_error(task_gid, owner_ack=owner_ack)
    if _resolve_error:
        return _resolve_error
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


def delete_asana_task(*, task_gid: str, approved: bool = False, owner_ack: bool = False) -> dict[str, Any]:
    from app.safety.approval_gate import assert_kory_approved_write

    assert_kory_approved_write(approved=approved, action="Asana delete task")
    task_gid, _resolve_error = resolve_task_or_error(task_gid, owner_ack=owner_ack)
    if _resolve_error:
        return _resolve_error
    if _should_simulate_asana():
        return {"ok": True, "task_gid": task_gid, "simulated": True, "dry_run": True}
    result = execute_asana_tool("ASANA_DELETE_TASK", {"task_gid": task_gid})
    return {"ok": True, "task_gid": task_gid, "dry_run": bool(result.get("dry_run"))}


def search_asana_tasks(
    *, query: str, limit: int = 15, mine_only: bool = True
) -> dict[str, Any]:
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
            mine_only=mine_only,
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
    owner_ack: bool = False,
) -> dict[str, Any]:
    from app.safety.approval_gate import assert_kory_approved_write

    assert_kory_approved_write(approved=approved, action="Asana move task section")
    task_gid, _resolve_error = resolve_task_or_error(task_gid, owner_ack=owner_ack)
    if _resolve_error:
        return _resolve_error
    target = section_gid.strip()
    if not target and section_name.strip():
        target = resolve_section_gid(section_name)
        if not target:
            names = ", ".join(r["name"] for r in list_project_sections()) or "none found"
            return {
                "ok": False,
                "error": f"No board named '{section_name}'. Available boards: {names}.",
            }
    if not target:
        target = settings.asana_section_gid or ""
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
    owner_ack: bool = False,
) -> dict[str, Any]:
    from app.safety.approval_gate import assert_kory_approved_write

    assert_kory_approved_write(approved=approved, action="Asana comment")
    task_gid, _resolve_error = resolve_task_or_error(task_gid, owner_ack=owner_ack)
    if _resolve_error:
        return _resolve_error
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
        # This tool takes task_id, unlike the update/delete tools which take task_gid.
        {"task_id": task_gid, "text": text},
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
    # Reads should cover everything Kory has, including projects added after
    # this was configured — the env list only ever held one project, so IFG
    # Tasks, Marketing Content and the rest were invisible to search.
    if _discover_projects_enabled():
        projects.extend(_discover_workspace_projects())

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


def _discover_projects_enabled() -> bool:
    import os

    return os.getenv("ASANA_DISCOVER_PROJECTS", "true").lower() in {"1", "true", "yes"}


def _discover_workspace_projects() -> list[dict[str, str]]:
    """Every non-archived project in Kory's workspace (read scope only)."""
    global _PROJECT_CACHE
    if _PROJECT_CACHE:
        return _PROJECT_CACHE
    try:
        workspaces = execute_asana_tool("ASANA_GET_MULTIPLE_WORKSPACES", {"limit": 5})
        wdata = workspaces.get("data")
        wrows = wdata.get("data") if isinstance(wdata, dict) else wdata
        workspace_gid = str((wrows or [{}])[0].get("gid") or "")
        if not workspace_gid:
            return []
        result = execute_asana_tool(
            "ASANA_GET_MULTIPLE_PROJECTS",
            {"workspace": workspace_gid, "limit": 100, "opt_fields": ["name", "archived"]},
        )
    except Exception:
        logger.debug("Asana project discovery failed; using configured projects.", exc_info=True)
        return []
    data = result.get("data")
    rows = data.get("data") if isinstance(data, dict) else data
    found = [
        {"gid": str(r.get("gid")), "name": str(r.get("name") or r.get("gid"))}
        for r in (rows or [])
        if isinstance(r, dict) and r.get("gid") and not r.get("archived")
    ]
    _PROJECT_CACHE = found
    return found


def list_asana_tasks(
    *,
    bucket: TaskBucket = "all",
    limit: int = 25,
    project_gid: str = "",
    project_name: str = "",
    mine_only: bool = True,
) -> dict[str, Any]:
    """List tasks from Kory NON-IFG (or related) project — read-only."""
    if _should_simulate_asana() and not project_gid:
        return _simulated_asana_tasks(bucket=bucket)

    # "What's overdue?" means across everything Kory has, not one project.
    if not project_gid.strip():
        return _list_tasks_across_projects(bucket=bucket, limit=limit, mine_only=mine_only)

    target_gid = project_gid.strip() or settings.asana_project_gid
    if not target_gid:
        return {"ok": False, "tasks": [], "error": "ASANA_PROJECT_GID not set"}

    try:
        result = execute_asana_tool(
            "ASANA_GET_TASKS_FROM_A_PROJECT",
            {
                "project_gid": target_gid,
                "limit": max(1, min(limit, 100)),
                # Without opt_fields Asana returns only gid and name, so due_on
                # and completed came back empty and every date bucket was empty:
                # 29 overdue tasks reported as "you're all clear".
                "opt_fields": TASK_FIELDS,
            },
        )
    except Exception:
        try:
            result = execute_asana_tool(
                "ASANA_GET_MULTIPLE_TASKS",
                {
                    "project": target_gid,
                    "limit": max(1, min(limit, 100)),
                    "opt_fields": TASK_FIELDS,
                },
            )
        except Exception as exc:
            return {"ok": False, "tasks": [], "error": str(exc)}

    tasks = _normalize_asana_tasks(result.get("data"))
    for task in tasks:
        if project_name:
            task["project"] = project_name
    if mine_only:
        tasks = [t for t in tasks if is_korys_task(t, project_gid=target_gid)]
    filtered = _filter_tasks_by_bucket(tasks, bucket=bucket)
    return {
        "ok": True,
        "bucket": bucket,
        "mine_only": mine_only,
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
                "assignee": ((row.get("assignee") or {}) or {}).get("name")
                if isinstance(row.get("assignee"), dict)
                else row.get("assignee"),
                "notes": (row.get("notes") or "")[:300] or None,
            }
        )
    return out


def is_korys_task(task: dict[str, Any], *, project_gid: str = "") -> bool:
    """Whether a task is Kory's to act on.

    Shared boards carry other people's work — of 29 overdue tasks, 18 belonged
    to Jason, Anju or Heidi. Reporting those as Kory's overdue list is worse
    than useless. Everything on his own personal project counts as his.
    """
    if project_gid and project_gid == (settings.asana_project_gid or ""):
        return True
    owner = str(task.get("assignee") or "").strip().lower()
    if not owner:
        return False
    return "kory" in owner


def _filter_tasks_by_bucket(tasks: list[dict[str, Any]], *, bucket: TaskBucket) -> list[dict[str, Any]]:
    from datetime import date

    today = date.today().isoformat()
    if bucket == "completed":
        # Asana hides these from active views; Kory still needs to see them.
        done = [t for t in tasks if t.get("completed")]
        done.sort(key=lambda t: str(t.get("due_on") or ""), reverse=True)
        return done
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
