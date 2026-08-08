"""Composio Search toolkit — web, travel, maps, news (read-only; no Kory mailbox writes)."""

from __future__ import annotations

import json
from typing import Any

from app.config import settings
from app.integrations.composio_client import ComposioNotConfiguredError, execute_search_tool

# All Composio Search slugs (COMPOSIO_SEARCH toolkit v20260424_00).
SEARCH_ALLOW_SLUGS = frozenset(
    {
        "COMPOSIO_SEARCH_AMAZON",
        "COMPOSIO_SEARCH_DUCK_DUCK_GO",
        "COMPOSIO_SEARCH_EVENT",
        "COMPOSIO_SEARCH_EXA_ANSWER",
        "COMPOSIO_SEARCH_EXA_SIMILARLINK",
        "COMPOSIO_SEARCH_FETCH_URL_CONTENT",
        "COMPOSIO_SEARCH_FINANCE",
        "COMPOSIO_SEARCH_FLIGHTS",
        "COMPOSIO_SEARCH_GOOGLE_MAPS",
        "COMPOSIO_SEARCH_GROQ_CHAT",
        "COMPOSIO_SEARCH_HOTELS",
        "COMPOSIO_SEARCH_IMAGE",
        "COMPOSIO_SEARCH_NEWS",
        "COMPOSIO_SEARCH_NPPESNPI_LOOKUP",
        "COMPOSIO_SEARCH_SCHOLAR",
        "COMPOSIO_SEARCH_SEC_FILINGS",
        "COMPOSIO_SEARCH_SHOPPING",
        "COMPOSIO_SEARCH_TAVILY",
        "COMPOSIO_SEARCH_TRENDS",
        "COMPOSIO_SEARCH_TRIP_ADVISOR",
        "COMPOSIO_SEARCH_WALMART",
        "COMPOSIO_SEARCH_WEB",
    }
)

# Curated slugs for daily EA workflows (documented in agent_instructions).
PRIMARY_SEARCH_SLUGS = frozenset(
    {
        "COMPOSIO_SEARCH_WEB",
        "COMPOSIO_SEARCH_TAVILY",
        "COMPOSIO_SEARCH_FLIGHTS",
        "COMPOSIO_SEARCH_HOTELS",
        "COMPOSIO_SEARCH_GOOGLE_MAPS",
        "COMPOSIO_SEARCH_NEWS",
        "COMPOSIO_SEARCH_EVENT",
        "COMPOSIO_SEARCH_FETCH_URL_CONTENT",
        "COMPOSIO_SEARCH_TRIP_ADVISOR",
    }
)


def search_enabled() -> bool:
    if not settings.composio_api_key:
        return False
    return settings.lexi_composio_search_enabled


def execute_search_action(
    slug: str,
    arguments: dict[str, Any],
    *,
    allow_unlisted: bool = False,
) -> dict[str, Any]:
    """Run one Composio Search slug (read-only; uses COMPOSIO_API_KEY + user_id)."""
    # Validate the slug first — a malformed or unlisted slug is rejected regardless
    # of whether the Composio API is configured (pure input check, no I/O).
    normalized = (slug or "").strip().upper()
    if not normalized.startswith("COMPOSIO_SEARCH_"):
        raise ValueError(f"Not a Composio Search slug: {slug}")
    if not allow_unlisted and normalized not in SEARCH_ALLOW_SLUGS:
        raise PermissionError(f"Search slug {normalized} is not on the allowlist.")
    if not search_enabled():
        raise ComposioNotConfiguredError(
            "Composio Search disabled. Set COMPOSIO_API_KEY and LEXI_COMPOSIO_SEARCH_ENABLED=true."
        )
    from app.integrations.person_research import throttle_search_calls

    throttle_search_calls()
    return execute_search_tool(normalized, arguments)


def web_search(query: str) -> dict[str, Any]:
    return execute_search_action("COMPOSIO_SEARCH_WEB", {"query": query.strip()})


def tavily_search(query: str) -> dict[str, Any]:
    return execute_search_action("COMPOSIO_SEARCH_TAVILY", {"query": query.strip()})


def search_flights(arguments: dict[str, Any]) -> dict[str, Any]:
    return execute_search_action("COMPOSIO_SEARCH_FLIGHTS", arguments)


def search_hotels(arguments: dict[str, Any]) -> dict[str, Any]:
    if not arguments.get("q"):
        raise ValueError("Hotel search requires 'q' (location).")
    return execute_search_action("COMPOSIO_SEARCH_HOTELS", arguments)


def search_maps(query: str) -> dict[str, Any]:
    # COMPOSIO_SEARCH_GOOGLE_MAPS takes "q", unlike the other search tools'
    # "query" — sending "query" fails with "missing: {'q'}".
    return execute_search_action("COMPOSIO_SEARCH_GOOGLE_MAPS", {"q": query.strip()})


def search_news(query: str) -> dict[str, Any]:
    return execute_search_action("COMPOSIO_SEARCH_NEWS", {"query": query.strip()})


def search_events(query: str) -> dict[str, Any]:
    return execute_search_action("COMPOSIO_SEARCH_EVENT", {"query": query.strip()})


def fetch_url_content(urls: list[str] | str, *, max_characters: int | None = 8000) -> dict[str, Any]:
    url_list = [urls] if isinstance(urls, str) else list(urls)
    args: dict[str, Any] = {"urls": url_list, "text": True}
    if max_characters:
        args["max_characters"] = max_characters
    return execute_search_action("COMPOSIO_SEARCH_FETCH_URL_CONTENT", args)


def parse_arguments_json(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"arguments_json invalid: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("arguments_json must be a JSON object.")
    return parsed
