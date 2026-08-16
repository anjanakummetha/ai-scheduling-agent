"""Merge rules.py defaults with explicit Kory memory overrides from Teams/Hermes."""

from __future__ import annotations

import re

from dataclasses import dataclass, field
from typing import Any

import rules as kory_rules
from app.storage.kory_memory import list_facts


@dataclass
class SchedulingPreferences:
    """Effective scheduling preferences (defaults + Kory memory)."""

    happy_hour_max_per_week: int = field(
        default_factory=lambda: int(kory_rules.CAPACITY_LIMITS.get("happy_hour_per_week", 2))
    )
    dinner_max_per_week: int = field(
        default_factory=lambda: int(kory_rules.CAPACITY_LIMITS.get("dinner_per_week", 1))
    )
    travel_week_max_meetings: int = 3
    lunch_allowed: bool = False
    # Remembered day rules, enforced by the validator (live K-3: "no meetings
    # before 8:30 AM MT Tuesdays" was stored and recalled fine — and the
    # engine offered Tue 7:00 AM anyway, because nothing below the prompt
    # ever read it). Weekdays are 0=Mon..6=Sun; the key -1 means every day.
    blocked_weekdays: set[int] = field(default_factory=set)
    earliest_start_by_day: dict[int, tuple[int, int]] = field(default_factory=dict)
    latest_end_by_day: dict[int, tuple[int, int]] = field(default_factory=dict)
    memory_facts: list[dict[str, Any]] = field(default_factory=list)

    def memory_prompt_block(self) -> str:
        if not self.memory_facts:
            return ""
        lines = ["KORY PREFERENCE OVERRIDES (from Teams — supersede defaults when relevant):"]
        for item in self.memory_facts:
            lines.append(f"- {item.get('fact_key')}: {item.get('fact_value')}")
        return "\n".join(lines)


def _parse_int(value: str, default: int) -> int:
    try:
        return int(value.strip())
    except (TypeError, ValueError):
        return default


def _parse_bool(value: str) -> bool | None:
    v = value.strip().lower()
    if v in {"1", "true", "yes", "on", "allowed"}:
        return True
    if v in {"0", "false", "no", "off", "disallowed", "never"}:
        return False
    return None


_LUNCH_YES = re.compile(
    # positive word before lunch ("fine with lunch") or after it ("lunch is
    # fine", "lunch approved for this one") — the one-directional pattern
    # missed Kory's actual approval phrasing live (I-2).
    r"\b(?:fine|ok(?:ay)?|good|happy|open|available|yes|approved?|allow(?:ed)?)\b"
    r"[^.!?]{0,40}\blunch(?:es| meetings?)?\b"
    r"|\blunch(?:es| meetings?)?\b[^.!?]{0,40}"
    r"\b(?:fine|ok(?:ay)?|good|works|approved?|allow(?:ed)?|yes|exception)\b",
    re.IGNORECASE,
)
_LUNCH_NO = re.compile(
    r"\b(?:no|never|don'?t|do not|stop|avoid)\b[^.!?]{0,40}\blunch(?:es| meetings?)?\b",
    re.IGNORECASE,
)
_CAP_PATTERNS = (
    ("happy_hour_max_per_week", re.compile(r"\b(\d+)\b[^.!?]{0,30}\bhappy hours?\b", re.I)),
    ("dinner_max_per_week", re.compile(r"\b(\d+)\b[^.!?]{0,30}\bdinners?\b", re.I)),
    ("travel_week_max_meetings", re.compile(r"\b(\d+)\b[^.!?]{0,40}\btravel\b", re.I)),
)

_DAY_INDEX = {
    "monday": 0, "mondays": 0, "tuesday": 1, "tuesdays": 1,
    "wednesday": 2, "wednesdays": 2, "thursday": 3, "thursdays": 3,
    "friday": 4, "fridays": 4, "saturday": 5, "saturdays": 5,
    "sunday": 6, "sundays": 6,
}
_DAY_ALT = "|".join(_DAY_INDEX)

