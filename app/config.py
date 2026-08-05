"""Application configuration loaded from environment variables."""

from dataclasses import dataclass, field
import os
from pathlib import Path

from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parents[1]


def _resolve_env_file() -> Path:
    """Which env file to load. LEXI_ENV_FILE wins; else LEXI_ENV picks
    .env.<env> when it exists; else the legacy .env. Defaults to .env for
    backward compatibility."""
    explicit = os.getenv("LEXI_ENV_FILE", "").strip()
    if explicit:
        return Path(explicit) if Path(explicit).is_absolute() else ROOT_DIR / explicit
    env = os.getenv("LEXI_ENV", "").strip().lower()
    if env in {"testing", "production"}:
        candidate = ROOT_DIR / f".env.{env}"
        if candidate.exists():
            return candidate
    return ROOT_DIR / ".env"


_ENV_FILE = _resolve_env_file()
load_dotenv(_ENV_FILE)

_HERMES_ENV = Path.home() / ".hermes" / ".env"
if _HERMES_ENV.exists():
    load_dotenv(_HERMES_ENV, override=False)

ANTHROPIC_OPENAI_BASE_URL = "https://api.anthropic.com/v1"
ASANA_BOARD_NAME = "Reservation Reminders"
ASANA_PARENT_PROJECT_NAME = "Kory NON-IFG"


def resolve_lexi_database_path() -> Path:
    """Resolve DB path from LEXI_DATABASE_PATH or default data/lexi.db under project root."""
    raw = os.getenv("LEXI_DATABASE_PATH", "").strip()
    if not raw:
        return ROOT_DIR / "data" / "lexi.db"
    path = Path(raw)
    return path if path.is_absolute() else ROOT_DIR / path


def resolve_llm_base_url() -> str:
    explicit = os.getenv("LLM_BASE_URL", "").strip()
    if explicit:
        return explicit
    return ANTHROPIC_OPENAI_BASE_URL


def resolve_llm_api_key() -> str:
    return (os.getenv("LLM_API_KEY") or os.getenv("ANTHROPIC_API_KEY") or "").strip()


def resolve_llm_model() -> str:
    """Default drafting/judgment model. Sonnet-class: near-Opus quality at lower cost."""
    return (
        os.getenv("LLM_MODEL", "").strip()
        or os.getenv("LEXI_LLM_MODEL", "").strip()
        or "claude-sonnet-5"
    )


def resolve_llm_model_for_role(role: str) -> str:
    """Cost-tiered model per role (plan Phase 3). Falls back to the default model.

    - triage / classification  → cheap, high-volume  (LEXI_LLM_MODEL_TRIAGE)
    - scheduler / planning      →                     (LEXI_LLM_MODEL_SCHEDULER)
    - drafting / compose (default) → quality, human-visible (LEXI_LLM_MODEL_DRAFTING)
    """
    env_key = {
        "triage": "LEXI_LLM_MODEL_TRIAGE",
        "scheduler": "LEXI_LLM_MODEL_SCHEDULER",
        "drafting": "LEXI_LLM_MODEL_DRAFTING",
    }.get(role)
    if env_key:
        explicit = os.getenv(env_key, "").strip()
        if explicit:
            return explicit
    return resolve_llm_model()


def resolve_llm_max_tokens_for_role(role: str) -> int:
    """Per-role output cap (plan Phase 3) — currently unbounded on every call."""
    defaults = {"triage": 800, "scheduler": 2000, "drafting": 1200}
    env_key = {
        "triage": "LEXI_LLM_MAX_TOKENS_TRIAGE",
        "scheduler": "LEXI_LLM_MAX_TOKENS_SCHEDULER",
        "drafting": "LEXI_LLM_MAX_TOKENS_DRAFTING",
    }.get(role)
    if env_key:
        raw = os.getenv(env_key, "").strip()
        if raw.isdigit():
            return int(raw)
    return defaults.get(role, 1536)


def resolve_kory_sender_emails() -> tuple[str, ...]:
    raw = os.getenv("KORY_SENDER_EMAILS", "").strip()
    if not raw:
        return ()
    return tuple(email.strip().lower() for email in raw.split(",") if email.strip())


def resolve_kory_cc_email() -> str:
    """Kory's real primary address — the single address CC'd on Lexi-sent mail."""
    return os.getenv("KORY_CC_EMAIL", "Kory.Mitchell@iconicfounders.com").strip().lower()


