"""Lexi assistant voice (when replying as Kory's EA, not as Kory)."""

from __future__ import annotations

VOICE_MODE_KORY = "kory"
VOICE_MODE_LEXI = "lexi"

# Standard Lexi outbound sign-off (plain text; one line each).
LEXI_SIGNOFF_LINES: tuple[str, ...] = (
    "Thank you,",
    "Lexi Knightly",
    "Executive Assistant",
    "lexi@iconicfounders.com",
)
LEXI_SIGNOFF_BLOCK = "\n".join(LEXI_SIGNOFF_LINES)


def normalize_voice_mode(value: str | None) -> str:
    mode = (value or VOICE_MODE_KORY).strip().lower()
    return mode if mode in {VOICE_MODE_KORY, VOICE_MODE_LEXI} else VOICE_MODE_KORY


def lexi_assistant_prompt_block() -> str:
    return f"""LEXI ASSISTANT VOICE (when voice_mode=lexi):
- Open with a brief professional intro as Kory's assistant Lexi (one line).
- Coordinate scheduling clearly; do not impersonate Kory.
- Always end with this exact sign-off (one line each, blank line before it):
{LEXI_SIGNOFF_BLOCK}
- Never sign as Kory when voice_mode=lexi."""


def voice_instruction_for_mode(voice_mode: str) -> str:
    if normalize_voice_mode(voice_mode) == VOICE_MODE_LEXI:
        return lexi_assistant_prompt_block()
    return "Use Kory's voice: sign off with Let's Win, then Kory on the next line."
