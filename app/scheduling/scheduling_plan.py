"""LLM + rules scheduling plan — window, duration, format before slot search."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.config import settings
from app.scheduling.scheduling_window import (
    SchedulingWindow,
    TimeOfDayWindow,
    infer_scheduling_window,
)

MT = ZoneInfo(settings.scheduling_timezone)

# The longest window a sender can plausibly mean; anything wider is treated as
# an LLM misread and discarded in favor of the rule parser.
_MAX_LLM_WINDOW_DAYS = 45

PLAN_SYSTEM_PROMPT = """You are Lexi, Kory Mitchell's scheduling assistant.
Read the email and return ONLY valid JSON with these keys:
- task_type: "offer_times" | "general_reply" | "no_action"
- window_start: "YYYY-MM-DD" or null — the FIRST day the sender's request covers
- window_end: "YYYY-MM-DD" or null — the LAST day (inclusive). Resolve relative
  phrases from the `today` field in the input ("next week" = Monday through
  Sunday of the week after today's; "this week or next" spans both weeks;
  "the week of the 17th" = that Monday-Sunday).
- window_label: short phrase echoing the sender's own words (e.g. "this week or next")
- earliest_hour: integer 0-23 or null — ONLY when the sender states a
  time-of-day preference ("early morning" = 7, "even 7 AM works" = 7, "after 3pm" = 15)
- latest_hour: integer 0-23 or null — the latest a meeting could START per the
  sender ("mornings" = 11, "before noon" = 11); null when unstated
- duration_minutes: integer or null (default 30 for intro calls)
- meeting_format: "virtual" | "in_person" | null
- urgency: boolean
- draft_context: one sentence on tone/context for the reply (no invented times)

