# Lexi — Session Handoff (updated 2026-08-11, overnight fix session)

**Resume phrase:** *"Read the open items, then keep testing the scheduling engine."*

18 commits, `main` at `81e8fc9`. CI green (689 tests), laptop/GitHub/box all in
sync, working tree clean. Everything below is deployed and live.

---

## 0. THE ONE THING TO KNOW

Twelve defects were found and fixed. **Every single one presented to the user as
success.** They are variants of one theme: *the system could not distinguish
"nothing" from "not enough", "unknown", or "didn't check"* — and each produced a
confident, wrong, actionable answer.

- Asana writes returned a hardcoded `ok: True` and never read the response
- HubSpot scanned 50 of 2,207 contacts and said "your book looks clean"
- A capped scan reported `complete: True` (a bug *introduced while fixing* the above)
- An unknown contact total was rendered as the row count
- One free slot was reported as zero, producing "booked until September"

**The lesson that kept paying off: verify against the destination, not the
reply.** Roughly half of Lexi's "Done!" messages were wrong when checked in
Asana / HubSpot / Outlook directly.

**Tell Kory:** for anything consequential, check the destination. Not because
Lexi is broadly unreliable — reads and triage are solid — but because its
confirmations describe what it *attempted*.

---

## 1. OPEN ITEMS (priority order)

### 1a. Calendar write surface only supports holds — **highest**
There is **no plain create-event tool**. `lexi_place_calendar_hold` is the only
way onto the calendar, and it applies a canonical `HOLD: ` prefix downstream
(`calendar_holds.py`). So "create a calendar event called X" produces
`HOLD: X`.

There is also **no move/reschedule tool**. A move falls through to the generic
`lexi_execute_outlook_action` passthrough, hand-assembling a slug + JSON. Logged
live: three calls, two returning in ~0.00–0.01s (error responses), and Lexi
still reported *"Done! shifted to 11:45"*. The event never moved.

Fix: add `lexi_create_calendar_event` (no HOLD prefix) and
`lexi_move_calendar_event` (event id + new time, verifies the result). Do not
patch the hold path.

Repro: `create a calendar event called "TEST move event" tomorrow at 11:30`
then `move it 15 minutes later`.

**Until fixed: Kory should create and move ordinary calendar events in Outlook.**

### 1b. Verify-after-write across all integrations
The plumbing fix landed (`execute_tool` now preserves Composio's `successful`
flag; Asana and HubSpot writers check it). The structural fix did not: **writes
should read back and return the observed state, not a boolean.** If `assign`
returns `assignee: "Kory Mitchell"` read back from Asana, Lexi can only say that
when it's true — it quotes data instead of narrating intent. One extra read per
consequential write; Composio is at ~12% of a 200k budget.

### 1c. `meeting_request` over-fires on transactional mail
Invoices, subscription renewals, login links and out-of-office replies are being
classified `meeting_request` (234 in 7 days). Nothing reached Kory — all landed
`no_reply_needed`/`rejected`, so the second stage caught it — but the intent
label alone is unreliable. Check what reads intent downstream.

Also worth spot-checking: `#8871 RE: McCombs | Iconic` and `#8927 Energy
Solutions Catch Up` look like genuine requests that were marked no-reply.

