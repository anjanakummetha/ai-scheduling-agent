# Lexi — Session Handoff (updated 2026-08-14)

**Resume phrase:** *"Read the open items, then start the HubSpot fixes."*

`main` at `1bf9050`. 49 commits since the last handoff. **878 tests green.**
Laptop / GitHub / box in sync, tree clean, deployed and live.

---

## 0. THE ONE THING TO KNOW

**A guard that the model never reaches is not a guard.**

Two fixes this session passed the full suite and did *nothing* in production —
not because the logic was wrong, but because the code never ran:

1. The Asana duplicate check sat *after* project resolution, so a request naming
   no project (the normal way Kory asks) stopped at "which project?" and never
   looked. Moved to the front. Still didn't fire.
2. Because `lexi_create_asana_task` **was never called at all.** Its description
   said project and due_on were "REQUIRED before anything is written", so the
   model collected them from Kory itself and answered without the tool. Logs
   showed `list_asana_projects` → `list_asana_boards` → text response.

The tool description *is* control flow. When it says "X is required", the model
gathers X first — which moves every guard inside that function out of reach.

**How to check:** `~/.hermes/logs/agent.log`. Grep the turn for
`agent.tool_executor: tool <name> completed`. If the tool isn't there, nothing
inside it ran, no matter how green the suite is. A `0.01s` completion means it
returned before any network call — usually an approval gate.

Corollary from the last handoff, still true and still paying off: **verify
against the destination, not the reply.**

---

## 1. OPEN ITEMS (priority order)

### 1a. HubSpot — **next up, this is where to start**
The ownership guard just landed (below). Everything else in HubSpot is
unreviewed this session. Anjana's stated order was: lock Asana, then HubSpot.
Asana is locked.

Safe to test today — reads, nothing writes without approval: `hubspot_status`,
`health_report`, `find_contacts`, `compare_books`, `recent_changes`,
`deals_snapshot`, `prebrief_enrich`, `duplicate_merges` (proposals only).

### 1b. Asana duplicate detection — **PARKED by Anjana 2026-08-14**
Not broken logic; unreachable code. `assert_kory_approved_write` is the first
line of `create_asana_task_from_chat`, so with `confirm=false` it returns
`confirmation_required` in 0.01s and `find_similar_open_task` never runs. Kory
sees "confirm and I'll add it", says yes, and *then* the duplicate surfaces —
one turn too late to save him the round trip.

The matcher is correct. Verified live 2026-08-14: `"Cancel MultCloud"` → *Cancel
multcloud* (1.0), `"Schedule consultation with Brooke"` → exact (1.0), `"Sort
out the insurance renewal"` → nothing, correctly.

**If resumed:** run the duplicate search *before* the approval gate. It is a
read-only search — nothing about it needs Kory's approval to run.

### 1c. Latency — deferred, deliberately
Measured, ranked, not implemented. See `lexi-latency-findings` memory. Median
16.2s, mean 28.5s, worst 252s.

**Do it as one pass at the end, not per-integration** — the top lever (Composio
re-fetching the tool schema before every call, doubling every round trip) is in
the shared wrapper and fixes all three at once. Doing it mid-testing adds a
second variable to every failure, and schema caching is the risky one: a stale
cached schema means malformed payloads, which is exactly the HubSpot note bug.

Two items are safe to do anytime because they can't change behavior: rotate
`~/.hermes/logs/mcp-stderr.log` (150MB, unbounded) and confirm whether prompt
caching is actually engaged (configured, but no hit/miss appears in logs).

### 1d. Asana subtasks are invisible
Found in the audit, not fixed, Kory hasn't been asked. No tool lists or creates
subtasks, so a parent task reads as a single item.

### 1e. Carried over, untouched this session
- **`meeting_request` over-fires** on transactional mail (234 in 7 days). All
  landed `no_reply_needed`/`rejected`, so nothing reached Kory — but check what
  reads intent downstream.
- **Memory writer persists inferences as facts.** Audit
  `/home/lexi/.hermes/memories/MEMORY.md` periodically.
- **26 stale staged asks** in `awaiting_reply_prompt` since Aug 3. Any inbound
  reply re-pings Kory. Needs a TTL or a bulk close with his sign-off.
- **Outlook reads have no pagination**; Composio ignores the `filter` arg, so
  searches fetch the top 50 and filter client-side. An older email is invisible.
- **`OUTLOOK_LIST_MESSAGES` hit 317 times** in one window. Unexplained.
- **Teams renders one turn twice** — the ❓ numbered card *and* Lexi's own prose
  asking the same thing. Cosmetic, and it lives in the separate gateway repo.

