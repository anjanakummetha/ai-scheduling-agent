# Lexi — Session Handoff (updated 2026-08-15, overnight)

**Resume phrase:** *"HubSpot is finished and deployed. Next session is
scheduling changes."*

`main` at `c1ad002`+. **1,041 tests green.** Laptop / GitHub / box in sync — the
box runs `c1ad002`; anything after it is documentation only and needs no deploy.

**Where this left off (2026-08-15, overnight):** the HubSpot work is done, tested
against the live portal, and deployed. It has **never been driven from Teams** —
that is Anjana's next test, and §6 lists what to type. Two things are waiting on
other people and neither blocks anything:

1. **The email to Kory and Heidi** — drafted and ready in
   `docs/EMAIL_TO_KORY_AND_HEIDI.md`. Not sent. It needs Kelley Johnson on it
   (she is the Sales Navigator admin, not Kory), and it leads with one unsafe
   setting rather than with statistics. Everything it claims is measured; the
   evidence is in `HUBSPOT_DATA_CLEANUP_FINDINGS.md`.
2. **Sales Navigator and HubSpot settings** — Anjana cannot change these without
   Kory and Heidi's sign-off. Deliberate, not blocked work.

**Next up: scheduling.** Nothing in this session touched it. Start from
`SCHEDULING_LIVE_TEST_PLAN.md`.

---

## HubSpot cleanup — built this session, ready for Teams

Kory's book, measured live: **2,224 contacts across IFG, 1,022 his.** No job
title 79 · no company 75 · **no phone 306** · never contacted 102 · DNC 62.
**27 duplicate pairs across a complete 2,224-contact scan, 16 of them touching
someone else's records.**

**The Sales Navigator premise was wrong in a useful way.** Sales Nav is *not*
leaving title and company blank — it fills them, and fills them well (92–93%
populated). What it leaves is junk that looks like data: `Prefer No Connection
to Company`, what LinkedIn writes when a member hides their employer, ~30
contacts book-wide. Not blank, so no `NOT_HAS_PROPERTY` filter surfaced it and
no blank-only guard offered to fix it. **Composio's LinkedIn toolkit cannot
help** — 22 tools, all posting/ads/org-stats; `LINKEDIN_GET_PERSON` resolves
only members who authorised your own app. Verified by listing it live.

