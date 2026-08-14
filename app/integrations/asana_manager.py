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


def _create_asana_task(
    *,
    title: str,
    notes: str,
    section: str = "",
    project_gid: str = "",
    project_name: str = "",
) -> dict[str, Any]:
    if _should_simulate_asana():
        task_id = f"asana-sim-{uuid.uuid4().hex[:12]}"
        return {
            "ok": True,
            "task_id": task_id,
            "title": title,
            "notes": notes,
            "board": ASANA_BOARD_NAME,
            "project": project_name or ASANA_PARENT_PROJECT_NAME,
            "simulated": True,
            "composio_log_id": None,
            "error": None,
        }

    if not (project_gid or settings.asana_project_gid):
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
            title=title, notes=notes, section=section, project_gid=project_gid
        )
        return {
            "ok": bool(task_id),
            "task_id": task_id,
            "title": title,
            "notes": notes,
            # The board the task actually landed on. This used to report a
            # constant, so a task filed under Personal was announced as
            # Reservation Reminders.
            "board": _section_name_for_gid(placed_gid, project_gid) or "(unfiled)",
            "project": project_name or ASANA_PARENT_PROJECT_NAME,
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
    *, title: str, notes: str, section: str = "", project_gid: str = ""
) -> tuple[str | None, str | None, str]:
    target_gid = (project_gid or settings.asana_project_gid or "").strip()
    if not target_gid:
        raise RuntimeError("ASANA_PROJECT_GID is not configured.")

    arguments: dict[str, Any] = {
        "data": {
            "name": title,
            "notes": notes,
            "projects": [target_gid],
        }
    }
    result = execute_asana_tool(ASANA_CREATE_TOOL, arguments)
    if result.get("dry_run"):
        return f"asana-dry-run-{uuid.uuid4().hex[:12]}", result.get("log_id"), ""
    task_id = _extract_task_id(result.get("data"))
    if not task_id:
        raise RuntimeError("Composio Asana create returned no task id.")
    placed_gid = _add_task_to_section_if_configured(task_id, section, project_gid=target_gid)
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


def _section_name_for_gid(section_gid: str, project_gid: str = "") -> str:
    if not section_gid:
        return ""
    for row in list_project_sections(project_gid):
        if row["gid"] == section_gid:
            return row["name"]
    return ""


def resolve_project_gid(project: str) -> tuple[str, str]:
    """Map a project Kory named ("IFG Tasks", a gid) to (gid, name).

    Returns ("", "") when nothing matches or the name is ambiguous — the
    caller must refuse rather than guess: a task filed on the wrong project
    is worse than an error.
    """
    raw = (project or "").strip()
    if not raw:
        return "", ""
    projects = list_asana_project_options().get("projects", [])
    if raw.isdigit():
        for row in projects:
            if row["gid"] == raw:
                return raw, row["name"]
        return raw, ""
    wanted = raw.casefold()
    for row in projects:
        if row["name"].strip().casefold() == wanted:
            return row["gid"], row["name"]
    hits = [row for row in projects if wanted in row["name"].strip().casefold()]
    if len(hits) == 1:
        return hits[0]["gid"], hits[0]["name"]
    return "", ""


def resolve_task_or_error(
    task: str, *, owner_ack: bool = False
) -> tuple[str, dict[str, Any] | None]:
    """Resolve a task name to a gid, or explain why it could not be resolved.

    An unresolved name used to reach Asana verbatim and come back as
    "task: Not a Long: <name>", which was then relayed to Kory as the feature
    being unsupported. Better to say which task is meant.
    """
    from app.integrations.asana_task_match import pick_task

    raw = (task or "").strip()
    if _should_simulate_asana():
        return raw, None  # nothing live to look a name up against
    if raw.isdigit():
        return raw, None
    try:
        candidates = (
            (search_asana_tasks(query=raw, limit=6, mine_only=False).get("tasks") or [])
            if raw
            else []
        )
    except Exception:
        candidates = []
    # Ranked pick, not "first hit wins". The old rule took candidates[0] only when
    # there was exactly one, so anything Kory phrased loosely enough to match two
    # tasks failed outright even when one was obviously the intended task.
    match, candidates = pick_task(raw, candidates)
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
    if not candidates:
        return "", {
            "ok": False,
            "error": f"No task found matching {raw!r}.",
            "candidates": [],
        }
    # Close enough to be a coin flip. This path leads to "mark it complete", and
    # completing the wrong task is not a silent error — name them and ask.
    return "", {
        "ok": False,
        "error_code": "task_ambiguous",
        "error": (
            f"{raw!r} could be {len(candidates)} tasks — which one? "
            + "; ".join(str(c.get("name")) for c in candidates[:5])
        ),
        "candidates": [
            {"gid": c.get("gid"), "name": c.get("name"), "project": c.get("project")}
            for c in candidates[:5]
        ],
    }


