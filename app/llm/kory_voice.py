"""Kory email voice profile from sent mail + rules."""

from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any

from app.config import settings

import rules as kory_rules

logger = logging.getLogger(__name__)

PROFILE_PATH = settings.lexi_database_path.parent / "kory_voice_profile.json"
_CACHE_TTL_SECONDS = 6 * 60 * 60
# A fetch that came back empty is almost always Graph throttling the mailbox.
# Retrying on the next compose is what turned one throttled call into a storm,
# so wait before asking again.
_RETRY_AFTER_FAILURE_SECONDS = 30 * 60


def _read_profile() -> dict[str, Any]:
    if not PROFILE_PATH.exists():
        return {}
    try:
        loaded = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write_profile(profile: dict[str, Any]) -> None:
    PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROFILE_PATH.write_text(json.dumps(profile, indent=2), encoding="utf-8")


def load_voice_profile(*, force_refresh: bool = False) -> dict[str, Any]:
    """Load cached voice profile or rebuild from sent mail."""
    if not force_refresh:
        cached = _read_profile()
        if cached:
            age = time.time() - float(cached.get("built_at_epoch") or 0)
            if age < _CACHE_TTL_SECONDS and (cached.get("sample_count") or 0) > 0:
                return cached
            # Stale, or empty because the last fetch failed. Either way, don't
            # refetch until the backoff expires — an empty profile can never
            # satisfy the check above, so without this every single compose
            # re-hit a mailbox that was already rejecting us.
            since_attempt = time.time() - float(cached.get("last_attempt_epoch") or 0)
            if since_attempt < _RETRY_AFTER_FAILURE_SECONDS:
                return cached
    return rebuild_voice_profile()


def rebuild_voice_profile(*, top: int = 30) -> dict[str, Any]:
    """Analyze sent mail and persist tone hints.

    A failed fetch must not overwrite a good profile. Losing 29 real samples to
    one throttled call both degraded the drafting voice silently and disabled
    caching, so keep the last good profile and record only the attempt.
    """
    from app.integrations.outlook_sent import fetch_sent_samples

    try:
        samples = fetch_sent_samples(top=top)
    except Exception as exc:
        logger.warning("Could not fetch sent mail for voice profile: %s", exc)
        samples = []

    now = time.time()
    if not samples:
        previous = _read_profile()
        if (previous.get("sample_count") or 0) > 0:
            previous["last_attempt_epoch"] = now
            _write_profile(previous)
            logger.info(
                "Voice profile refresh returned no samples; keeping %s cached sample(s).",
                previous.get("sample_count"),
            )
            return previous

    profile = _analyze_samples(samples)
    profile["built_at_epoch"] = now
    profile["last_attempt_epoch"] = now
    profile["sample_count"] = len(samples)
    _write_profile(profile)
    return profile


def voice_prompt_block(*, recipient_email: str | None = None) -> str:
    """System-prompt section describing Kory's writing style."""
    profile = load_voice_profile()
    sign_off = kory_rules.EMAIL_RULES.get("sign_off", "Let's Win")
    lines = [
        "KORY EMAIL VOICE (from sent-mail analysis + rules):",
        f"- Sign-off: {sign_off}, then Kory on its own line.",
        "- Tone: direct, warm, concise — no fluff or corporate filler.",
        "- Format: blank line between paragraphs; bullet times when offering slots.",
        "- Never mention YPO in outgoing drafts unless the inbound thread requires it.",
    ]
    for hint in profile.get("tone_hints") or []:
        lines.append(f"- {hint}")

    examples = profile.get("example_snippets") or []
    if recipient_email:
        contact_examples = _contact_examples(profile, recipient_email)
        if contact_examples:
            examples = contact_examples + examples

    if examples:
        lines.append("")
        lines.append("Examples of how Kory actually writes (match length and cadence):")
        for idx, snippet in enumerate(examples[:3], start=1):
            lines.append(f"Example {idx}:\n{snippet}")

    return "\n".join(lines)


def _contact_examples(profile: dict[str, Any], recipient_email: str) -> list[str]:
    from app.integrations.outlook_sent import fetch_sent_to_recipient

    try:
        samples = fetch_sent_to_recipient(recipient_email, top=2)
    except Exception:
        return []
    return [s.get("body", "").strip() for s in samples if s.get("body")]


def _analyze_samples(samples: list[dict[str, str]]) -> dict[str, Any]:
    bodies = [s.get("body", "") for s in samples if s.get("body")]
    closings = Counter()
    greeting_styles: list[str] = []
    avg_len = 0

    for body in bodies:
        avg_len += len(body)
        first_line = body.split("\n", 1)[0].strip()
        if first_line.lower().startswith(("hi ", "hey ", "hello ")):
            greeting_styles.append(first_line[:40])
        for match in re.finditer(
            r"(Let's Win,?|Thanks,?|Best,?|Cheers,?)\s*\n\s*Kory\s*$",
            body,
            flags=re.IGNORECASE | re.MULTILINE,
        ):
            closings[match.group(1).strip()] += 1

    tone_hints: list[str] = []
    if closings:
        top_close = closings.most_common(1)[0][0]
        tone_hints.append(f"Preferred closing observed in sent mail: \"{top_close},\" then Kory.")
    if bodies:
        tone_hints.append(
            f"Typical reply length: ~{max(80, avg_len // max(len(bodies), 1))} characters."
        )
    if greeting_styles:
        tone_hints.append(f"Common greeting: \"{greeting_styles[0]}\".")

    snippets = [_snippet(body) for body in bodies[:5] if body]
    return {
        "tone_hints": tone_hints,
        "example_snippets": snippets,
        "greeting_samples": greeting_styles[:5],
        "closing_counts": dict(closings),
    }


def _snippet(body: str, *, max_chars: int = 450) -> str:
    text = re.sub(r"\n{3,}", "\n\n", body.strip())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"
