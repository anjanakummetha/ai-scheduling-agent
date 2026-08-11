"""Composio session and tool execution helpers (read Kory / write sandbox)."""

from __future__ import annotations

import logging
import random
import time
from functools import lru_cache
from typing import Any, Callable, Literal

from composio import Composio

from app.config import settings

logger = logging.getLogger(__name__)

_RETRYABLE_TOKENS = ("429", "500", "502", "503", "504", "overloaded", "rate limit",
                     "timeout", "timed out", "temporarily", "connection", "econnreset")
_NON_RETRYABLE_TOKENS = ("400", "401", "403", "404", "invalid", "not found", "unauthorized")


def _is_retryable_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    if any(tok in msg for tok in _NON_RETRYABLE_TOKENS):
        return False
    return any(tok in msg for tok in _RETRYABLE_TOKENS)


def _execute_with_retry(fn: Callable[[], Any], *, is_write: bool, tool_slug: str) -> Any:
    """Run fn with backoff. Reads retry transient errors; writes never auto-retry
    (a retried write could double-send/double-book)."""
    max_attempts = 1 if is_write else 3
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            if attempt >= max_attempts or not _is_retryable_error(exc):
                raise
            delay = min(8.0, 0.5 * (2 ** (attempt - 1))) + random.uniform(0, 0.4)
            logger.warning(
                "Composio %s attempt %d/%d failed (%s); retrying in %.1fs",
                tool_slug, attempt, max_attempts, str(exc)[:120], delay,
            )
            time.sleep(delay)


class ComposioNotConfiguredError(RuntimeError):
    """Raised when Composio credentials are missing."""


ConnectionRole = Literal["read", "write", "asana", "lexi", "hubspot"]


def _require_api_key() -> str:
    if not settings.composio_api_key:
        raise ComposioNotConfiguredError("COMPOSIO_API_KEY is missing.")
    return settings.composio_api_key


@lru_cache
def get_composio() -> Composio:
    # A per-request timeout so a stuck Composio call can't hang the worker
    # indefinitely (a write with no timeout previously hung for minutes).
    # max_retries=0 keeps our own _execute_with_retry the sole retry authority —
    # the SDK must never retry a write (that could double-send/double-book).
    return Composio(
        api_key=_require_api_key(),
        timeout=settings.composio_timeout_seconds,
        max_retries=0,
    )


def _account_entity_id(connection_id: str) -> str:
    account = get_composio().connected_accounts.get(connection_id)
    user_id = getattr(account, "user_id", None)
    if not user_id and hasattr(account, "model_dump"):
        user_id = account.model_dump().get("user_id")
    if not user_id:
        raise ComposioNotConfiguredError(
            f"Could not resolve Composio user_id for connection {connection_id}."
        )
    return str(user_id)


def resolve_connection(role: ConnectionRole) -> tuple[str, str]:
    """Return (connected_account_id, entity_id) for read, write, lexi, or Asana."""
    if role == "lexi":
        connection_id = settings.lexi_composio_connection_id
        if not connection_id:
            raise ComposioNotConfiguredError("LEXI_COMPOSIO_CONNECTION_ID is missing.")
        entity_id = _account_entity_id(connection_id)
        return connection_id, entity_id

    if role == "read":
        connection_id = settings.kory_composio_connection_id
        if not connection_id:
            raise ComposioNotConfiguredError("KORY_COMPOSIO_CONNECTION_ID is missing.")
        entity_id = settings.composio_entity_id or _account_entity_id(connection_id)
        return connection_id, entity_id

    if role == "asana":
        connection_id = settings.asana_composio_connection_id
        if not connection_id:
            raise ComposioNotConfiguredError("ASANA_COMPOSIO_CONNECTION_ID is missing.")
        entity_id = settings.composio_entity_id or _account_entity_id(connection_id)
        return connection_id, entity_id

    if role == "hubspot":
        connection_id = settings.hubspot_composio_connection_id
        if not connection_id:
            raise ComposioNotConfiguredError("HUBSPOT_COMPOSIO_CONNECTION_ID is missing.")
        entity_id = settings.composio_entity_id or _account_entity_id(connection_id)
        return connection_id, entity_id

    # write
    if settings.lexi_write_mode == "kory":
        connection_id = settings.kory_composio_connection_id
        if not connection_id:
            raise ComposioNotConfiguredError("KORY_COMPOSIO_CONNECTION_ID is missing.")
        entity_id = settings.composio_entity_id or _account_entity_id(connection_id)
        return connection_id, entity_id

    connection_id = settings.sandbox_composio_connection_id
    if not connection_id:
        raise ComposioNotConfiguredError("SANDBOX_COMPOSIO_CONNECTION_ID is missing.")
    entity_id = settings.sandbox_composio_entity_id or _account_entity_id(connection_id)
    return connection_id, entity_id


