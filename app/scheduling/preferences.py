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