def _add_task_to_section_if_configured(
    task_gid: str, section: str = "", project_gid: str = ""
) -> str:
    home_gid = (settings.asana_project_gid or "").strip()
    target_project = (project_gid or home_gid).strip()
    if section.strip():
        section_gid = resolve_section_gid(section, target_project)
    elif target_project and home_gid and target_project != home_gid:
        # The default section belongs to Kory's personal project; applying it
        # to a task on another project would silently drag the task back home.
        section_gid = ""
    else:
        section_gid = settings.asana_section_gid
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


TaskBucket = Literal["overdue", "due_today", "upcoming", "all", "completed", "any"]

TASK_FIELDS = ["name", "completed", "due_on", "notes", "assignee", "assignee.name"]

# Work addresses win when one person has two Asana accounts.
_COMPANY_EMAIL_DOMAINS = ("@iconicfounders.com", "@ifg.vc")


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
    project: str = "",
    assignee: str = "",
    approved: bool = False,
) -> dict[str, Any]:
    from app.safety.approval_gate import assert_kory_approved_write

    assert_kory_approved_write(approved=approved, action="Asana create task")

    # Kory keeps eight projects. "Create a task" without naming one used to file
    # silently into the default, so the task existed somewhere he wasn't looking
    # and Lexi reported it done. Ask instead — he can still say "default".
    if not project.strip():
        options = [r["name"] for r in list_asana_project_options().get("projects", [])]
        return {
            "ok": False,
            "error_code": "project_required",
            "error": "No project named — ask Kory which one before creating the task.",
            "projects": options,
            "kory_message": (
                "Which project should this go in?\n"
                + "\n".join(f"• {name}" for name in options)
                + "\n\nOr say \"default\" for your usual list."
            ),
        }

    project_gid = ""
    project_name = ""
    if project.strip().lower() in {"default", "usual", "my list", "personal"}:
        project = ""
    if project.strip():
        project_gid, project_name = resolve_project_gid(project)
        if not project_gid:
            names = (
                ", ".join(r["name"] for r in list_asana_project_options().get("projects", []))
                or "none found"
            )
            return {
                "ok": False,
                "error": (
                    f"No Asana project matching {project!r} (or the name matches several). "
                    f"Available projects: {names}."
                ),
            }
    result = _create_asana_task(
        title=title,
        notes=notes or title,
        section=section,
        project_gid=project_gid,
        project_name=project_name,
    )
    # Due date and assignee are set on the created task. Asana's create call does
    # not take an assignee here, so it is a second write — but a create that was
    # asked to assign and did not must not report success, or "create a task for
    # Heidi" leaves an unowned task and says it is hers.
    if result.get("ok") and result.get("task_id"):
        if due_on.strip():
            result["due_update"] = update_asana_task(
                task_gid=str(result["task_id"]), due_on=due_on.strip(), approved=approved
            )
        if assignee.strip():
            assigned = update_asana_task(
                task_gid=str(result["task_id"]), assignee=assignee.strip(), approved=approved
            )
            result["assignee_update"] = assigned
            if assigned.get("ok"):
                result["assignee"] = assigned.get("assignee") or assignee.strip()
            else:
                result["ok"] = False
                result["error"] = (
                    f"Task created, but assigning it to {assignee.strip()!r} failed: "
                    f"{assigned.get('error') or 'unknown error'}"
                )
    return result


def _asana_write_ok(result: dict[str, Any]) -> tuple[bool, str]:
    """Did the Asana call actually do anything?

    execute_asana_tool only raises when Composio sets `error`. A vendor refusal
    comes back as successful=false with no error, which every writer here used
    to discard in favour of a hardcoded ok:True — so a task filed into the wrong
    section, or not filed at all, still reported "Done!" to Kory.
    """
    if not isinstance(result, dict):
        return False, f"unexpected Asana response: {type(result).__name__}"
    if result.get("dry_run"):
        return True, ""
    if result.get("successful") is False:
        data = result.get("data")
        detail = ""
        if isinstance(data, dict):
            detail = str(data.get("error") or data.get("message") or "").strip()
        return False, detail or "Asana rejected the request"
    return True, ""


