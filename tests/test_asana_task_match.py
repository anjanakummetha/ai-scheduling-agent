"""Matching what Kory calls a task to what the task is actually called.

Search was literal substring containment. "the elevator task" matched nothing —
the trailing word "task" alone was enough to miss "Load Elevator market-study
landing page" — and Lexi reported that as the task not existing. Names below are
real titles from Kory's Asana.
"""

import pytest

from app.integrations.asana_task_match import pick_task, rank_tasks, score_task_name, tokenize

REAL_TASKS = [
    "Load Elevator market-study landing page into Dripify campaign",
    "Open Outreach support ticket re: mobile-app recording upload failure",
    "Check HubSpot field-audit history — why was James Pfeiffer's contact changed",
    "Enroll IFG in Shift coworking, 16-hr/month membership plan at $299",
    "Send Kory the podcast-analytics API key + site name.",
    "Remind me to send Bruce Krinksy a fractional CFO",
    "Elevate Conference: Nat'l Assoc. of Landscape Professionals | Nov.",
    "Speaking Engagements for Kory",
    "Project 2: Daily CEO Dashboard",
    "Notebook lm",
    "tom stevens",
    "Book dinner reservation",
    "Review deck",
]
TASKS = [{"gid": str(i), "name": n} for i, n in enumerate(REAL_TASKS)]


@pytest.mark.parametrize(
    "query,expected",
    [
        ("the elevator task", "Load Elevator"),          # the live failure
        ("elevator", "Load Elevator"),
        ("mark the dripify one complete", "Load Elevator"),
        ("bruce krinsky", "Bruce Krinksy"),              # Asana itself has the typo
        ("podcast analytics key", "podcast-analytics"),
        ("the shift coworking task", "Shift coworking"),
        ("hubspot field audit", "HubSpot field-audit"),
        ("daily ceo dashboard", "Daily CEO Dashboard"),
        ("speaking engagements", "Speaking Engagements"),
        ("notebook lm", "Notebook lm"),
        ("tom", "tom stevens"),
        ("dinner", "Book dinner"),
    ],
)
def test_loose_phrasing_finds_the_right_task(query, expected):
    winner, _ = pick_task(query, TASKS)
    assert winner is not None, f"{query!r} resolved to nothing"
    assert expected.lower() in winner["name"].lower()


def test_elevate_and_elevator_stay_apart():
    """Two similar titles must not collapse into each other."""
    elevate, _ = pick_task("elevate conference", TASKS)
    elevator, _ = pick_task("elevator market study", TASKS)
    assert "Elevate Conference" in elevate["name"]
    assert "Load Elevator" in elevator["name"]


def test_unrelated_words_score_zero():
    """Short unrelated words sit at 0.3-0.5 on raw similarity — that is coincidence."""
    assert score_task_name("dinner", "Review deck") == 0.0
    assert rank_tasks("xylophone", TASKS) == []


def test_a_real_typo_still_matches():
    """Typos land far above the floor; coincidences do not."""
    assert score_task_name("krinsky", "Krinksy") > 0.7


def test_ambiguity_asks_instead_of_guessing():
    """This path leads to 'mark it complete' — a coin flip is not acceptable."""
    tasks = [{"gid": "1", "name": "book flights"}, {"gid": "2", "name": "book hotel"}]
    winner, candidates = pick_task("book", tasks)
    assert winner is None
    assert len(candidates) == 2


def test_an_exact_name_wins_outright():
    tasks = [{"gid": "1", "name": "Notebook lm"}, {"gid": "2", "name": "Notebook lm ideas"}]
    winner, _ = pick_task("Notebook lm", tasks)
    assert winner["gid"] == "1"


def test_filler_only_query_does_not_match_everything():
    winner, candidates = pick_task("the task", TASKS)
    assert winner is None or winner["match_score"] < 1.0
    assert len(candidates) < len(TASKS)


def test_tokenize_drops_filler_but_never_everything():
    assert tokenize("mark the elevator task complete") == ["elevator"]
    assert tokenize("mark it complete") == ["mark", "it", "complete"]


def test_ranking_is_ordered_and_scored():
    ranked = rank_tasks("elevator", TASKS)
    assert ranked, "expected at least one match"
    assert ranked[0]["match_score"] >= ranked[-1]["match_score"]
    assert all("match_score" in t for t in ranked)


def test_search_ranks_instead_of_substring_matching(monkeypatch):
    """The integration point: search must return ranked hits, not containment."""
    from app.integrations import asana_manager

    monkeypatch.setattr(
        asana_manager, "list_asana_project_options",
        lambda: {"projects": [{"gid": "p1", "name": "IFG Tasks"}]},
    )
    monkeypatch.setattr(
        asana_manager, "list_asana_tasks",
        lambda **kw: {"tasks": TASKS},
    )
    out = asana_manager.search_asana_tasks(query="the elevator task")
    assert out["ok"] is True
    assert out["tasks"], "loose phrasing must still find the task"
    assert "Load Elevator" in out["tasks"][0]["name"]


# ── audit findings: description text, completed tasks, duplicate accounts ────


def test_a_word_only_in_the_description_still_finds_the_task():
    """"the FINRA task" found nothing: FINRA appears in the description of
    "Follow up with Angelo (Morgan Stanley) — Affiliate Investment Bank program"
    and never in its title. Kory names the substance, not the title."""
    tasks = [
        {"gid": "1", "name": "Follow up with Angelo (Morgan Stanley) — Affiliate Investment Bank",
         "notes": "Once IFG has its FINRA registration he can advocate for a listing."},
        {"gid": "2", "name": "Book dinner reservation", "notes": ""},
    ]
    winner, _ = pick_task("the FINRA task", tasks)
    assert winner is not None and winner["gid"] == "1"


def test_a_title_match_outranks_a_description_match():
    tasks = [
        {"gid": "1", "name": "Dripify campaign setup", "notes": ""},
        {"gid": "2", "name": "Unrelated", "notes": "mentions dripify once in passing"},
    ]
    ranked = rank_tasks("dripify", tasks)
    assert ranked[0]["gid"] == "1"


def test_open_work_sorts_above_finished_work_on_a_tie():
    """"mark X complete" means the live one, not last year's copy."""
    tasks = [
        {"gid": "done", "name": "Elevator landing page", "completed": True},
        {"gid": "open", "name": "Elevator landing page", "completed": False},
    ]
    assert rank_tasks("elevator landing page", tasks)[0]["gid"] == "open"


def test_a_completed_task_is_still_findable():
    """Search excluded completed tasks, so "did I finish X?" answered "no such
    task" — denying it exists rather than saying it is done."""
    tasks = [{"gid": "1", "name": "Send Matt the market study", "completed": True}]
    winner, _ = pick_task("the market study task", tasks)
    assert winner is not None and winner["completed"] is True


def test_search_uses_a_single_pass_over_open_and_done(monkeypatch):
    from app.integrations import asana_manager

    calls = []
    monkeypatch.setattr(
        asana_manager, "list_asana_project_options",
        lambda: {"projects": [{"gid": "p1", "name": "IFG Tasks"}]},
    )

    def fake_list(**kw):
        calls.append(kw.get("bucket"))
        return {"tasks": TASKS}

    monkeypatch.setattr(asana_manager, "list_asana_tasks", fake_list)
    asana_manager.search_asana_tasks(query="elevator")
    # Buckets filter an already-fetched list, so asking twice fetched Asana twice.
    assert calls == ["any"], calls