def require_composio_connection_id() -> str:
    """Backward-compatible: return write connection id."""
    return resolve_connection("write")[0]


def get_composio_entity_id() -> str:
    """Backward-compatible: return write entity id."""
    return resolve_connection("write")[1]


def require_asana_connection_id() -> str:
    return resolve_connection("asana")[0]


def _is_write_tool(tool_slug: str) -> bool:
    from app.safety.kory_read_only import is_outlook_write_slug

    slug = tool_slug.upper()
    if slug.startswith("ASANA_") and any(
        token in slug
        for token in (
            "CREATE",
            "ADD",
            "UPDATE",
            "DELETE",
            "REMOVE",
            "SET_",
            "COMMENT",
        )
    ):
        # Keep GET/SEARCH/LIST as reads.
        if not any(token in slug for token in ("GET", "SEARCH", "LIST", "FIND", "FETCH")):
            return True
    if slug.startswith("HUBSPOT_") and any(
        token in slug
        for token in (
            "CREATE",
            "UPDATE",
            "DELETE",
            "MERGE",
            "ARCHIVE",
            "BATCH",
        )
    ):
        if not any(token in slug for token in ("GET", "SEARCH", "LIST", "FIND", "FETCH", "READ")):
            return True
    return is_outlook_write_slug(slug)


def _is_outlook_read_tool(tool_slug: str) -> bool:
    slug = tool_slug.upper()
    if slug.startswith("ASANA_"):
        return False
    if slug.startswith("HUBSPOT_"):
        return not _is_write_tool(tool_slug)
    if _is_write_tool(tool_slug):
        return False
    return slug.startswith("OUTLOOK_") or slug.startswith("MICROSOFT_OUTLOOK_")


def _is_outlook_outbound_tool(tool_slug: str) -> bool:
    slug = tool_slug.upper()
    return slug.startswith(("OUTLOOK_SEND", "OUTLOOK_CREATE", "MICROSOFT_OUTLOOK_SEND"))


def _kory_outbound_email_blocked(tool_slug: str, role: ConnectionRole) -> bool:
    if not settings.lexi_kory_outbound_blocked:
        return False
    if role != "write" or settings.lexi_write_mode != "kory":
        return False
    return _is_outlook_outbound_tool(tool_slug)