def resolve_cc_kory_enabled() -> bool:
    """Whether Lexi CCs Kory on its outbound mail (off for tests, on in production)."""
    return os.getenv("LEXI_CC_KORY_ENABLED", "true").strip().lower() in {"1", "true", "yes"}


def resolve_hubspot_bcc_address() -> str:
    """HubSpot logging BCC address for outbound scheduling mail to outsiders."""
    return os.getenv("LEXI_HUBSPOT_BCC_ADDRESS", "").strip().lower()


def resolve_hubspot_bcc_enabled() -> bool:
    """Whether to BCC the HubSpot logging address (production only)."""
    return os.getenv("LEXI_HUBSPOT_BCC_ENABLED", "false").strip().lower() in {"1", "true", "yes"}


def resolve_lexi_write_mode() -> str:
    mode = os.getenv("LEXI_WRITE_MODE", "sandbox").strip().lower()
    return mode if mode in {"sandbox", "kory"} else "sandbox"


def resolve_composio_search_enabled() -> bool:
    return os.getenv("LEXI_COMPOSIO_SEARCH_ENABLED", "true").lower() in {"1", "true", "yes"}


def resolve_composio_timeout_seconds() -> float:
    """Per-request timeout for Composio calls so a stuck call can't hang the worker."""
    try:
        return float(os.getenv("LEXI_COMPOSIO_TIMEOUT_SECONDS", "30").strip() or "30")
    except ValueError:
        return 30.0


def resolve_teams_inbound_notify_mode() -> str:
    mode = os.getenv("LEXI_TEAMS_INBOUND_NOTIFY_MODE", "delegation_and_followups").strip().lower()
    if mode in {"delegation_only", "delegation_and_followups", "important", "all"}:
        return mode
    return "delegation_and_followups"


def resolve_default_send_channel() -> str:
    ch = os.getenv("LEXI_DEFAULT_SEND_CHANNEL", "kory").strip().lower()
    return ch if ch in {"kory", "lexi"} else "kory"


def resolve_lexi_calendar_search_days() -> int:
    try:
        return max(14, int(os.getenv("LEXI_CALENDAR_SEARCH_DAYS", "60")))
    except ValueError:
        return 60


def resolve_lexi_calendar_search_days_max() -> int:
    try:
        return max(30, int(os.getenv("LEXI_CALENDAR_SEARCH_DAYS_MAX", "120")))
    except ValueError:
        return 120