# "no meetings on Fridays" / "keep Mondays clear" / "block off Wednesdays".
_DAY_BLOCK_RE = re.compile(
    rf"\b(?:no|zero|never)\s+(?:more\s+)?(?:meetings?|calls?|appointments?|scheduling|anything)\s+"
    rf"(?:on\s+)?({_DAY_ALT})\b"
    rf"|\bkeep\s+({_DAY_ALT})\s+(?:free|clear|open)\b"
    rf"|\bblock\s+(?:off\s+)?({_DAY_ALT})\b",
    re.IGNORECASE,
)
# "Friday is fine" / "Mondays are ok" — a guidance-level unblock.
_DAY_UNBLOCK_RE = re.compile(
    rf"\b({_DAY_ALT})\s+(?:is|are)\s+(?:fine|ok(?:ay)?|good|approved)\b",
    re.IGNORECASE,
)
# "no meetings before 8:30 AM MT Tuesdays" / "nothing before 10" /
# "don't schedule anything after 4 pm on Fridays".
_TIME_BOUND_RE = re.compile(
    rf"\b(?:no\s+(?:meetings?|calls?|appointments?|anything)\s+|nothing\s+"
    rf"|don'?t\s+(?:schedule|book)\s+(?:anything\s+|meetings?\s+)?)"
    rf"(before|after)\s+(\d{{1,2}})(?::(\d{{2}}))?\s*(am|pm)?"
    rf"(?:\s*(?:MT|mountain(?:\s+time)?))?"
    rf"(?:\s+(?:on\s+)?({_DAY_ALT}))?",
    re.IGNORECASE,
)
# "before 8 is fine (for this one)" lifts the FLOOR; "after 4 is fine" lifts
# the CAP — clearing floors for both silently deleted a standing rule Kory
# never mentioned (review defect 5).
_FLOOR_LIFT_RE = re.compile(
    r"\bbefore\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?\s+is\s+"
    r"(?:fine|ok(?:ay)?|good|approved)\b"
    r"|\bearly\s+is\s+(?:fine|ok(?:ay)?|good|approved)\b",
    re.IGNORECASE,
)
_CAP_LIFT_RE = re.compile(
    r"\bafter\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?\s+is\s+"
    r"(?:fine|ok(?:ay)?|good|approved)\b"
    r"|\blate\s+is\s+(?:fine|ok(?:ay)?|good|approved)\b",
    re.IGNORECASE,
)
_DAYPART_NEARBY_RE = re.compile(r"\b(?:morning|afternoon|evening)s?\b", re.IGNORECASE)
# "no meetings Friday this week" is a one-week ask, not a standing rule — a
# permanent weekday block from it would outlive the week it was about.
_TEMPORAL_SCOPE_RE = re.compile(
    r"\b(?:this|next|the coming)\s+(?:week|monday|tuesday|wednesday|thursday|"
    r"friday)\b|\btomorrow\b|\btoday\b|\bthis one\b",
    re.IGNORECASE,
)


def _bound_hour(hour: int, minute: int, meridiem: str | None) -> tuple[int, int] | None:
    if not 1 <= hour <= 12 and not (meridiem is None and 0 <= hour <= 23):
        return None
    mer = (meridiem or "").lower()
    if mer == "pm" and hour < 12:
        hour += 12
    elif mer == "am" and hour == 12:
        hour = 0
    elif not mer and 1 <= hour <= 7:
        hour += 12  # business-hours reading: a bare "after 4" is 4 PM
    return (hour, minute)


def _apply_day_rules(prefs: SchedulingPreferences, value: str) -> None:
    """Enforceable day rules from a remembered sentence or Teams guidance."""
    for match in _DAY_BLOCK_RE.finditer(value):
        # "no meetings Friday afternoons" is a time rule, not a full-day block,
        # and "no meetings Friday this week" is one week, not a standing rule.
        window = value[max(0, match.start() - 10):match.end() + 16]
        if _DAYPART_NEARBY_RE.search(window) or _TEMPORAL_SCOPE_RE.search(window):
            continue
        token = next(g for g in match.groups() if g)
        prefs.blocked_weekdays.add(_DAY_INDEX[token.lower()])
    for match in _DAY_UNBLOCK_RE.finditer(value):
        prefs.blocked_weekdays.discard(_DAY_INDEX[match.group(1).lower()])
    for match in _TIME_BOUND_RE.finditer(value):
        kind, hour_s, minute_s, meridiem, day = match.groups()
        bound = _bound_hour(int(hour_s), int(minute_s or 0), meridiem)
        if bound is None:
            continue
        hour_raw = int(hour_s)
        if not meridiem:
            if kind.lower() == "before" and 1 <= hour_raw <= 12:
                # "before 10" with no meridiem is a morning floor, not 10 PM.
                bound = (hour_raw, int(minute_s or 0))
            elif kind.lower() == "after" and 1 <= hour_raw <= 11:
                # "nothing after 8" means 8 PM — an 8 AM cap would reject
                # every working hour of every day (review defect 6).
                bound = (hour_raw + 12, int(minute_s or 0))
        day_key = _DAY_INDEX[day.lower()] if day else -1
        target = (
            prefs.earliest_start_by_day
            if kind.lower() == "before"
            else prefs.latest_end_by_day
        )
        target[day_key] = bound
    if _FLOOR_LIFT_RE.search(value):
        prefs.earliest_start_by_day.clear()
    if _CAP_LIFT_RE.search(value):
        prefs.latest_end_by_day.clear()