Never invent a constraint the sender did not state — use null for anything
unstated. Do not propose specific clock times — only interpret the ask.
No markdown fences."""


@dataclass
class SchedulingPlan:
    task_type: str = "offer_times"
    window: SchedulingWindow | None = None
    duration_minutes: int | None = None
    meeting_format: str | None = None
    urgency: bool = False
    draft_context: str = ""
    source: str = "rules"
    raw: dict[str, Any] = field(default_factory=dict)
    # Sender's stated time-of-day preference, LLM-extracted and code-clamped.
    # When set it overrides the regex infer_time_of_day_window in the engine.
    time_window: TimeOfDayWindow | None = None
    # Kory's per-proposal Teams guidance ("Lunch approved for this one") —
    # carried on the plan so the engine can apply one-off rule exceptions.
    kory_guidance: str = ""


def build_scheduling_plan(
    *,
    subject: str = "",
    body: str = "",
    intent: str | None = None,
    reference_now: datetime | None = None,
    use_llm: bool = True,
) -> SchedulingPlan:
    """Combine rule-based window detection with optional LLM interpretation."""
    rule_window = infer_scheduling_window(subject=subject, body=body, now=reference_now)
    plan = SchedulingPlan(
        window=rule_window,
        source="rules" if rule_window else "default",
    )

    if not use_llm or not settings.llm_api_key:
        plan = _apply_intent_defaults(plan, intent)
        return plan

    try:
        from app.llm.hermes_client import get_hermes_client

        client = get_hermes_client()
        today_mt = (reference_now or datetime.now(tz=MT)).astimezone(MT)
        payload = {
            "subject": subject,
            "body": body,
            "intent": intent,
            # Without today's date the model cannot resolve "next week" to
            # real dates — it would be guessing the year and weekday.
            "today": today_mt.strftime("%Y-%m-%d (%A)"),
            "timezone": settings.scheduling_timezone,
            "rule_window": (
                {
                    "label": rule_window.label,
                    "start": rule_window.start.isoformat(),
                    "end": rule_window.end.isoformat(),
                }
                if rule_window
                else None
            ),
        }
        response = client.chat.completions.create(
            role="scheduler",
            messages=[
                {"role": "system", "content": PLAN_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, default=str)},
            ],
        )
        content = response.choices[0].message.content or ""
        parsed = _parse_json_object(content)
        plan = _merge_llm_plan(plan, parsed, subject=subject, body=body, now=reference_now)
        plan.source = "llm+rules" if rule_window else "llm"
        plan.raw = parsed
    except Exception:
        pass

    return _apply_intent_defaults(plan, intent)


def _apply_intent_defaults(plan: SchedulingPlan, intent: str | None) -> SchedulingPlan:
    if plan.task_type == "offer_times" and plan.duration_minutes is None:
        from app.scheduling.meeting_type import resolve_meeting_type

        spec = resolve_meeting_type(intent=intent or "")
        plan.duration_minutes = spec.duration_minutes
    return plan


def _parse_llm_date(value: Any) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def _llm_window_from_dates(
    parsed: dict[str, Any],
    *,
    sender_window: SchedulingWindow | None,
    today: date,
) -> SchedulingWindow | None:
    """Validate + clamp explicit LLM dates. Every check protects a live failure:
    the model does language, this code does the arithmetic guarantees."""
    start = _parse_llm_date(parsed.get("window_start"))
    end = _parse_llm_date(parsed.get("window_end"))
    if not start or not end or end < start:
        return None
    if end < today:
        return None  # entirely in the past — a misread, not a request
    if start < today:
        start = today  # "this week" said mid-week: clamp, don't reject
    if (end - start).days > _MAX_LLM_WINDOW_DAYS:
        return None
    # Keep the established anti-hallucination guard: when the sender named no
    # timeframe, an ungrounded single-day window hard-narrows scheduling to one
    # day and forces a needless defer to Kory.
    if sender_window is None and start == end:
        return None
    raw_label = parsed.get("window_label")
    label = raw_label.strip() if isinstance(raw_label, str) and raw_label.strip() else (
        f"{start.strftime('%b')} {start.day}–{end.strftime('%b')} {end.day}"
    )
    return SchedulingWindow(start=start, end=end, source="llm", label=label)


def _llm_time_window(parsed: dict[str, Any], *, east_coast: bool = False) -> TimeOfDayWindow | None:
    """Clamped time-of-day preference. Floor 7:00 (ruling V-1: Kory's earliest
    for outside meetings) — 6:00 for East-Coast contacts, whose lane Kory's
    spec explicitly allows (ruled 2026-08-08); a start at/after the end is
    discarded."""
    floor = 6 if east_coast else 7
    earliest = parsed.get("earliest_hour")
    latest = parsed.get("latest_hour")
    if not isinstance(earliest, int) and not isinstance(latest, int):
        return None
    start_hour = max(floor, earliest) if isinstance(earliest, int) and 0 <= earliest <= 23 else floor
    end_hour = latest if isinstance(latest, int) and 0 < latest <= 23 else 17
    end_hour = min(end_hour, 19)
    if end_hour <= start_hour:
        return None
    label_bits = []
    if isinstance(earliest, int):
        label_bits.append(f"from {start_hour}:00")
    if isinstance(latest, int):
        label_bits.append(f"until {end_hour}:00")
    return TimeOfDayWindow(
        start_hour=start_hour,
        start_minute=0,
        end_hour=end_hour,
        end_minute=0,
        label=" ".join(label_bits) or "stated preference",
    )


def _merge_llm_plan(
    plan: SchedulingPlan,
    parsed: dict[str, Any],
    *,
    subject: str,
    body: str,
    now: datetime | None,
) -> SchedulingPlan:
    task = str(parsed.get("task_type") or "offer_times").strip().lower()
    if task in {"offer_times", "general_reply", "no_action"}:
        plan.task_type = task

    # `plan.window` here is the deterministic rule window — None when the
    # regex parser found no timeframe in the sender's own email.
    sender_window = plan.window
    today = ((now or datetime.now(tz=MT)).astimezone(MT)).date()

    # Preferred path: explicit dates from the model, validated by code. This is
    # what frees scheduling from the regex vocabulary — "the week after Labor
    # Day" needs no new branch, just dates that pass the clamps.
    dated = _llm_window_from_dates(parsed, sender_window=sender_window, today=today)
    if dated is not None:
        plan.window = dated
    else:
        label = parsed.get("window_label")
        if isinstance(label, str) and label.strip():
            llm_window = infer_scheduling_window(
                subject=f"{subject} {label}",
                body=body,
                now=now,
            )
            # Same single-day guard as above, for the label fallback path.
            if llm_window and not (
                sender_window is None and llm_window.start == llm_window.end
            ):
                plan.window = llm_window

    from app.scheduling.scheduling_window import _EAST_COAST_CUE

    time_window = _llm_time_window(
        parsed, east_coast=bool(_EAST_COAST_CUE.search(f"{subject}\n{body}"))
    )
    if time_window is not None:
        plan.time_window = time_window

    dur = parsed.get("duration_minutes")
    if isinstance(dur, int) and dur > 0:
        plan.duration_minutes = dur
    elif isinstance(dur, str) and dur.isdigit():
        plan.duration_minutes = int(dur)

    fmt = parsed.get("meeting_format")
    if fmt in {"virtual", "in_person"}:
        plan.meeting_format = fmt

    plan.urgency = bool(parsed.get("urgency"))
    plan.draft_context = str(parsed.get("draft_context") or "").strip()
    return plan


def _parse_json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)