### 1d. Memory writer persists inferences as facts
After a failed Asana search, Lexi invented an explanation ("the Anju project
isn't indexed"), wrote it to `/home/lexi/.hermes/memories/MEMORY.md`, and would
have loaded it every session. **It was false** — the real cause was the
`mine_only` ownership filter. Entry deleted (backup:
`MEMORY.md.bak.before-false-asana-removal`); 5 entries remain.

The writer should record Kory's preferences, not its own theories about why a
tool failed. Audit that file periodically. One surviving entry — *"Kory approves
Asana writes freely… pass `confirm='true'` directly"* — is a learned habit worth
revisiting.

### 1e. Staged-ask backlog (carried over)
26 threads in `awaiting_reply_prompt` since Aug 3. Any inbound reply re-pings
Kory. Decide: TTL, or bulk-close with his sign-off.

### 1f. 27 duplicate HubSpot contact pairs found
Real cleanup nobody knew about (e.g. `clay.harris@eosworldwide.com` ↔
`clay@clayharris.com`). **Staged proposals only — nothing merged.** Merges are
permanent and require naming each pair. Worth showing Kory rather than either of
us applying them.

### 1g. Smaller
- Outlook reads have **no pagination** and Composio ignores `OUTLOOK_LIST_MESSAGES`'
  `filter`, so searches fetch the top 50 and filter client-side. An older email
  is invisible. Backup poll uses `top: 15` (≈6× headroom at current volume).
- `search_contacts` still returns `total` from a HubSpot response that may omit
  it; now `None` when unknown, but every caller must treat `None` as unknown.
- CI has been pushed with branch-protection bypass all session (runs pass after
  the fact).

---

## 2. FIXED THIS SESSION

**Infrastructure**
- Disk 45% → 38% (7 GB). 96 unpruned per-deploy DB backups; `deploy_lexi.sh` now
  keeps 3. Note `git checkout HEAD --` (not `checkout --`) is required to clear a
  *staged* change — the old stash/pop path stranded one on the box.
- **CI ran zero tests while reporting failure.** Bare `pytest` matched
  `*_test.py`, sweeping in `scripts/hs_bcc_test.py`, which `sys.exit()`s at
  import → `INTERNALERROR`. `pytest.ini` pins `testpaths`/`python_files`.
  0 → 689 tests. The phase suite and approval-safety gates had **never run**.
- Suite now owns `data/lexi_pytest.db`, rebuilt each run. Previously it wrote to
  whatever `.env` named — a live orchestrator polled the same file, saw a fixture
  proposal in `pending_invite`, and **pushed real Adaptive Cards into Kory's
  Teams chat**. Also killed a stale local `uvicorn` (running since 20 Jul).
- Three scripts that could reach live Teams now force it off before config loads.
- `data/kory_voice_profile.json` untracked + gitignored (held verbatim excerpts of
  real correspondence); a failed fetch no longer wipes a good profile, with a
  30-min backoff. That loop was hammering a throttled mailbox on every compose.

**Production bugs**
- **4 MCP decision tools were silent no-ops** — `get_pending_decisions`,
  `approve_decision`, `modify_and_approve_decision`, `reject_decision` called
  another `@_tool` function without awaiting it, returning a coroutine. Kory's
  typed `approve #N` was unaffected, but the guide tells him numbers are optional.
- **Asana** (6): project-only move filed into an unrelated default section;
  "move" never removed the old project; all writes hardcoded `ok: True`;
  unassigned tasks invisible (`mine_only`); no assignment support; **reads
  truncated to one page** (9-task project returned 4).
- **HubSpot** (5): duplicate scan sampled 50 of 2,207 and said "clean" (a full
  scan finds **27 pairs**); enrichment previewed 12 while approving 25; writes
  never checked the response; descriptions claimed writes were blocked while they
  were live; unknown totals rendered as row counts.
- **All-day events blocked timed meetings.** An all-day "Kory in Chicago" made
  the whole Thursday unbookable and the confirm-time guard fails closed with no
  override, so Kory was locked out of his own calendar by a travel banner. The old
  exemption list (`good friday`, `tax day`, `"stay at "`) was this bug patched one
  name at a time.
- **"You're booked until September."** `MIN_SLOT_OPTIONS = 2` discarded any week
  holding exactly one opening. Happy hour only generates candidates at 15:30/16:00
  and skips Fridays, so next week only Tuesday survived — one slot, treated as
  none. Recurring Mon/Wed/Fri meetings reproduced the collision every week, so the
  ladder walked to Sept 7. Chat availability now passes `min_options=1`; automated
  offers keep 2.

---

## 3. HARD-WON FACTS (do not relearn)

- **A deploy alone is not enough for Lexi to notice a new/changed tool.** The
  session must turn over too — a clean gateway restart *resumes* the prior
  session with its stale tool list. Clear `~/.hermes/sessions/sessions.json` and
  restart as part of every deploy that touches a tool signature. Symptom: Lexi
  says a capability doesn't exist right after you shipped it.
- `get_raw_composio_tools()` **pages at 20 by default** — pass `limit=500`.
  Without it, `ASANA_UPDATE_A_TASK` looks like it doesn't exist (153 tools exist).
- Composio returns **200 with `successful: false`** and no `error`. `execute_tool`
  raises only on `error`, so check `successful`.
- Asana tasks are **multi-homed**: `ADD_TASK_TO_SECTION`/`ADD_PROJECT_FOR_TASK`
  add, they don't move. A real move needs `ASANA_REMOVE_PROJECT_FROM_TASK`.
- Asana `list_asana_tasks` / `search_asana_tasks` default `mine_only=True`;
  unassigned tasks are invisible.
- HubSpot `search_contacts` **has no owner filter by default** in the duplicate
  path — it scans the whole portal (2,207), not Kory's 1,016.
- Kory-channel replies go to the **sender only**; Lexi-channel replies are
  **reply-all**. Chat-initiated offers compose a fresh email (synthetic thread id).
- Logs are files: `logs/lexi.log` (orchestrator) and `~/.hermes/logs/agent.log`
  (tool calls, `api_calls`, `tool_turns`). **`api_calls=1` with no tool calls means
  Lexi answered from context without checking anything.**
- Scheduling diagnostics are the fast diagnostic: `scheduling_window`,
  `candidates_scored`, `window_expanded`, `expanded_window`, `block_minutes`.

---

## 4. PRODUCTION POSTURE (unchanged, live)

`LEXI_DRY_RUN=false` · `LEXI_WRITE_MODE=kory` · `LEXI_KORY_OUTBOUND_BLOCKED=false`
· `LEXI_REQUIRE_KORY_APPROVAL=true` · `LEXI_AUTO_EXECUTE_ENABLED=false` ·
`LEXI_ALLOW_IMMEDIATE_SEND=false` · Asana + HubSpot live writes **on** ·
`LEXI_TEAMS_TEXT_ONLY=true`

**`LEXI_ALLOWED_RECIPIENTS` is unset — there is no recipient allowlist.** Name
your own address explicitly in any send test.

Email sends require typed `approve #N`. Asana/HubSpot writes do **not** — they're
gated by a model-supplied boolean (accepted-risk design item).

---

## 5. COST (measured)

- Anthropic **$8.25 total ever** (Jul 23 – Aug 11); ~$0.33–0.43/day → **$11–13/mo**
  at Kory-idle volume. Triage (Haiku 4.5) is ~93% of calls.
- **Blind spot:** the Hermes gateway (`claude-sonnet-4-6`) writes nothing to
  `llm_cost_log`. Kory's actual chat turns are unmeasured. Estimate **$40–75/mo**
  all-in once he's active.
- Composio **~12% of a 200k monthly budget**. Tonight's full-book scans are
  negligible (~23 calls each).
- Prompt caching shows zero on triage — correct, not a bug: prompts are 440–650
  tokens, below Haiku 4.5's 4096-token cacheable minimum.

---

## 6. TESTING STATE

Asana and HubSpot verified end-to-end. **Scheduling is mid-test** — the engine
is the remaining surface. Ground truth for next week, for verifying answers:

| Day | Committed |
|---|---|
| Mon 17 | Trainer 06:30–08:30 · Pipeline 09:00–10:00 · WOB 11:00–13:00 · Inbox review 14:30–15:30 · Sujash 16:00–16:30 |
| **Tue 18** | **nothing after 15:00** |
| Wed 19 | Sujash 16:00–16:30 · Kory‑Jonny 16:00–17:30 |
| Thu 20 | Dan Phillips 16:00–16:30 |
| Fri 21 | Inbox review 15:00–16:00 · Sujash 16:00–16:30 |

Still to test: meeting types (coffee/drinks/lunch venue vs Teams link, durations),
hard blocks (before 08:30, Doug Mon 13:15–14:15), timezone handling ("mornings her
time" = recipient's zone), reschedule (2 options), and the full offer → reply →
book lifecycle. Prompt list is in the session transcript.

**Watch for:** a slot offered inside a hard block, and any "no availability"
claim. The second is where confident wrongness hides.