---

## 2. DONE THIS SESSION

**Calendar** — `lexi_create_calendar_event` (no `HOLD:` prefix) and
`lexi_move_calendar_event`. The move uses the **flat** `start_datetime` /
`end_datetime` / `time_zone` schema, not the nested `start: {dateTime}` shape
the create path uses, and verifies by reading the event back and comparing the
*instant*, not the wall-clock string. Closes handoff item 1a.

**Deploy** — `NEW_SESSION=1` clears the Hermes session so a changed tool
signature is actually picked up; warns loudly when `hermes_mcp_server.py`
changed and the flag was left off.

**Email**
- Kory's branded HTML signature on every send from his account, with the CID
  logo. Podcast line ends on the link (`https://www.theturnpodcast.com/`, label
  "The Turn Podcast"), no trailing phrase.
- Sign-off stripper handles the variants Lexi writes, so the block isn't doubled.
- `lexi_save_email_to_drafts` — Kory parks a draft in Outlook to review later.
  The draft *is* the review step, so no approval gate.
- Draft recipients are verified against his own addresses → last 50 sent/inbox →
  HubSpot. An unreachable CRM verifies as `unchecked` rather than blocking.

**Briefing** — the briefing Kory was actually sent is stored (not regenerated),
so Lexi can act on "the thing in my brief this morning". 4-day lookback, pruned
at 30 days.

**Asana** (the bulk of the session — ~15 commits)
- Fuzzy task matching on title **and description** (`asana_task_match.py`). "The
  FINRA task" finds a task whose title never says FINRA. `_TYPO_FLOOR = 0.72`
  separates real typos (`krinsky`/`krinksy` = 0.86) from coincidence
  (`dinner`/`deck` = 0.40).
- Search is one pass over all projects, `limit=500` — 98 → 691 tasks scanned for
  one extra HTTP call.
- People resolved the way Kory says them ("Anju" → Anjana Kummetha), tolerant of
  descriptors and near-miss spellings; `@iconicfounders.com` preferred on ties.
- **Owner guard now runs on the gid path**, not just the name path. This is the
  one that mattered: Lexi completed Heidi's task on a shared board because the
  guard only checked when the task was identified by name. Reopened it and
  verified. The guard fails *open* on a read failure so an outage can't lock
  Kory out of his own tasks.
- Board lookup searches the named project → the task's own project → all
  projects. Placement is read back **after** the unfile, so a move reports where
  the task landed rather than where it was.
- Asks board + due date + owner in one message, proposes a board when the title
  names one, declines to guess on a tie.
- Multi-homed tasks collapse by gid — one task on two projects is not two tasks.
- `complete_asana_task` reads the task back; still-open is reported as failure.