def execute_tool(
    tool_slug: str,
    arguments: dict[str, Any],
    *,
    role: ConnectionRole | None = None,
) -> dict[str, Any]:
    """Execute a Composio tool with read/write/asana routing."""
    if role is None:
        if tool_slug.upper().startswith("ASANA_"):
            role = "asana"
        elif tool_slug.upper().startswith("HUBSPOT_"):
            role = "hubspot"
        elif _is_outlook_read_tool(tool_slug):
            role = "read"
        else:
            role = "write"

    if _kory_outbound_email_blocked(tool_slug, role):
        raise PermissionError(
            "Kory outbound email is DISABLED (LEXI_KORY_OUTBOUND_BLOCKED=true). "
            "No sends or draft creation until explicitly re-enabled."
        )

    connection_id, entity_id = resolve_connection(role)
    from app.safety.kory_read_only import assert_kory_space_write_allowed

    assert_kory_space_write_allowed(tool_slug=tool_slug, connection_id=connection_id)

    slug_u = tool_slug.upper()
    asana_write_blocked = (
        slug_u.startswith("ASANA_")
        and _is_write_tool(tool_slug)
        and (settings.lexi_dry_run or not settings.asana_live_writes_enabled)
    )
    hubspot_write_blocked = (
        slug_u.startswith("HUBSPOT_")
        and _is_write_tool(tool_slug)
        and (settings.lexi_dry_run or not settings.hubspot_live_writes_enabled)
    )
    if (
        (settings.lexi_dry_run and _is_write_tool(tool_slug))
        or asana_write_blocked
        or hubspot_write_blocked
    ):
        reason = "dry_run"
        if asana_write_blocked and not settings.asana_live_writes_enabled:
            reason = "asana_live_writes_disabled"
        if hubspot_write_blocked and not settings.hubspot_live_writes_enabled:
            reason = "hubspot_live_writes_disabled"
        preview = {
            "tool": tool_slug,
            "arguments": arguments,
            "dry_run": True,
            "role": role,
            "blocked_reason": reason,
        }
        print(
            f"\n[Lexi WRITE BLOCKED] {tool_slug} (role={role}, reason={reason})\n"
            f"  args: {arguments}\n",
            flush=True,
        )
        return {"data": preview, "log_id": "dry-run-no-log", "dry_run": True}

    # Structural guard on REAL execution: outside production, never reach a recipient
    # off the allowlist (independent of the dry-run / write-mode flags above).
    from app.safety.recipient_allowlist import assert_recipients_allowed

    assert_recipients_allowed(tool_slug, arguments)

    # Budget tracking — count every real Composio call (plan Phase 3).
    from app.storage.composio_call_log import record_composio_call

    record_composio_call()

    response = _execute_with_retry(
        lambda: get_composio().tools.execute(
            tool_slug,
            arguments=arguments,
            connected_account_id=connection_id,
            user_id=entity_id,
            dangerously_skip_version_check=True,
        ),
        is_write=_is_write_tool(tool_slug),
        tool_slug=tool_slug,
    )
    if isinstance(response, dict):
        data = response.get("data")
        error = response.get("error")
        log_id = response.get("log_id")
        successful = response.get("successful")
    else:
        data = getattr(response, "data", None)
        error = getattr(response, "error", None)
        log_id = getattr(response, "log_id", None)
        successful = getattr(response, "successful", None)

    if error:
        raise RuntimeError(f"{tool_slug} failed: {error}")

    # Composio can answer 200 with successful=false and no `error` — the request
    # was well-formed but the vendor refused it. Dropping this flag here is why
    # an Asana "move" into a section that never received the task still reported
    # success all the way up to Kory. Callers cannot check what they never see.
    return {
        "data": data,
        "successful": successful,
        "log_id": log_id,
        "connected_account_id": connection_id,
        "entity_id": entity_id,
        "role": role,
    }


def execute_read_tool(tool_slug: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return execute_tool(tool_slug, arguments, role="read")


def execute_write_tool(tool_slug: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return execute_tool(tool_slug, arguments, role="write")


def execute_asana_tool(tool_slug: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return execute_tool(tool_slug, arguments, role="asana")


def execute_hubspot_tool(tool_slug: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return execute_tool(tool_slug, arguments, role="hubspot")


def resolve_search_user_id() -> str:
    """Entity/user id for Composio Search (no connected account required)."""
    if settings.composio_entity_id:
        return settings.composio_entity_id
    kory_id = settings.kory_composio_connection_id
    if kory_id:
        return _account_entity_id(kory_id)
    return "lexi-default"


def execute_search_tool(tool_slug: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Execute Composio Search toolkit tools (read-only; no Outlook connection)."""
    _require_api_key()
    slug = tool_slug.upper()
    if not slug.startswith("COMPOSIO_SEARCH_"):
        raise ValueError(f"Not a Composio Search slug: {tool_slug}")

    user_id = resolve_search_user_id()
    response = get_composio().tools.execute(
        slug,
        arguments=arguments,
        user_id=user_id,
        dangerously_skip_version_check=True,
    )
    if isinstance(response, dict):
        data = response.get("data")
        error = response.get("error")
        log_id = response.get("log_id")
    else:
        data = getattr(response, "data", None)
        error = getattr(response, "error", None)
        log_id = getattr(response, "log_id", None)

    if error:
        raise RuntimeError(f"{slug} failed: {error}")

    return {
        "data": data,
        "log_id": log_id,
        "user_id": user_id,
        "role": "search",
    }