New: `lexi_hubspot_apply_batch` (the apply path never existed, so every proposal
was a dead end and #28's guard could not run) and `lexi_hubspot_undo_batch` with
a write log that records the prior value. `is_placeholder` now catches the junk.
Signature mining reads **phone**.

**Testing without touching HubSpot:** `scripts/hubspot_cleanup_rehearsal.py`
runs the whole path against the live book with every write intercepted at
`execute_hubspot_tool`. It caught what unit tests could not — a signature
offering `(800) 962-0418` as its owner's phone. A toll-free switchboard on a
contact record reads as a direct line; worse than the blank it replaced.

### The profile tier — `app/integrations/hubspot_person_lookup.py`

Lexi *can* read LinkedIn, and the tier that does it is built. This was the
session's biggest surprise: Composio's Search `FETCH_URL_CONTENT` is backed by
Exa, which returns a **structured person record** — name, location, work history
with titles, companies and dates — for a LinkedIn profile URL. No scraping, no
auth wall, and it was already wired up. The old tool description told the model
Lexi had no LinkedIn access; that text was wrong and is now rewritten.

**It is only safe because of an inversion.** It never asks what someone's job
title is. It asks whether a candidate profile shows a role at the employer
already on the record. A stranger who shares the name does not also share the
employer, so the answer is checkable. Employer corroboration and title
extraction are the *same* step: find the role that proves the identity, and its
title is the fill.

Two ways in. A stored `hs_linkedin_url` (741 of Kory's contacts have one) is the
first candidate and costs one fetch; otherwise a web search finds candidates and
costs three calls. **The stored URL is not taken on trust** — Phil Holland's
resolves to *Brian Holland*, the same bad-match class as Sales Navigator's
Thomas Heckler. The name check is the only thing between that and a wrong title.

Where there is no employer on file at all and the URL came from the record, it
falls back to a single unambiguous current role, labelled
`linkedin_profile_on_record` with confidence `on_record` and evidence that says
plainly it was **not** independently corroborated. Several concurrent roles is a
question for Kory, not a pick — Jeremy Boka is a VP, a brewery co-owner and a
city councillor.

Three outcomes that are findings rather than fills, each reported separately:
records that are **not people** (a job title on `accounting@` makes it look
human), people who **may have moved**, and people who **hold several roles**.

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

### 1-NEXT. Scheduling — **the next session's work**
Untouched by the HubSpot session. Resume from `SCHEDULING_LIVE_TEST_PLAN.md`;
the last recorded state there is RUN 15 (2026-08-08): 86 tools verified, four
fixes, interactive cards **parked** in text-only mode (`TEAMS_TEXT_ONLY=true`),
any-project Asana shipped. Nothing about scheduling changed since.

### 1-EMAIL. The email to Kory, Heidi and Kelley — **drafted, not sent**
`docs/EMAIL_TO_KORY_AND_HEIDI.md` is a ready draft, not notes. It carries the
six decisions, the numbers, and the one worked example that makes the safety
argument (Thomas Heckler — Heidi's own contact, matched to an Australian software
engineer). Blocked on Anjana sending it, not on any technical work.

Three things that document already warns about, repeated because they are easy
to get wrong: **Kelley Johnson is the Sales Navigator admin, not Kory**, so an
email without her cannot action decisions 1 and 3; **do not say Sales Navigator
is broken** — it is scoped to deal contacts and one setting is unsafe, which is
a different and defensible claim; and **quote 28%, not 40%**, for Lexi's share of
the title and company gaps. The 40% was an estimate this session replaced with a
measurement.

### 1a. HubSpot — **finished; waiting on Kory in Teams**
See the block at the top. Reads were already good; the cleanup path is new,
rehearsed against live data, and apply and undo have both now run against the
real portal — but none of it has run *in Teams*.

**The first apply in Teams writes to the real shared portal.** Enrichment is
undoable (`lexi_hubspot_undo_batch`). A merge is not, by anything, ever — 16 of
the 27 pairs are colleagues' records and the guard blocks those without an
explicit ack.

**The placeholder-company contacts are no longer stuck.** They came *from*
LinkedIn, so they carry a `hs_linkedin_url`, and the profile tier resolves them
from it — Gregory Krier, James Hite, Rich Halvas, Andrew Beaudoin and Matthew
Battey all came back on a probe of the thirteen. Web search on the individual is
no longer an open question either way: it is built, corroborated, and shipped.

**Measured ceiling, full sweep 2026-08-15: 65 values across 59 contacts** —
company 36, phone 16, job title 13. Was 46 across 43. The profile tier is 20 of
the 65 and roughly doubles what she reaches on title and company.

### 1b. Asana duplicate detection — **still PARKED** (the HubSpot twin is fixed)
The same defect in `stage_meeting_note` was fixed this evening: the read-only
lookup now runs before the approval gate. Asana's `create_asana_task_from_chat`
still has the original shape, unchanged, by Anjana's call.

**PARKED by Anjana 2026-08-14**
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

### 1b-2. Hold-release cards fire one per slot — **seen live 2026-08-14, 11:54**
Anjana received three consecutive Teams cards, all reading *"Lexi — hold released
(no reply) / Check in / From Iconic Founders Group"*, differing only in the slot:
`2026-08-24T10:00-06:00`, `2026-08-26T11:00-06:00`, `2026-08-28T09:00-06:00`.
Each said "Held 3 days with no response — calendar hold removed. Ask me to
re-offer times for Check in."

Three things wrong, in rough order of severity:

1. **One request, three cards.** Those are the three slots *offered for a single
   meeting*. They expired together and should be one card — "Check in: 3 held
   slots released, no reply. Want me to re-offer?" Fanning out per slot means a
   five-slot offer would produce five identical cards.
2. **This is an FYI, and FYIs belong in the brief.** House rule: Teams is for
   decisions Kory has to make. Nothing was asked of him here — the holds were
   already removed. It reads as three notifications about work that is finished.
3. **"From Iconic Founders Group" is a company, not a person.** The requester
   looks to have resolved to the organisation. If someone at IFG asked for the
   check-in, Kory cannot tell who from this card. Worth confirming against the
   originating thread before assuming it is cosmetic.

Not investigated yet — recorded from a screenshot, not the log. Start at
`agent.log` around 11:54 and at whatever emits `hold released (no reply)`.

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

## 1.5 DONE THIS EVENING (HubSpot)

- **`lexi_hubspot_apply_batch`** — the apply path. Enrichment applies the whole
  batch; a merge names one pair and says it is permanent.
- **`lexi_hubspot_undo_batch`** — restores each field's prior value, including
  back to blank. Logged only after HubSpot accepts the write, so the log never
  claims something that did not land. Not repeatable: a second undo would write
  stale values over whatever came after.
- **`is_placeholder` catches the LinkedIn junk.** Anchored to the whole field
  after normalisation — "No Limits Consulting" and "Tbd Ventures LLC" are real
  companies, and marking a real value as junk would make enrichment overwrite
  good data.
- **Phone from signatures**, preferring a labelled mobile or direct line,
  skipping fax and toll-free.
- **`meeting_note` looks the contact up before asking Kory to confirm** — the
  1b defect, fixed here rather than parked.
- **Unresolvable owner ids** no longer read as names; `owner_map` no longer
  caches an empty result forever after one transient failure.
- **Duplicate proposals lead with the person**, not two email addresses.

---

## 1.6 DONE OVERNIGHT (the profile tier, and four defects it exposed)

- **`hubspot_person_lookup.py`** — the corroborated LinkedIn tier described at
  the top, plus `lexi_hubspot_undo_batch(force=…)`.
- **Undo now re-checks before restoring.** Apply always did; undo did not. A
  batch can sit applied for weeks, so restoring blind discards a hand-correction
  and calls it an undo. Fields that no longer hold what we wrote are left alone
  and named; `force=true` overrides. Reading the current state is a *safety*
  step, so if HubSpot cannot be read the undo stops rather than restoring blind.
- **The undo round trip is finally proven against the real portal.** Batch
  `hs-44f5a7df43d0` (Rob Walters → Vessel Advisors, George Song → Strand Equity)
  reverted to blank, verified by an independent read, refused a second revert,
  re-applied, verified again. Net change zero. That was the last unproven claim
  in the safety story.

**Four defects the live rehearsal found that unit tests could not:**

1. **`WM` is Waste Management.** `is_placeholder` treated every two-character
   company value as junk — a comment in the file asserted exactly that — so a
   correct value was being offered for overwrite. Two letters is a real company
   often enough (WM, GE, 3M, EY, BP); punctuation at that length still is not.
2. **Credentials parsed as names.** "CRIS James Hite" and "Jason Buesing PE" are
   James Hite and Jason Buesing. Both were refused as strangers.
3. **Domain suffixes.** `mccombshq.com` is McCombs Enterprises; `imacorp.com` is
   IMA Financial Group. Containment still needs four characters, but exact
   equality now needs three — "ima" *is* the company's whole identity.
4. **Phase 1 ate the whole batch budget** on company-website fetches: twenty
   candidates, seventy seconds, three contacts reaching the inbox. It now gets
   55%, and contacts carrying a LinkedIn URL sort first — one fetch instead of
   three, and they are the placeholder records nothing else can reach.

---

## 2. DONE EARLIER THIS SESSION

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

**New overnight**

- **Composio Search's `FETCH_URL_CONTENT` is backed by Exa, and Exa returns a
  structured *person entity*** — name, location, full work history with titles,
  companies and dates — when handed a LinkedIn profile URL. No auth wall, no
  scraping, already wired up. Lexi could read LinkedIn the whole time; the tool
  description asserting otherwise was the only thing stopping her. Use the
  entity, never the answer-style endpoint: an answer endpoint will compose a
  plausible job title out of nothing, an entity has fields that are present or
  absent.
- **Finding a profile is easy; proving it is the right human is the entire
  problem.** A candidate was found for 8 of 8 contacts probed, and 6 were the
  wrong person or not a person. Kory's book is unusually hostile to name
  matching: two Chris Gavoras, 27 duplicate pairs, shared mailboxes filed as
  people, credentials in the name field, and at least one misspelled surname.
- **HubSpot's own `hs_linkedin_url` can be wrong.** Phil Holland's resolves to
  Brian Holland. Sales Navigator's matching put an Australian software engineer
  on Thomas Heckler's record. Treat a stored URL as a *candidate*, never as
  identity.
- **A rehearsal against live data finds what unit tests cannot.** Intercept the
  write at the lowest layer, run everything above it for real, and print the
  payload. That is how the toll-free phone number surfaced, and later how `WM`,
  the credential names and the domain-suffix misses surfaced — no fixture would
  have contained any of them, because nobody would think to write one.
- **HubSpot will accept a write, return success, and store nothing.**
  `hs_linkedin_url` advertises `readOnlyValue: false` in its own property
  metadata. Writing to it succeeds and the field stays empty — caught only by
  reading the contact back. **Property metadata is a claim, not a fact.** Any
  field added to `SETTABLE_FIELDS` needs a live write-and-read round trip first;
  company, jobtitle and phone have had one.
- **A test can encode the bug.** `is_placeholder("WM", field="company")` was
  *asserted true*, with a comment explaining why a two-letter company name could
  not be real. Kory has a contact at Waste Management. When live data contradicts
  a test, check which one is describing reality.
- **Composio's LinkedIn toolkit has no people search.** 22 tools, all posting,
  ads and org-page stats. `LINKEDIN_GET_PERSON` takes a person id that is
  "unique to the context of your application only". No Sales Navigator API
  exists. Don't go back to this — the Exa route above is the one that works.
- **"Populated" is not "correct".** Every health metric here counted blank
  fields, so a book that is 93% "complete" hid ~30 contacts whose company reads
  `Prefer No Connection to Company`. Measure the junk, not just the gaps.

**Carried forward**

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

`execute_hubspot_batch` now has an MCP tool (`lexi_hubspot_apply_batch`), so the
guard is finally reachable. Merges still apply one pair at a time and refuse a
cross-owner pair without `owner_ack`.

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

**HubSpot: built, rehearsed, apply and undo both proven live, not yet exercised
in Teams.** Reads verified 2026-08-05 and again 08-14. The cleanup path ran
end-to-end against the live book with writes intercepted, and the **undo round
trip has now run against the real portal** — batch `hs-44f5a7df43d0` reverted to
blank, verified by an independent read, refused a second revert, re-applied,
verified again, net change zero.

**Suggested order in Teams**, easiest to hardest:

| Say | Exercises |
|---|---|
| *"Which of my contacts are missing information?"* | health report — read-only, no risk |
| *"Find duplicate contacts"* | duplicate scan + the owner labels on his own data |
| *"Fill in what's missing on my contacts"* | the four enrichment tiers, evidence lines, the findings that are not fills |
| *"Apply those"* | the write path, the ownership guard, the undo log |
| *"Undo that"* | the restore, including the re-check that leaves edited fields alone |
| *"Jeremy Boka's company is Sustainable Sites"* | `set_field` — answering the scan's own question |
| merges | **last**, one pair at a time, and permanent |

Watch for two things specifically. The evidence line under each fill should name
where the value came from — a profile URL and the employer it was matched on, or
the message a signature was read from. And a batch that reports contacts
remaining should make progress when he says *keep going*; a round that repeats
the same names is the spinning bug returning.

**Scheduling: mid-test**, paused by Anjana. Resume at Group C of
`docs/SCHEDULING_LIVE_TEST_PLAN.md`. Still untested: meeting types
(coffee/drinks/lunch venue vs Teams link), hard blocks (before 08:30, Doug Mon
13:15–14:15), timezone handling, reschedule, and the full offer → reply → book
lifecycle.

**Watch for:** any "no availability" claim, and a slot offered inside a hard
block. The first is where confident wrongness hides.
