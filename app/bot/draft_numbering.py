"""Draft numbers mean the list Kory was actually shown.

He reads `pending`:

    1) Dana Reyes — intro call
    2) Rob Walters — coffee
    3) Priya Nair — board prep

and types `approve draft 1`. The number was resolved by re-reading the queue at
command time, so anything that changed the queue in between silently changed
what "draft 1" meant:

* Dana's draft clears — approved from a card, auto-executed, holds expired, or
  Kory rejected it — and everything shifts up. `approve draft 1` sends ROB's
  offer to Rob, with Kory believing he sent Dana's.
* A new HIGH priority proposal is staged (the orchestrator polls continuously,
  and the queue sorts priority first), which pushes every number down by one.

Both send a real email to a real person on the strength of a number that meant
something else a moment earlier. It is the same defect as a status disagreeing
with the world, wearing different clothes: the reference Kory used and the
reference Lexi resolved were two different things.

So rendering the list records it, and a draft number resolves against that
record. If the proposal it names has since moved on, the command is refused by
name rather than quietly landing on its neighbour.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass

from app.storage.lexi_db import get_lexi_connection

logger = logging.getLogger(__name__)

# Kory is the only person who drives this surface, so one snapshot is enough and
# a per-conversation key would be ceremony. If Lexi ever serves more than one
# person, this becomes a key on the row rather than a new mechanism.
_SNAPSHOT_KEY = "teams_pending_list"


def ensure_snapshot_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS teams_list_snapshots (
            key TEXT PRIMARY KEY,
            proposal_ids TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )


@dataclass(frozen=True)
class DraftReference:
    """What a number Kory typed turned out to mean."""

    proposal_id: int | None
    problem: str = ""
    from_snapshot: bool = False

    @property
    def ok(self) -> bool:
        return self.proposal_id is not None and not self.problem


def record_pending_snapshot(proposal_ids: list[int]) -> None:
    """Remember the order the pending list was just rendered in."""
    try:
        with get_lexi_connection() as conn:
            ensure_snapshot_table(conn)
            conn.execute(
                """
                INSERT INTO teams_list_snapshots (key, proposal_ids, created_at)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(key) DO UPDATE SET
                    proposal_ids = excluded.proposal_ids,
                    created_at = excluded.created_at
                """,
                (_SNAPSHOT_KEY, json.dumps(list(proposal_ids))),
            )
            conn.commit()
    except sqlite3.Error:
        # Losing the snapshot degrades to the old live-queue behaviour; it must
        # never stop the list from rendering.
        logger.exception("Could not record the pending-list snapshot.")


# A listing Kory acted on hours later still means what it said; one from last
# week is a different world. Outside this window the live queue is the better
# reading of the number.
SNAPSHOT_TRUSTED_HOURS = 24


def _load_snapshot() -> list[int]:
    try:
        with get_lexi_connection() as conn:
            ensure_snapshot_table(conn)
            row = conn.execute(
                "SELECT proposal_ids FROM teams_list_snapshots WHERE key = ? "
                "AND created_at >= datetime('now', ?)",
                (_SNAPSHOT_KEY, f"-{SNAPSHOT_TRUSTED_HOURS} hours"),
            ).fetchone()
    except sqlite3.Error:
        logger.exception("Could not read the pending-list snapshot.")
        return []
    if not row:
        return []
    try:
        parsed = json.loads(str(row[0]))
    except ValueError:
        return []
    return [int(pid) for pid in parsed] if isinstance(parsed, list) else []


def _describe(proposal_id: int) -> tuple[str, str]:
    """(status, label) for a proposal, for an error Kory can act on."""
    try:
        with get_lexi_connection() as conn:
            row = conn.execute(
                """
                SELECT p.status, e.subject, e.sender
                FROM proposals AS p
                LEFT JOIN email_threads AS e ON e.thread_id = p.thread_id
                WHERE p.id = ?
                """,
                (proposal_id,),
            ).fetchone()
    except sqlite3.Error:
        return "", ""
    if not row:
        return "", ""
    from app.bot.teams_labels import email_thread_label

    return str(row["status"] or ""), email_thread_label(
        subject=row["subject"], sender=row["sender"]
    )


def resolve_draft_number(number: int, *, live_queue_ids: list[int]) -> DraftReference:
    """Map the number Kory typed to the proposal he meant.

    Prefers the snapshot of the list he was shown. Falls back to the live queue
    when there is no snapshot covering that position — then the number genuinely
    does refer to the current list, which is the best available meaning.
    """
    if number < 1:
        return DraftReference(proposal_id=number)

    snapshot = _load_snapshot()
    if number <= len(snapshot):
        proposal_id = snapshot[number - 1]
        status, label = _describe(proposal_id)
        if not status:
            # The snapshot names something this database has never heard of — a
            # rebuilt database, a swept test row. Refusing would be theatre: we
            # cannot tell Kory what he meant, so fall through to the live queue,
            # where the number at least refers to a list that exists.
            pass
        elif proposal_id not in live_queue_ids:
            # The thing he named moved on. Say so — resolving to whatever now
            # sits in that position is how the wrong person gets emailed.
            readable = status.replace("_", " ")
            return DraftReference(
                proposal_id=None,
                problem=(
                    f"Draft {number} was **{label}**, and it is no longer waiting "
                    f"to send (it is now {readable}). Nothing was sent. "
                    "Say **pending** for the current list."
                ),
                from_snapshot=True,
            )
        else:
            return DraftReference(proposal_id=proposal_id, from_snapshot=True)

    if number <= len(live_queue_ids):
        return DraftReference(proposal_id=live_queue_ids[number - 1])

    # Out of range for both: a raw proposal id. They cannot collide — a draft
    # number is bounded by the queue length and ids are four digits and up.
    return DraftReference(proposal_id=number)
