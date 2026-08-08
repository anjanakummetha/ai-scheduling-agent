"""Tests for Composio Search allowlist (no live API)."""

from app.integrations.composio_search import (
    SEARCH_ALLOW_SLUGS,
    execute_search_action,
    parse_arguments_json,
    search_enabled,
)


def test_search_allowlist_includes_web_and_travel():
    assert "COMPOSIO_SEARCH_WEB" in SEARCH_ALLOW_SLUGS
    assert "COMPOSIO_SEARCH_FLIGHTS" in SEARCH_ALLOW_SLUGS
    assert "COMPOSIO_SEARCH_HOTELS" in SEARCH_ALLOW_SLUGS


def test_reject_non_search_slug():
    try:
        execute_search_action("OUTLOOK_SEND_EMAIL", {"query": "x"})
        raise AssertionError("Expected ValueError")
    except ValueError as exc:
        assert "Not a Composio Search" in str(exc)


def test_reject_unlisted_search_slug():
    try:
        execute_search_action("COMPOSIO_SEARCH_FAKE", {"query": "x"})
        raise AssertionError("Expected PermissionError")
    except PermissionError as exc:
        assert "allowlist" in str(exc)


def test_parse_arguments_json():
    assert parse_arguments_json('{"q": "Denver"}') == {"q": "Denver"}


def test_search_enabled_is_bool():
    assert isinstance(search_enabled(), bool)


def test_maps_sends_q_not_query(monkeypatch):
    """COMPOSIO_SEARCH_GOOGLE_MAPS takes "q" unlike the other search tools —
    sending "query" failed live with "missing: {'q'}"."""
    import app.integrations.composio_search as cs

    seen = {}

    def fake_exec(slug, args):
        seen[slug] = args
        return {"data": {}}

    monkeypatch.setattr(cs, "execute_search_action", fake_exec)
    cs.search_maps("coffee shops Cherry Creek Denver")
    assert seen["COMPOSIO_SEARCH_GOOGLE_MAPS"] == {"q": "coffee shops Cherry Creek Denver"}
    cs.search_news("Denver construction")
    assert seen["COMPOSIO_SEARCH_NEWS"] == {"query": "Denver construction"}