def _write_result(result: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """Build a writer's return value from what Asana actually reported."""
    ok, detail = _asana_write_ok(result)
    payload: dict[str, Any] = {"ok": ok, "dry_run": bool(result.get("dry_run")), **extra}
    if not ok:
        payload["error"] = detail
    return payload


def resolve_asana_user_gid(name_or_email: str) -> tuple[str, str]:
    """Resolve a person to an Asana user gid. Returns (gid, display_name)."""
    needle = (name_or_email or "").strip().lower()
    if not needle:
        return "", ""
    if needle.isdigit():
        return needle, ""
    if needle in {"me", "kory", "my", "myself", "mine"}:
        try:
            me = execute_asana_tool("ASANA_GET_CURRENT_USER", {})
            payload = (me.get("data") or {}).get("data") or {}
            if payload.get("gid"):
                return str(payload["gid"]), str(payload.get("name") or "")
        except Exception as exc:  # fall through to the workspace search
            logger.warning("ASANA_GET_CURRENT_USER failed: %s", exc)
        # ASANA_GET_CURRENT_USER came back empty in production, which left
        # "assign it to me" searching the workspace for a user literally named
        # "me". This is Kory's assistant — first person always means Kory.
        needle = "kory"
    workspace = os.getenv("ASANA_WORKSPACE_GID", "").strip()
    if not workspace:
        try:
            spaces = execute_asana_tool("ASANA_GET_MULTIPLE_WORKSPACES", {"limit": 5})
            rows = (spaces.get("data") or {}).get("data") or []
            if rows:
                workspace = str(rows[0].get("gid") or "")
        except Exception as exc:
            logger.warning("Could not resolve Asana workspace: %s", exc)
    if not workspace:
        return "", ""
    try:
        users = execute_asana_tool(
            "ASANA_GET_USERS_FOR_WORKSPACE",
            {"workspace_gid": workspace, "limit": 100, "opt_fields": ["name", "email"]},
        )
    except Exception as exc:
        logger.warning("ASANA_GET_USERS_FOR_WORKSPACE failed: %s", exc)
        return "", ""
    rows = (users.get("data") or {}).get("data") or []
    # Sujash Barman has two accounts in this workspace — a company address and a
    # university one. First-match-wins silently picked the university account, so
    # "assign it to Sujash" landed on the wrong record and reported success. When
    # a name matches more than one person, prefer the company address rather than
    # whichever Asana happened to list first.
    matches = [
        row
        for row in (rows if isinstance(rows, list) else [])
        if needle == str(row.get("email") or "").lower()
        or needle == str(row.get("name") or "").lower()
        or needle in str(row.get("name") or "").lower().split()
    ]
    if len(matches) > 1:
        company = [
            row
            for row in matches
            if str(row.get("email") or "").lower().endswith(_COMPANY_EMAIL_DOMAINS)
        ]
        if len(company) == 1:
            return str(company[0].get("gid") or ""), str(company[0].get("name") or "")

    for row in rows if isinstance(rows, list) else []:
        name = str(row.get("name") or "").lower()
        email = str(row.get("email") or "").lower()
        if needle == email or needle == name or needle in name.split():
            return str(row.get("gid") or ""), str(row.get("name") or "")

    return _fuzzy_workspace_user(needle, rows if isinstance(rows, list) else [])


# A person is a smaller target than a task title, so the bar sits higher.
_USER_MATCH_MIN = 0.62
_USER_MATCH_MARGIN = 0.08


def _fuzzy_workspace_user(needle: str, rows: list[dict[str, Any]]) -> tuple[str, str]:
    """Last resort when an exact token match found nobody.

    Exact matching is brittle against how people are actually referred to:
    Asana holds "Anju Kummetha", so "Anjana" — her real name — resolved to
    nobody, and any descriptor ("Anju — CEO Executive AI Tools") killed it
    outright. Both came back as "no such user in this workspace", which reads as
    the person not existing.
    """
    from app.integrations.asana_task_match import score_task_name

    # Drop anything after a dash or bracket: that is a description of the person,
    # not their name. "Anju — CEO Executive AI Tools (Summer 2026)" is Anju.
    core = re.split(r"[—–\-(\[]", needle, maxsplit=1)[0].strip() or needle
    if not core:
        return "", ""

    scored: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        name = str(row.get("name") or "")
        local = str(row.get("email") or "").split("@", 1)[0].replace(".", " ")
        best = max(score_task_name(core, name), score_task_name(core, local))
        if best >= _USER_MATCH_MIN:
            scored.append((best, row))
    if not scored:
        return "", ""

    scored.sort(key=lambda pair: -pair[0])
    if len(scored) > 1 and scored[0][0] - scored[1][0] < _USER_MATCH_MARGIN:
        # Too close to call. Prefer the work account (one person, two logins);
        # otherwise decline rather than assign someone else's task to a guess.
        top = [row for score, row in scored if scored[0][0] - score < _USER_MATCH_MARGIN]
        company = [
            row
            for row in top
            if str(row.get("email") or "").lower().endswith(_COMPANY_EMAIL_DOMAINS)
        ]
        if len(company) != 1:
            return "", ""
        return str(company[0].get("gid") or ""), str(company[0].get("name") or "")

    winner = scored[0][1]
    return str(winner.get("gid") or ""), str(winner.get("name") or "")


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
    written = _write_result(
        result,
        task_gid=task_gid,
        blocked_reason=(result.get("data") or {}).get("blocked_reason")
        if isinstance(result.get("data"), dict)
        else None,
    )
    if not written.get("ok"):
        return written

    # "Marked it complete" has to mean the task is complete. The successful flag
    # says Asana accepted the call, not that the state changed — so read it back
    # and let the task itself settle it.
    observed = _read_task_state(task_gid)
    if observed is None:
        written["verified"] = False
        written["warning"] = (
            "Asana accepted the change but the task could not be read back to confirm it."
        )
        return written
    written["verified"] = bool(observed.get("completed"))
    written["name"] = observed.get("name")
    if not written["verified"]:
        written["ok"] = False
        written["error"] = (
            f"Asana accepted the update but {observed.get('name') or task_gid!r} is still open."
        )
    return written


def _read_task_state(task_gid: str) -> dict[str, Any] | None:
    """Current name/completed/assignee for a task. None means unverified."""
    try:
        res = execute_asana_tool("ASANA_GET_A_TASK", {"task_gid": task_gid})
    except Exception as exc:  # noqa: BLE001 — a failed read-back is not a failed write
        logger.warning("Asana read-back failed for %s: %s", task_gid, exc)
        return None
    row = (res.get("data") or {}).get("data") or {}
    if not isinstance(row, dict) or not row.get("gid"):
        return None
    assignee = row.get("assignee")
    return {
        "gid": str(row.get("gid")),
        "name": row.get("name"),
        "completed": bool(row.get("completed")),
        "assignee": (assignee or {}).get("name") if isinstance(assignee, dict) else assignee,
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
    assignee: str = "",
    approved: bool = False,
    owner_ack: bool = False,
) -> dict[str, Any]:
    """Update title/notes/due date/assignee — blocked unless live writes enabled."""
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
    assignee_name = ""
    if assignee.strip():
        # ASANA_UPDATE_A_TASK's `data` takes assignee (schema-checked), but it
        # wants a user gid — a display name is silently ignored, which is how
        # "assign to Kory" appeared to work while nothing changed.
        user_gid, assignee_name = resolve_asana_user_gid(assignee)
        if not user_gid:
            return {
                "ok": False,
                "error": (
                    f"No Asana user matching {assignee!r} in this workspace. "
                    "Use their full name or email as it appears in Asana."
                ),
            }
        data["assignee"] = user_gid
    if not data:
        return {"ok": False, "error": "No fields to update."}
    if _should_simulate_asana():
        return {"ok": True, "task_gid": task_gid, "simulated": True, "dry_run": True, "data": data}
    result = execute_asana_tool(
        "ASANA_UPDATE_A_TASK",
        {"task_gid": task_gid, "data": data},
    )
    written = _write_result(result, task_gid=task_gid, assignee=assignee_name or None)
    if not written.get("ok"):
        return written
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
    return _write_result(result, task_gid=task_gid)


def search_asana_tasks(
    *, query: str, limit: int = 15, mine_only: bool = False
) -> dict[str, Any]:
    """Search tasks by name across configured / related projects (read-only).

    Ranked, not literal. Substring containment meant "the elevator task" matched
    nothing — the trailing word "task" alone was enough to miss "Load Elevator
    market-study landing page" — and a typo on either side (Asana itself spells
    one contact "Krinksy") found nothing at all. Lexi reported both as the task
    not existing.
    """
    from app.integrations.asana_task_match import rank_tasks

    if not query.strip():
        return {"ok": False, "tasks": [], "error": "query is required"}
    projects = list_asana_project_options()
    pool: list[dict[str, Any]] = []
    seen: set[str] = set()
    for project in projects.get("projects", []):
        # 500, not 50: one project already sits at 47 and a silent truncation here
        # reads as "no such task". bucket="any" so completed tasks are searchable —
        # "did I finish X?", "reopen X" and "mark X complete" (when it already is)
        # all need to find a done task and say so rather than deny it exists.
        listed = list_asana_tasks(
            bucket="any",
            limit=500,
            project_gid=str(project.get("gid") or ""),
            project_name=str(project.get("name") or ""),
            mine_only=mine_only,
        )
        for task in listed.get("tasks") or []:
            gid = str(task.get("gid") or "")
            if gid and gid in seen:
                continue
            if gid:
                seen.add(gid)
            pool.append({**task, "project": project.get("name")})
    ranked = rank_tasks(query, pool)
    return {"ok": True, "query": query, "tasks": ranked[:limit], "scanned": len(pool)}


def move_asana_task_to_section(
    *,
    task_gid: str,
    section_gid: str = "",
    section_name: str = "",
    project: str = "",
    unfile_others: bool = False,
    approved: bool = False,
    owner_ack: bool = False,
) -> dict[str, Any]:
    from app.safety.approval_gate import assert_kory_approved_write

    assert_kory_approved_write(approved=approved, action="Asana move task section")
    task_gid, _resolve_error = resolve_task_or_error(task_gid, owner_ack=owner_ack)
    if _resolve_error:
        return _resolve_error
    project_gid = ""
    if project.strip():
        project_gid, _project_name = resolve_project_gid(project)
        if not project_gid:
            names = (
                ", ".join(r["name"] for r in list_asana_project_options().get("projects", []))
                or "none found"
            )
            return {
                "ok": False,
                "error": (
                    f"No Asana project matching {project!r}. Available projects: {names}."
                ),
            }
    target = section_gid.strip()
    if not target and section_name.strip():
        target = resolve_section_gid(section_name, project_gid)
        if not target:
            names = ", ".join(r["name"] for r in list_project_sections(project_gid)) or "none found"
            return {
                "ok": False,
                "error": f"No board named '{section_name}'. Available boards: {names}.",
            }
    # A project was named but no section. Previously `target` fell through to the
    # global ASANA_SECTION_GID default — a section in the *original* project — so
    # "move it to <other project>" filed the task under "Reservation Reminders"
    # and reported success. Route through the project instead; Asana picks that
    # project's default section.
    if project_gid and not target:
        if _should_simulate_asana():
            return {
                "ok": True,
                "task_gid": task_gid,
                "project_gid": project_gid,
                "simulated": True,
                "dry_run": True,
            }
        added = execute_asana_tool(
            "ASANA_ADD_PROJECT_FOR_TASK",
            {"task_gid": task_gid, "project": project_gid},
        )
        out = _write_result(added, task_gid=task_gid, project_gid=project_gid)
        if out.get("ok") and unfile_others:
            out.update(unfile_task_from_other_projects(
                task_gid=task_gid, keep_project_gid=project_gid
            ))
        return out

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
    out = _write_result(result, task_gid=task_gid, section_gid=target)
    if out.get("ok") and unfile_others and project_gid:
        out.update(unfile_task_from_other_projects(
            task_gid=task_gid, keep_project_gid=project_gid
        ))
    return out


def _task_project_gids(task_gid: str) -> list[str]:
    """Project gids a task currently belongs to (read-only)."""
    try:
        res = execute_asana_tool("ASANA_GET_A_TASK", {"task_gid": task_gid})
    except Exception as exc:
        logger.warning("Could not read projects for task %s: %s", task_gid, exc)
        return []
    payload = (res.get("data") or {}).get("data") or {}
    return [str(p.get("gid")) for p in (payload.get("projects") or []) if p.get("gid")]


def unfile_task_from_other_projects(
    *, task_gid: str, keep_project_gid: str
) -> dict[str, Any]:
    """Drop every project membership except keep_project_gid.

    Asana tasks are multi-homed, so adding one never displaced the old — "move
    it to X" left the task on its previous board too, and Kory had no way to
    undo that from chat. This is what makes a move a move.
    """
    removed, failed = [], []
    for gid in _task_project_gids(task_gid):
        if gid == keep_project_gid:
            continue
        try:
            res = execute_asana_tool(
                "ASANA_REMOVE_PROJECT_FROM_TASK", {"task_gid": task_gid, "project": gid}
            )
        except Exception as exc:
            failed.append({"project_gid": gid, "error": str(exc)[:160]})
            continue
        ok, detail = _asana_write_ok(res)
        (removed if ok else failed).append(
            {"project_gid": gid} if ok else {"project_gid": gid, "error": detail}
        )
    return {"removed_from": removed, "remove_failures": failed}


def remove_asana_task_from_project(
    *,
    task_gid: str,
    project: str,
    approved: bool = False,
    owner_ack: bool = False,
) -> dict[str, Any]:
    """Unfile a task from one project.

    Asana tasks are multi-homed, so adding to a project never removed the old
    one — every "move" silently accumulated another project. This is the missing
    half; pair it with move_asana_task_to_section for a real move.
    """
    from app.safety.approval_gate import assert_kory_approved_write

    assert_kory_approved_write(approved=approved, action="Asana remove task from project")
    task_gid, _resolve_error = resolve_task_or_error(task_gid, owner_ack=owner_ack)
    if _resolve_error:
        return _resolve_error
    project_gid, project_name = resolve_project_gid(project)
    if not project_gid:
        names = (
            ", ".join(r["name"] for r in list_asana_project_options().get("projects", []))
            or "none found"
        )
        return {"ok": False, "error": f"No Asana project matching {project!r}. Available: {names}."}
    if _should_simulate_asana():
        return {"ok": True, "task_gid": task_gid, "project_gid": project_gid, "dry_run": True}
    result = execute_asana_tool(
        "ASANA_REMOVE_PROJECT_FROM_TASK",
        {"task_gid": task_gid, "project": project_gid},
    )
    return _write_result(
        result, task_gid=task_gid, project_gid=project_gid, project=project_name
    )


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
    return _write_result(result, task_gid=task_gid, comment=text)


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


ASANA_PAGE_SIZE = 100  # Asana's per-page maximum
ASANA_MAX_PAGES = 6  # 600 tasks; bounds Composio spend on a huge project


def _fetch_project_tasks_paged(project_gid: str) -> dict[str, Any]:
    """All tasks in a project, following Asana's next_page cursor.

    Returns the same shape as a single execute_asana_tool call so callers are
    unchanged, with `truncated` set if we stopped at ASANA_MAX_PAGES — a cap
    that is reported rather than silently applied.
    """
    rows: list[Any] = []
    offset = ""
    log_id = None
    dry_run = False
    truncated = False
    for page in range(ASANA_MAX_PAGES):
        args: dict[str, Any] = {
            "project_gid": project_gid,
            "limit": ASANA_PAGE_SIZE,
            # Without opt_fields Asana returns only gid and name, so due_on and
            # completed came back empty and every date bucket was empty: 29
            # overdue tasks reported as "you're all clear".
            "opt_fields": TASK_FIELDS,
        }
        if offset:
            args["offset"] = offset
        result = execute_asana_tool("ASANA_GET_TASKS_FROM_A_PROJECT", args)
        log_id = result.get("log_id") or log_id
        dry_run = bool(result.get("dry_run")) or dry_run
        data = result.get("data")
        if not isinstance(data, dict):
            break
        page_rows = data.get("data")
        rows.extend(page_rows if isinstance(page_rows, list) else [])
        offset = str(((data.get("next_page") or {}) or {}).get("offset") or "")
        if not offset:
            break
        if page == ASANA_MAX_PAGES - 1:
            truncated = True
            logger.warning(
                "Asana project %s has more than %s tasks; list truncated.",
                project_gid,
                ASANA_MAX_PAGES * ASANA_PAGE_SIZE,
            )
    return {"data": {"data": rows}, "log_id": log_id, "dry_run": dry_run, "truncated": truncated}


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
        # Page through. Asana caps a page at 100 and hands back next_page.offset;
        # the old code took page one and dropped the cursor, so a project with
        # more tasks than one page silently under-reported — and *which* tasks
        # you saw depended on where the page happened to fall. A 9-task project
        # returned 4 at limit=50 because the first 50 rows were mostly completed.
        # Fetch pages, then filter — never filter a single arbitrary page.
        result = _fetch_project_tasks_paged(target_gid)
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
        "truncated": bool(result.get("truncated")),
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
    if bucket == "any":
        # Open and done together. Search needs both, and the buckets are applied
        # to an already-fetched list — asking twice fetched Asana twice.
        return tasks
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