@dataclass(frozen=True)
class Settings:
    app_name: str = "Lexi Scheduling Agent"
    lexi_database_path: Path = field(default_factory=resolve_lexi_database_path)
    rules_dir: Path = ROOT_DIR / "app" / "rules"
    llm_base_url: str = field(default_factory=resolve_llm_base_url)
    llm_api_key: str = field(default_factory=resolve_llm_api_key)
    llm_model: str = field(default_factory=resolve_llm_model)
    composio_api_key: str | None = os.getenv("COMPOSIO_API_KEY")
    # Read path: Kory Outlook
    kory_composio_connection_id: str | None = (
        os.getenv("KORY_COMPOSIO_CONNECTION_ID", "").strip() or None
    )
    composio_entity_id: str | None = os.getenv("COMPOSIO_ENTITY_ID", "").strip() or None
    # Write path: sandbox (pilot) or Kory when LEXI_WRITE_MODE=kory
    lexi_write_mode: str = field(default_factory=resolve_lexi_write_mode)
    sandbox_composio_connection_id: str | None = (
        os.getenv("SANDBOX_COMPOSIO_CONNECTION_ID", "").strip() or None
    )
    sandbox_composio_entity_id: str | None = (
        os.getenv("SANDBOX_COMPOSIO_ENTITY_ID", "").strip() or None
    )
    sandbox_mailbox_email: str | None = os.getenv("SANDBOX_MAILBOX_EMAIL", "").strip() or None
    sandbox_email_loopback: bool = os.getenv("SANDBOX_EMAIL_LOOPBACK", "true").lower() in {
        "1",
        "true",
        "yes",
    }
    demo_mode: bool = os.getenv("DEMO_MODE", "false").lower() in {"1", "true", "yes"}
    lexi_dry_run: bool = os.getenv("LEXI_DRY_RUN", "true").lower() in {"1", "true", "yes"}
    # When true, blocks all Kory mailbox outbound (sends + draft creation) regardless of approval.
    lexi_kory_outbound_blocked: bool = os.getenv(
        "LEXI_KORY_OUTBOUND_BLOCKED", "true"
    ).lower() in {"1", "true", "yes"}
    # Blocks ALL Composio writes against Kory's connected account (calendar + mail).
    lexi_kory_space_read_only: bool = os.getenv(
        "LEXI_KORY_SPACE_READ_ONLY", "true"
    ).lower() in {"1", "true", "yes"}
    lexi_composio_connection_id: str | None = (
        os.getenv("LEXI_COMPOSIO_CONNECTION_ID", "").strip() or None
    )
    lexi_mailbox_email: str | None = os.getenv("LEXI_MAILBOX_EMAIL", "").strip() or None
    lexi_cc_emails: str | None = os.getenv("LEXI_CC_EMAILS", "").strip() or None
    lexi_default_send_channel: str = field(default_factory=resolve_default_send_channel)
    lexi_delegation_auto_draft: bool = os.getenv(
        "LEXI_DELEGATION_AUTO_DRAFT", "true"
    ).lower() in {"1", "true", "yes"}
    lexi_delegation_cc_only: bool = os.getenv(
        "LEXI_DELEGATION_CC_ONLY", "true"
    ).lower() in {"1", "true", "yes"}
    lexi_teams_inbound_notify_mode: str = field(default_factory=resolve_teams_inbound_notify_mode)
    lexi_composio_search_enabled: bool = field(default_factory=resolve_composio_search_enabled)
    composio_timeout_seconds: float = field(default_factory=resolve_composio_timeout_seconds)
    scheduling_timezone: str = os.getenv("SCHEDULING_TIMEZONE", "America/Denver")
    outlook_timezone: str = os.getenv("OUTLOOK_TIMEZONE", "America/New_York")
    lexi_calendar_search_days: int = field(default_factory=resolve_lexi_calendar_search_days)
    lexi_calendar_search_days_max: int = field(
        default_factory=resolve_lexi_calendar_search_days_max
    )
    asana_project_gid: str | None = os.getenv("ASANA_PROJECT_GID", "").strip() or None
    asana_section_gid: str | None = os.getenv("ASANA_SECTION_GID", "").strip() or None
    asana_composio_connection_id: str | None = (
        os.getenv("ASANA_COMPOSIO_CONNECTION_ID", "").strip() or None
    )
    hubspot_composio_connection_id: str | None = (
        os.getenv("HUBSPOT_COMPOSIO_CONNECTION_ID", "").strip() or None
    )
    # Kory's HubSpot contact-owner id. Reads span the whole portal; writes are
    # scoped to contacts he owns unless he explicitly confirms someone else's.
    hubspot_kory_owner_id: str = os.getenv("HUBSPOT_KORY_OWNER_ID", "159133511").strip()
    # Explicit live-write kill switches for CRM/task systems. Keep false for UAT.
    asana_live_writes_enabled: bool = os.getenv(
        "LEXI_ASANA_LIVE_WRITES_ENABLED", "false"
    ).lower() in {"1", "true", "yes"}
    hubspot_live_writes_enabled: bool = os.getenv(
        "LEXI_HUBSPOT_LIVE_WRITES_ENABLED", "false"
    ).lower() in {"1", "true", "yes"}
    # Outreach campaigns: stage locally by default; never send until enabled.
    outreach_live_sends_enabled: bool = os.getenv(
        "LEXI_OUTREACH_LIVE_SENDS_ENABLED", "false"
    ).lower() in {"1", "true", "yes"}
    outreach_outlook_drafts_enabled: bool = os.getenv(
        "LEXI_OUTREACH_OUTLOOK_DRAFTS_ENABLED", "false"
    ).lower() in {"1", "true", "yes"}
    asana_enabled: bool = os.getenv("ASANA_ENABLED", "false").lower() in {"1", "true", "yes"}
    lexi_teams_enabled: bool = os.getenv("LEXI_TEAMS_ENABLED", "false").lower() in {
        "1",
        "true",
        "yes",
    }
    lexi_teams_text_only: bool = os.getenv("LEXI_TEAMS_TEXT_ONLY", "true").lower() in {
        "1",
        "true",
        "yes",
    }
    lexi_suppress_teams_push: bool = os.getenv(
        "LEXI_SUPPRESS_TEAMS_PUSH", "false"
    ).lower() in {"1", "true", "yes"}
    kory_sender_emails: tuple[str, ...] = field(default_factory=resolve_kory_sender_emails)
    kory_cc_email: str = field(default_factory=resolve_kory_cc_email)
    cc_kory_enabled: bool = field(default_factory=resolve_cc_kory_enabled)
    hubspot_bcc_address: str = field(default_factory=resolve_hubspot_bcc_address)
    hubspot_bcc_enabled: bool = field(default_factory=resolve_hubspot_bcc_enabled)