**HubSpot** — ownership guard on the write paths (PR #28). `meeting_note` had
one since it shipped; merge and enrichment never did, and
`propose_duplicate_merges` scans the whole portal on purpose.
- Proposals name the owner of any record that isn't Kory's.
- Merges re-read both contacts at apply time and refuse a cross-owner pair
  without an explicit ack. **This guard fails CLOSED** — deliberately unlike
  Asana's. An Asana mistake is reopened in one call; a HubSpot merge is not
  undone at all.
- Enrichment returns `applied` / `already_filled` / `not_kory_owned`. Both of
  the last two write nothing and they mean opposite things to Kory.

---

## 3. HARD-WON FACTS (do not relearn)

**New this session**

- **The tool description is control flow.** See §0. Wording like "X is REQUIRED
  before anything is written" makes the model gather X *before calling*, which
  strands every guard inside the function. Say "call this first, the tool will
  ask" instead. `tests/test_create_task_tool_description.py` pins this.
- **Check the log, not the transcript.** A plausible-sounding reply proves
  nothing about which tools ran. `agent.log` has the ground truth.
- **`importlib.reload(app.config)` breaks test isolation.** It builds a new
  `Settings` while already-imported modules keep the old reference — a test can
  pass alone and fail in the suite. Patch settings on the module under test.
- **Composio ignores `archived` on `HUBSPOT_RETRIEVE_OWNERS`** (verified live:
  `True` and `"true"` both return the same 8 active owners). So an archived
  owner id can't be resolved to a name. Same defect class as
  `OUTLOOK_LIST_MESSAGES` ignoring `filter` — **treat every Composio filter or
  scope argument as decorative until proven live.**
- `OUTLOOK_UPDATE_CALENDAR_EVENT` takes **flat** `start_datetime` /
  `end_datetime` / `time_zone`. The create path's nested `start: {dateTime,
  timeZone}` is silently ignored, and the event does not move.

**Carried forward**

- **A deploy alone is not enough for Lexi to notice a new or changed tool.** The
  session must turn over: `ssh root@<host> 'NEW_SESSION=1 bash -s' <
  scripts/deploy_lexi.sh`. Left off by default because clearing the session ends
  whatever thread Kory is mid-conversation on.
- `get_raw_composio_tools()` **pages at 20** — pass `limit=500`.
- Composio returns **200 with `successful: false`** and no `error`.
- Asana tasks are **multi-homed**: `ADD_PROJECT_FOR_TASK` adds without removing.
  A real move needs `ASANA_REMOVE_PROJECT_FROM_TASK`.
- Graph message ids are **mailbox-scoped** — read back with the same connection
  that wrote.
- Kory-channel replies go to the **sender only**; Lexi-channel replies are
  **reply-all**.
- Logs are files: `logs/lexi.log` (orchestrator), `~/.hermes/logs/agent.log`
  (tool calls). **`api_calls=1` with no tool calls means Lexi answered from
  context without checking anything.**

---

## 4. TESTING CONSTRAINTS (Anjana's rules — do not violate)

> "I don't want to do any permanent alterations into a board with multiple
> people he works with."

Almost every Asana project is shared. **IFG Tasks** holds Heidi ×27, Matt ×3,
Natalie ×4. Even **Kory NON-IFG** has Heidi ×2. Check membership before writing
a test prompt — an earlier prompt of mine pointed at the most-shared board.

Allowed for write tests: CEO executive tools, assignments to **Anju or Kory
only**, and his personal **non-IFG** board.

HubSpot is the **shared company CRM** — and merges are permanent. Of Kory's 27
duplicate pairs, **16 involve someone else's records**:

| owner | pairs |
|---|---|
| Natalie Asher | 8 |
| unknown owner (159291600) — not in the owners list, likely deactivated | 6 |
| Matt Maley | 2 |
| Kory's own | 11 |

**`execute_hubspot_batch` has no MCP tool**, so Lexi can propose merges but has
no path to apply one. The guard is there for when that changes.

---

## 5. PRODUCTION POSTURE (live)

`LEXI_ENV=production` · `LEXI_DRY_RUN=false` · `LEXI_WRITE_MODE=kory` ·
`LEXI_KORY_OUTBOUND_BLOCKED=false` · `LEXI_REQUIRE_KORY_APPROVAL=true` ·
`LEXI_AUTO_EXECUTE_ENABLED=false` · `LEXI_ALLOW_IMMEDIATE_SEND=false` · Asana +
HubSpot live writes **on** · outreach sends/campaigns **off**.

**`LEXI_ALLOWED_RECIPIENTS` is unset — there is no recipient allowlist.** Name
your own address explicitly in any send test.

Email sends require a typed `approve #N`. Asana/HubSpot writes do **not** —
they're gated by a model-supplied boolean (accepted-risk design item).

Box: `root@srv1686061.hstgr.cloud`, code at `/home/lexi/AI_Scheduling_Agent`,
venv at `.venv/bin/python` (run as `sudo -u lexi`). **The VPS is multi-tenant** —
a Hermes Agent container and traefik also run there. Never force-restart.

Composio at ~15% of a 200k monthly budget.

---

## 6. TESTING STATE

**Asana: locked.** Kory tested every surface through Teams — fuzzy lookup,
person resolution, the owner guard, section and project moves, complete with
read-back, ambiguity questions, assign-on-create, board suggestion, multi-homed
collapse. Open items are 1b and 1d above, both low-severity.

**Outlook: signature, drafts and the briefing→draft workflow verified live.**
Calendar create/move built and unit-tested; not yet exercised by Kory in Teams.

**HubSpot: next.** Reads were verified 2026-08-05; write steps 2 and 4 passed
live. The ownership guard is new and **has not been exercised in Teams** —
worth a read-only `duplicate_merges` run first so Kory sees the owner labels on
his own data before anything is approved.

**Scheduling: mid-test**, paused by Anjana. Resume at Group C of
`docs/SCHEDULING_LIVE_TEST_PLAN.md`. Still untested: meeting types
(coffee/drinks/lunch venue vs Teams link), hard blocks (before 08:30, Doug Mon
13:15–14:15), timezone handling, reschedule, and the full offer → reply → book
lifecycle.

**Watch for:** any "no availability" claim, and a slot offered inside a hard
block. The first is where confident wrongness hides.
