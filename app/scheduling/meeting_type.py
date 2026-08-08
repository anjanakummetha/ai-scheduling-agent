"""Canonical meeting-type resolution — single source of truth for Kory scheduling."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import rules as kory_rules


def _parse_duration(text: str) -> int | None:
    from app.scheduling.calendar_intelligence import parse_duration_from_text

    return parse_duration_from_text(text)

# Triage / legacy intent aliases → canonical type key in rules.MEETING_TYPES
INTENT_ALIASES: dict[str, str] = {
    "dinner_request": "dinner",
    "lunch_request": "lunch",
    "meeting_request": "referral_or_intro",
    "referral": "referral_or_intro",
    "virtual_30": "referral_or_intro",
    "internal_sync": "referral_or_intro",
    "delegation": "referral_or_intro",
    "reschedule": "referral_or_intro",
    "unknown": "referral_or_intro",
}

NEW_CLIENT_CUES = (
    "diligence",
    "term sheet",
    "new client",
    "lp call",
    "portfolio company",
    "project ",
    "deal review",
    "investor",
    "acquisition",
)

INTRO_CUES = (
    "intro call",
    "intro meeting",
    "introduction",
    "quick intro",
    "referral",
    "30-minute intro",
    "30 minute intro",
    "30-min intro",
    "30 min intro",
    "30-minute call",
    "30 minute call",
    "30 minutes",
    "30 mins",
    "quick call",
    "introductory",
    "family office",
)

_SCHEDULING_SIGNAL_PATTERNS = (
    re.compile(r"\b30\s*[- ]?min(?:ute)?s?\b", re.I),
    re.compile(r"\b60\s*[- ]?min(?:ute)?s?\b", re.I),
    re.compile(r"\bnext week\b", re.I),
    re.compile(r"\bfind a time\b", re.I),
    re.compile(r"\bset up\b.{0,40}\b(call|meeting|intro)\b", re.I),
    re.compile(r"\bwould you have\b", re.I),
    re.compile(r"\bwhat works\b", re.I),
    re.compile(r"\bavailability\b", re.I),
    re.compile(r"\bschedule\b", re.I),
    re.compile(r"\bintro call\b", re.I),
)

PODCAST_CUES = (
    "the turn",
    "podcast",
    "pre-interview",
    "recording",
)


@dataclass(frozen=True)
class MeetingTypeSpec:
    """Resolved meeting type for slot search, validation, and (later) draft context."""

    type_key: str
    label: str
    duration_minutes: int
    calendar_block_minutes: int
    default_format: str
    preferred_times: tuple[str, ...]
    triage_intent: str

    def draft_type_label(self) -> str:
        """Short label for outbound email context (Section C)."""
        labels = {
            "referral_or_intro": "30-minute virtual intro call",
            "new_client": "60-minute client meeting",
            "coffee": "coffee meeting",
            "happy_hour": "happy hour",
            "dinner": "dinner meeting",
            "podcast": "podcast recording",
            "lunch": "lunch meeting",
        }
        return labels.get(self.type_key, self.label.lower())

    def card_type_label(self) -> str:
        """Short label for Teams approval cards."""
        labels = {
            "referral_or_intro": "Intro",
            "new_client": "New client",
            "coffee": "Coffee",
            "happy_hour": "Happy hour",
            "dinner": "Dinner",
            "podcast": "Podcast",
            "lunch": "Lunch",
            "virtual_30": "Virtual",
        }
        return labels.get(self.type_key, self.label)


def _type_config(type_key: str) -> dict[str, Any]:
    return dict(kory_rules.MEETING_TYPES.get(type_key) or {})


def _spec_from_type_key(
    type_key: str,
    *,
    triage_intent: str = "",
    duration_override: int | None = None,
) -> MeetingTypeSpec:
    cfg = _type_config(type_key)
    duration = duration_override or int(
        cfg.get("duration_minutes") or cfg.get("calendar_block_minutes") or 30
    )
    block = int(cfg.get("calendar_block_minutes") or duration)
    if type_key == "coffee":
        block = int(cfg.get("calendar_block_minutes") or 90)
        duration = int(cfg.get("duration_minutes") or 60)
    elif type_key in {"happy_hour", "dinner"}:
        duration = int(cfg.get("duration_minutes") or 90)
        block = duration
    preferred = tuple(cfg.get("preferred_times") or ())
    fmt = str(cfg.get("format") or "virtual_default")
    if fmt == "virtual_default":
        fmt = "virtual"
    elif fmt == "virtual_or_inperson":
        fmt = "virtual"
    return MeetingTypeSpec(
        type_key=type_key,
        label=str(cfg.get("label") or type_key.replace("_", " ").title()),
        duration_minutes=duration,
        calendar_block_minutes=block,
        default_format=fmt,
        preferred_times=preferred,
        triage_intent=triage_intent or type_key,
    )


def _combined_text(subject: str, body: str) -> str:
    return f"{subject}\n{body}".lower()


def _scheduling_text(subject: str, body: str) -> str:
    """Subject + body for type/intent detection, with signatures and quotes stripped."""
    body_clean = body or ""
    lower = body_clean.lower()
    for marker in (
        "see amazing founders who sold",
        "the turn - available on all podcast",
        "available on all podcast stations",
        "let's win!",
        "let's win,",
        "\n\n-----",
        "\n\n>",
        "[prior messages in this email chain]",
    ):
        idx = lower.find(marker)
        if idx > 0:
            body_clean = body_clean[:idx]
            lower = body_clean.lower()
    return f"{subject}\n{body_clean}".lower()


def _resolve_type_key(intent: str | None, subject: str, body: str) -> str:
    """Map triage intent + email text to canonical rules.MEETING_TYPES key."""
    combined = _scheduling_text(subject, body)
    subject_lower = (subject or "").lower()
    intent_key = (intent or "unknown").lower().replace(" ", "_")

    # Meal/coffee cues outrank podcast text cues: "coffee before the recording
    # session?" is a coffee, and misreading it as podcast loses the venue, the
    # 90-min reserve, and the post-coffee buffer. An explicit podcast triage
    # intent still wins outright.
    if intent_key == "podcast":
        return "podcast"
    if intent_key in {"happy_hour"} or re.search(r"happy\s*hour", combined):
        return "happy_hour"
    if intent_key in {"coffee"} or (
        "coffee" in combined and "happy hour" not in combined
    ):
        return "coffee"
    if intent_key in {"dinner", "dinner_request"} or (
        "dinner" in combined and "happy hour" not in combined
    ):
        return "dinner"
    if intent_key in {"lunch", "lunch_request"} or "lunch" in combined:
        return "lunch"
    if any(cue in combined for cue in PODCAST_CUES):
        return "podcast"

    if "intro" in subject_lower or any(cue in subject_lower for cue in INTRO_CUES):
        return "referral_or_intro"

    if intent_key in {"pitch", "new_client", "board_meeting"}:
        if any(cue in combined for cue in NEW_CLIENT_CUES):
            return "new_client"
        explicit = _parse_duration(combined)
        if explicit and explicit >= 60:
            return "new_client"
        if any(cue in combined for cue in INTRO_CUES):
            return "referral_or_intro"
        if explicit == 30:
            return "referral_or_intro"
        return "referral_or_intro"

    if intent_key in INTENT_ALIASES:
        return INTENT_ALIASES[intent_key]

    if intent_key in kory_rules.MEETING_TYPES:
        return intent_key

    if any(cue in combined for cue in INTRO_CUES):
        return "referral_or_intro"
    if any(cue in combined for cue in NEW_CLIENT_CUES):
        return "new_client"
    if re.search(r"\b(teams|zoom|virtual)\b", combined) and re.search(
        r"\b30\b", combined
    ):
        return "referral_or_intro"

    return "referral_or_intro"


def resolve_meeting_type(
    *,
    intent: str | None = None,
    subject: str = "",
    body: str = "",
) -> MeetingTypeSpec:
    """Resolve meeting type from triage intent and email content."""
    type_key = _resolve_type_key(intent, subject, body)
    explicit = _parse_duration(_combined_text(subject, body))
    spec = _spec_from_type_key(type_key, triage_intent=(intent or ""))

    if not explicit:
        return spec

    duration = explicit
    block = explicit
    meeting_duration = spec.duration_minutes

    if type_key == "coffee":
        block = max(int(_type_config("coffee").get("calendar_block_minutes") or 90), duration + 30)
        meeting_duration = int(_type_config("coffee").get("duration_minutes") or 60)
    elif type_key == "referral_or_intro":
        # Honor the sender's stated length ("45 minutes" stays 45 — Kory's
        # rule is "go with whatever they noted", not round-down-to-30).
        meeting_duration = duration
        block = meeting_duration
    elif type_key == "new_client":
        meeting_duration = max(duration, 60) if duration >= 45 else duration
        block = meeting_duration
    else:
        meeting_duration = duration
        block = duration

    return MeetingTypeSpec(
        type_key=spec.type_key,
        label=spec.label,
        duration_minutes=meeting_duration,
        calendar_block_minutes=block,
        default_format=spec.default_format,
        preferred_times=spec.preferred_times,
        triage_intent=spec.triage_intent,
    )


def infer_duration_from_context(
    *,
    intent: str | None = None,
    subject: str = "",
    body: str = "",
    plan_duration_minutes: int | None = None,
) -> int:
    """Meeting duration for slot end time (not always calendar block)."""
    if plan_duration_minutes and plan_duration_minutes > 0:
        explicit = _parse_duration(_combined_text(subject, body))
        if not explicit:
            return plan_duration_minutes
    return resolve_meeting_type(intent=intent, subject=subject, body=body).duration_minutes


def calendar_block_minutes_for_context(
    *,
    intent: str | None = None,
    subject: str = "",
    body: str = "",
    plan_duration_minutes: int | None = None,
) -> int:
    """Scheduling reserve for conflict checks (e.g. coffee = 90 incl. post-buffer)."""
    spec = resolve_meeting_type(intent=intent, subject=subject, body=body)
    if plan_duration_minutes and plan_duration_minutes > 0:
        explicit = _parse_duration(_combined_text(subject, body))
        if not explicit and spec.type_key not in {"coffee", "happy_hour", "dinner"}:
            return plan_duration_minutes
    return spec.calendar_block_minutes


def offer_duration_minutes_for_context(
    *,
    intent: str | None = None,
    subject: str = "",
    body: str = "",
    plan_duration_minutes: int | None = None,
) -> int:
    """Duration for offered slots, holds, and confirmed invites (coffee = 60)."""
    spec = resolve_meeting_type(intent=intent, subject=subject, body=body)
    if plan_duration_minutes and plan_duration_minutes > 0:
        explicit = _parse_duration(_combined_text(subject, body))
        if not explicit and spec.type_key not in {"coffee", "happy_hour", "dinner"}:
            return plan_duration_minutes
    return spec.duration_minutes


def email_requests_scheduling(subject: str, body: str) -> bool:
    """True when the email clearly asks to book a meeting (even if LLM said non_scheduling)."""
    combined = _scheduling_text(subject, body)
    if any(pattern.search(combined) for pattern in _SCHEDULING_SIGNAL_PATTERNS):
        return True
    inferred = infer_triage_intent_from_text(subject, body)
    return inferred not in {"non_scheduling", "unknown"}


def effective_scheduling_intent(
    intent: str | None,
    *,
    subject: str = "",
    body: str = "",
) -> str:
    """Best intent for scheduler routing — upgrades mis-triaged non_scheduling."""
    key = (intent or "unknown").strip().lower().replace(" ", "_")
    if key not in {"non_scheduling", "unknown", "cancellation", "delegation"}:
        return key
    return infer_triage_intent_from_text(subject, body)


def should_run_scheduler(
    *,
    intent: str | None,
    subject: str = "",
    body: str = "",
) -> bool:
    """Whether to propose calendar slots (not a general LLM reply)."""
    effective = effective_scheduling_intent(intent, subject=subject, body=body)
    if effective in {"non_scheduling", "unknown", "cancellation"}:
        return email_requests_scheduling(subject, body)
    return True


def infer_triage_intent_from_text(subject: str, body: str) -> str:
    """Keyword heuristic when LLM triage is unavailable."""
    combined = _scheduling_text(subject, body)
    subject_lower = (subject or "").lower()
    if "intro" in subject_lower or any(cue in subject_lower for cue in INTRO_CUES):
        return "referral_or_intro"
    if any(w in combined for w in ("unsubscribe", "newsletter", "no longer wish", "daily digest")):
        return "non_scheduling"
    if any(cue in combined for cue in PODCAST_CUES):
        return "podcast"
    if "dinner" in combined and "happy hour" not in combined:
        return "dinner_request"
    if "lunch" in combined:
        return "lunch_request"
    if re.search(r"happy\s*hour", combined) or "drinks" in combined:
        return "happy_hour"
    if "coffee" in combined:
        return "coffee"
    if any(cue in combined for cue in NEW_CLIENT_CUES):
        return "pitch"
    if any(cue in combined for cue in INTRO_CUES):
        return "referral_or_intro"
    if any(
        w in combined
        for w in ("meet", "meeting", "sync", "call", "schedule", "availability", "calendar")
    ):
        return "meeting_request"
    return "unknown"


def normalize_scheduling_intent(
    intent: str | None,
    *,
    subject: str = "",
    body: str = "",
) -> str:
    """Canonical type key for slot_engine and validators."""
    return resolve_meeting_type(intent=intent, subject=subject, body=body).type_key