settings = Settings()


# --- Startup safety validation (plan Phase 0) -------------------------------
# python-dotenv silently keeps the LAST value of a duplicated key, so a kill
# switch a human reads at the top of the file may not be the one in effect.
# We fail fast on that, and on env combinations that are unsafe by construction.

class StartupSafetyError(RuntimeError):
    """Raised at import time when the environment is unsafe or self-contradictory."""


def _iter_env_files() -> list[Path]:
    candidates = [_ENV_FILE, _HERMES_ENV]
    return [p for p in candidates if p.exists()]


def find_duplicate_env_keys() -> dict[str, list[int]]:
    """Return {KEY: [1-indexed line numbers]} for keys defined more than once in one env file."""
    dupes: dict[str, list[int]] = {}
    for path in _iter_env_files():
        seen: dict[str, list[int]] = {}
        try:
            lines = path.read_text().splitlines()
        except OSError:
            continue
        for i, raw in enumerate(lines, start=1):
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key = line.split("=", 1)[0].strip()
            if not key or not (key[0].isalpha() or key[0] == "_"):
                continue
            seen.setdefault(key, []).append(i)
        for key, at in seen.items():
            if len(at) > 1:
                dupes[key] = at
    return dupes


def resolve_lexi_env() -> str:
    """Runtime posture. Defaults to 'testing' — production must be requested explicitly."""
    env = os.getenv("LEXI_ENV", "").strip().lower()
    return env if env in {"testing", "production"} else "testing"


def _bool_env(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes"}


def safety_posture_summary() -> dict[str, object]:
    """Effective values of every safety-relevant gate — for the boot banner and status tool."""
    return {
        "LEXI_ENV": resolve_lexi_env(),
        "LEXI_WRITE_MODE": resolve_lexi_write_mode(),
        "LEXI_DRY_RUN": settings.lexi_dry_run,
        "LEXI_KORY_OUTBOUND_BLOCKED": settings.lexi_kory_outbound_blocked,
        "LEXI_KORY_SPACE_READ_ONLY": settings.lexi_kory_space_read_only,
        "LEXI_REQUIRE_KORY_APPROVAL": _bool_env("LEXI_REQUIRE_KORY_APPROVAL", True),
        "LEXI_AUTO_EXECUTE_ENABLED": _bool_env("LEXI_AUTO_EXECUTE_ENABLED", False),
        "LEXI_ALLOW_IMMEDIATE_SEND": _bool_env("LEXI_ALLOW_IMMEDIATE_SEND", False),
        "LEXI_ASANA_LIVE_WRITES_ENABLED": settings.asana_live_writes_enabled,
        "LEXI_HUBSPOT_LIVE_WRITES_ENABLED": settings.hubspot_live_writes_enabled,
        "LEXI_OUTREACH_LIVE_SENDS_ENABLED": settings.outreach_live_sends_enabled,
    }


def validate_startup_safety() -> None:
    """Fail fast on duplicate env keys and incoherent safety combinations."""
    dupes = find_duplicate_env_keys()
    if dupes:
        detail = "; ".join(f"{k} (lines {', '.join(map(str, v))})" for k, v in sorted(dupes.items()))
        raise StartupSafetyError(
            "Duplicate keys in the env file — python-dotenv keeps only the last value, "
            f"so the effective setting may not be the one you see: {detail}. "
            "Remove the duplicates before starting."
        )
    env = resolve_lexi_env()
    if env == "testing" and resolve_lexi_write_mode() == "kory":
        raise StartupSafetyError(
            "LEXI_ENV=testing with LEXI_WRITE_MODE=kory is not allowed — testing must never "
            "write to Kory's real account. Set LEXI_WRITE_MODE=sandbox, or LEXI_ENV=production "
            "only when you intend real writes."
        )


# Run the hazard checks at import so no process can start on a duplicate/incoherent env.
validate_startup_safety()
