"""The SDK re-fetched every tool's schema before every execute (top measured
latency lever). get_composio() now installs a slug-keyed cache over
get_raw_composio_tool_by_slug — one fetch per slug per process."""

from __future__ import annotations

from unittest.mock import MagicMock

from app.integrations.composio_client import _install_schema_cache


def _client_with_counting_fetch():
    client = MagicMock()
    calls = {"n": 0}

    def fetch(slug: str):
        calls["n"] += 1
        return {"slug": slug, "toolkit": "outlook"}

    client.tools.get_raw_composio_tool_by_slug = fetch
    return client, calls


def test_schema_fetched_once_per_slug(monkeypatch):
    monkeypatch.delenv("LEXI_COMPOSIO_SCHEMA_CACHE", raising=False)
    client, calls = _client_with_counting_fetch()
    _install_schema_cache(client)
    fn = client.tools.get_raw_composio_tool_by_slug
    fn("OUTLOOK_LIST_MESSAGES")
    fn("OUTLOOK_LIST_MESSAGES")
    fn("OUTLOOK_LIST_MESSAGES")
    fn("OUTLOOK_GET_MESSAGE")
    assert calls["n"] == 2


def test_escape_hatch_disables_cache(monkeypatch):
    monkeypatch.setenv("LEXI_COMPOSIO_SCHEMA_CACHE", "false")
    client, calls = _client_with_counting_fetch()
    _install_schema_cache(client)
    fn = client.tools.get_raw_composio_tool_by_slug
    fn("OUTLOOK_LIST_MESSAGES")
    fn("OUTLOOK_LIST_MESSAGES")
    assert calls["n"] == 2
