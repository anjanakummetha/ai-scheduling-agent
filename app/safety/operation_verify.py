"""Fail-closed verification after Lexi operations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.scheduling.email_format import normalize_draft_for_display


@dataclass
class VerifyResult:
    ok: bool
    checks: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checks": self.checks,
            "errors": self.errors,
            "warnings": self.warnings,
        }


def verify_draft_reply(
    draft: str,
    *,
    voice_mode: str = "kory",
    require_kory_signoff: bool = True,
) -> VerifyResult:
    """Validate draft formatting before showing Kory or sending."""
    from app.scheduling.lexi_voice import normalize_voice_mode

    mode = normalize_voice_mode(voice_mode)
    result = VerifyResult(ok=True)
    text = normalize_draft_for_display(draft, max_chars=None, voice_mode=mode)
    result.checks.append("normalized_line_breaks")

    if not text.strip():
        result.ok = False
        result.errors.append("Draft is empty after normalization.")
        return result

    if "\n\n" not in text and len(text) > 120:
        result.warnings.append(
            "Draft may be dense — consider blank lines between paragraphs."
        )

    if mode == "lexi":
        if not re_search_lexi_signoff(text):
            result.ok = False
            result.errors.append(
                "Lexi draft must end with Thank you, / Lexi Knightly / Executive Assistant / "
                "lexi@iconicfounders.com (one line each)."
            )
        else:
            result.checks.append("lexi_signoff")
        return result

    if require_kory_signoff:
        if not re_search_signoff(text):
            result.ok = False
            result.errors.append(
                'Draft must end with "Let\'s Win," on its own line followed by Kory.'
            )
        else:
            result.checks.append("kory_signoff")

    return result


def verify_operation_result(
    *,
    operation: str,
    success: bool,
    detail: str = "",
    expected: str = "",
) -> VerifyResult:
    """Generic self-check: report failure instead of claiming success."""
    result = VerifyResult(ok=success)
    result.checks.append(operation)
    if success:
        if detail:
            result.checks.append(detail)
        return result
    result.errors.append(expected or f"{operation} failed.")
    if detail:
        result.errors.append(detail)
    return result


def verify_send_ack(
    *,
    message_id: str | None,
    status_code: int | None = None,
) -> VerifyResult:
    """Confirm outbound send returned an id or success status."""
    ok = bool(message_id) or status_code in {200, 201, 202}
    result = VerifyResult(ok=ok)
    result.checks.append("outbound_send_ack")
    if message_id:
        result.checks.append(f"message_id={message_id}")
    if not ok:
        result.errors.append(
            "Send did not return a message id or success status — treat as FAILED, do not tell Kory it sent."
        )
    return result


def merge_verify(base: dict[str, Any], verify: VerifyResult) -> dict[str, Any]:
    """Attach verification block to an action result dict."""
    out = dict(base)
    out["verify"] = verify.to_dict()
    if not verify.ok and base.get("ok"):
        out["ok"] = False
        out["error"] = (
            base.get("error")
            or "; ".join(verify.errors)
            or "Operation verification failed."
        )
    if verify.warnings and not out.get("warnings"):
        out["warnings"] = verify.warnings
    return out


def re_search_signoff(text: str) -> bool:
    import re

    return bool(
        re.search(
            r"Let's Win,?\s*\n\s*Kory\s*$",
            text.strip(),
            flags=re.IGNORECASE,
        )
    )


def re_search_lexi_signoff(text: str) -> bool:
    import re

    return bool(
        re.search(
            r"Thank you,?\s*\n\s*Lexi Knightly\s*\n\s*Executive Assistant\s*\n\s*"
            r"lexi@iconicfounders\.com\s*$",
            text.strip(),
            flags=re.IGNORECASE,
        )
    )
