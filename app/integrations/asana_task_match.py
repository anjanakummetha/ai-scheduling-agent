"""Match what Kory calls a task to what the task is actually called.

Task search was literal substring containment — `needle in name`. In chat nobody
quotes a task title verbatim: "the elevator task" never matches "Load Elevator
market-study landing page into Dripify campaign", because of the trailing word
"task" alone. Add a typo on either side ("Bruce Krinksy" is spelled that way *in
Asana*) and containment finds nothing, which Lexi then reported as the task not
existing.

Scoring is deterministic and local — no LLM, no extra API call. It returns ranked
candidates so the caller can act on a clear winner and ask about a near-tie
rather than silently picking one.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

# Words that carry no signal about *which* task is meant.
_FILLER = frozenset(
    {
        "a", "an", "and", "as", "asana", "at", "board", "card", "complete", "completed",
        "delete", "do", "done", "for", "from", "in", "it", "item", "list", "mark",
        "me", "my", "of", "off", "on", "one", "please", "project", "task", "tasks",
        "that", "the", "then", "there", "this", "to", "todo", "up", "with",
    }
)

# Below this a candidate is noise rather than a weak match.
MIN_SCORE = 0.34
# A winner must beat the runner-up by this much to be taken without asking.
DECISIVE_MARGIN = 0.12


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9\s]+", " ", (text or "").casefold()).strip()


def tokenize(text: str, *, drop_filler: bool = True) -> list[str]:
    tokens = [t for t in _normalize(text).split() if t]
    if not drop_filler:
        return tokens
    meaningful = [t for t in tokens if t not in _FILLER]
    # "mark the task complete" is all filler — fall back rather than match nothing.
    return meaningful or tokens


# Unrelated short words routinely score 0.3-0.5 on raw sequence similarity
# ("dinner" vs "deck" is 0.4). Anything below this is coincidence, not a typo:
# real typos sit far higher ("krinsky"/"krinksy" is 0.86).
_TYPO_FLOOR = 0.72


def _token_similarity(a: str, b: str) -> float:
    """1.0 identical, high for a prefix/typo, 0 for unrelated."""
    if a == b:
        return 1.0
    if len(a) >= 4 and len(b) >= 4 and (a.startswith(b) or b.startswith(a)):
        return 0.94
    if len(a) >= 5 and len(b) >= 5 and (a in b or b in a):
        return 0.88
    ratio = SequenceMatcher(None, a, b).ratio()
    return ratio if ratio >= _TYPO_FLOOR else 0.0


def score_task_name(query: str, name: str) -> float:
    """How well a task title answers what Kory said. 0..1."""
    q_norm, n_norm = _normalize(query), _normalize(name)
    if not q_norm or not n_norm:
        return 0.0
    if q_norm == n_norm:
        return 1.0
    # A quoted-ish phrase that appears verbatim is as good as exact.
    if len(q_norm) >= 4 and q_norm in n_norm:
        return 0.97

    q_tokens = tokenize(query)
    n_tokens = tokenize(name, drop_filler=False)
    if not q_tokens or not n_tokens:
        return 0.0

    # Best match for each query token, so word order and extra title text don't matter.
    per_token = []
    for q in q_tokens:
        best = max((_token_similarity(q, n) for n in n_tokens), default=0.0)
        per_token.append(best)
    coverage = sum(per_token) / len(per_token)

    # A single strong hit on a distinctive word ("dripify") should still surface a
    # long title, so reward the best token as well as the average.
    strongest = max(per_token)
    score = 0.7 * coverage + 0.3 * strongest

    # Titles far longer than the query are likelier to match by accident.
    if len(n_tokens) > 3 * len(q_tokens):
        score *= 0.93
    return round(min(score, 0.96), 4)


# A hit in the description is real evidence but weaker than one in the title, so
# it is capped below a title match rather than competing with it.
_NOTES_WEIGHT = 0.82
_NOTES_SCAN_CHARS = 600


def score_task(query: str, task: dict[str, Any]) -> float:
    """Score a task on its title and its description.

    Title-only matching missed how Kory actually refers to work: "the FINRA task"
    found nothing, because FINRA appears in the description of "Follow up with
    Angelo (Morgan Stanley) — Affiliate Investment Bank program" and never in its
    title. He names the substance; the title is whoever typed it that day.
    """
    best = score_task_name(query, str(task.get("name") or ""))
    notes = str(task.get("notes") or "")[:_NOTES_SCAN_CHARS]
    if notes:
        best = max(best, round(score_task_name(query, notes) * _NOTES_WEIGHT, 4))
    return best


def rank_tasks(query: str, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tasks ordered best-first, each with a `match_score`, weak ones dropped."""
    scored = []
    for task in tasks:
        score = score_task(query, task)
        if score >= MIN_SCORE:
            scored.append({**task, "match_score": score})
    # Open work first when scores tie — "mark X complete" means the live one.
    scored.sort(
        key=lambda t: (-t["match_score"], bool(t.get("completed")), str(t.get("name") or ""))
    )
    return scored


def pick_task(query: str, tasks: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Return (confident winner or None, ranked candidates).

    A winner is only confident when it is clearly ahead of the runner-up. Two
    plausible tasks should produce a question, not a coin flip — this path leads
    to "mark it complete", and completing the wrong task is not a silent error.
    """
    ranked = rank_tasks(query, tasks)
    if not ranked:
        return None, []
    if len(ranked) == 1:
        return ranked[0], ranked
    best, runner_up = ranked[0], ranked[1]
    if best["match_score"] == 1.0 and runner_up["match_score"] < 1.0:
        return best, ranked
    if best["match_score"] - runner_up["match_score"] >= DECISIVE_MARGIN:
        return best, ranked
    return None, ranked
