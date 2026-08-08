"""Kory can remove a remembered rule from chat — delete_fact.

Before this, kory_memory had upsert+list only: removing a stale rule meant
SQL on the box (the Tuesday-8:30 test-fact cleanup).
"""

from __future__ import annotations

from app.storage.kory_memory import delete_fact, list_facts, upsert_fact


def _seed():
    upsert_fact(fact_key="no_friday_afternoons", fact_value="Never book Friday afternoons", source="test")
    upsert_fact(fact_key="podcast_day", fact_value="Podcasts recorded on Fridays only", source="test")


def _cleanup():
    for f in list_facts():
        delete_fact(fact=f["fact_key"])


def test_delete_by_exact_key():
    _seed()
    try:
        out = delete_fact(fact="no_friday_afternoons")
        assert out["ok"] is True
        assert out["deleted"]["fact_key"] == "no_friday_afternoons"
        assert all(f["fact_key"] != "no_friday_afternoons" for f in list_facts())
    finally:
        _cleanup()


def test_delete_by_unique_substring_of_value():
    _seed()
    try:
        out = delete_fact(fact="never book friday")
        assert out["ok"] is True
        assert out["deleted"]["fact_key"] == "no_friday_afternoons"
    finally:
        _cleanup()


def test_ambiguous_match_refuses_and_lists_candidates():
    _seed()
    try:
        out = delete_fact(fact="friday")  # matches both facts
        assert out["ok"] is False
        assert len(out["candidates"]) == 2
        assert len(list_facts()) == 2, "nothing may be deleted on ambiguity"
    finally:
        _cleanup()


def test_no_match_reports_and_shows_stored_facts():
    _seed()
    try:
        out = delete_fact(fact="lunch rule")
        assert out["ok"] is False and "No stored fact" in out["error"]
        assert len(list_facts()) == 2
    finally:
        _cleanup()