_SLOT_MIN_RELAX = re.compile(
    r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"january|february|march|april|june|july|august|september|october|november|december|"
    r"week|weeks|today|tomorrow|morning|mornings|afternoon|afternoons|evening|evenings|"
    r"noon|lunch|breakfast|dinner|am|pm|a\.m\.|p\.m\.|time|times|slot|slots|"
    r"hour|hours|minutes|early|earlier|late|later|only|exception|asap|urgent|"
    r"available|availability|open)\b"
    r"|\b\d{1,2}(?::\d{2})?\b",
    re.IGNORECASE,
)


def guidance_relaxes_slot_minimum(guidance: str) -> bool:
    """A single offered slot is acceptable ONLY when Kory's guidance actually
    constrains the search — a day, a time window, a policy exception ("lunch is
    fine", "try Friday", "only mornings"). Style or redo guidance ("redo the
    draft in my voice") must keep the normal 2-slot minimum: live defect
    2026-08-05, a redo request silently produced a one-slot offer."""
    return bool(_SLOT_MIN_RELAX.search(guidance or ""))


def _apply_freeform_fact(prefs: SchedulingPreferences, value: str) -> None:
    """Read a remembered sentence for rules the engine can actually enforce.

    "remember" stores natural language under an opaque key (email:<thread-id>),
    so a preference Kory states in his own words reached the draft prompt but
    never the validator — he could say he is fine with lunch and still never be
    offered one.
    """
    if _LUNCH_NO.search(value):
        prefs.lunch_allowed = False
    elif _LUNCH_YES.search(value):
        prefs.lunch_allowed = True

    for attr, pattern in _CAP_PATTERNS:
        match = pattern.search(value)
        if match:
            setattr(prefs, attr, _parse_int(match.group(1), getattr(prefs, attr)))

    _apply_day_rules(prefs, value)


def load_scheduling_preferences(guidance: str = "") -> SchedulingPreferences:
    """Load defaults merged with kory_memory scheduling facts.

    `guidance` is Kory's per-proposal instruction from a Teams escalation
    ("Lunch approved for this one"). It is scanned with the same enforceable-
    preference rules as memory facts and applied LAST, so a one-off exception
    overrides the standing rule for this run only — live I-2 defect: the
    guidance reached the draft prompt but never the validator, so approving a
    lunch exception still produced zero lunch slots.
    """
    prefs = SchedulingPreferences()
    facts = list_facts(limit=100)
    prefs.memory_facts = facts

    _KNOWN_KEYS = {
        "happy_hour_max_per_week", "happy_hour_per_week", "max_happy_hours",
        "dinner_max_per_week", "dinner_per_week", "max_dinners",
        "travel_week_max_meetings", "travel_check_ins",
        "lunch_meetings", "allow_lunch",
    }

    for item in facts:
        key = str(item.get("fact_key") or "").strip().lower()
        value = str(item.get("fact_value") or "").strip()
        if not key or not value:
            continue
        if key not in _KNOWN_KEYS:
            _apply_freeform_fact(prefs, value)
            continue
        if key in {"happy_hour_max_per_week", "happy_hour_per_week", "max_happy_hours"}:
            prefs.happy_hour_max_per_week = _parse_int(value, prefs.happy_hour_max_per_week)
        elif key in {"dinner_max_per_week", "dinner_per_week", "max_dinners"}:
            prefs.dinner_max_per_week = _parse_int(value, prefs.dinner_max_per_week)
        elif key in {"travel_week_max_meetings", "travel_check_ins"}:
            prefs.travel_week_max_meetings = _parse_int(value, prefs.travel_week_max_meetings)
        elif key in {"lunch_meetings", "allow_lunch"}:
            parsed = _parse_bool(value)
            if parsed is None:
                # e.g. "yes - Kory is fine with lunch meetings"
                _apply_freeform_fact(prefs, value)
            else:
                prefs.lunch_allowed = parsed

    if guidance.strip():
        _apply_freeform_fact(prefs, guidance)

    return prefs
