# Teams-Chat Full Read/Write Verification Plan

**Created 2026-08-08. Status: PLAN ONLY — nothing executed yet.**

Goal (per `SESSION_HANDOFF.md` §0 + Anjana's directive): verify **every read and every
write the Teams chat can perform** works correctly, end-to-end, against the real
surfaces (Kory's actual Outlook mailbox + calendar, real Asana workspace, real HubSpot
portal) — while guaranteeing **no real change is visible to anyone Kory actually knows**.
Also includes the one queued feature: **Kory can write to ANY Asana project** (currently
scope-guarded to the "Kory NON-IFG" board).

Tool surface: 86 registered MCP tools in `hermes_mcp_server.py` (campaign tools stay
unregistered — parked).

---

## 1. Containment rules (how writes stay invisible to real people)

These are hard constraints on every test in this plan:

1. **Only external party is Anjana.** Every outbound email, calendar invite, and
   scheduling counterparty is `anjanakummetha@gmail.com` (with `+tag` aliases where a
   distinct "person" is needed, e.g. `anjanakummetha+jordan@gmail.com`). No test
   artifact is ever addressed to, CC'd to, or shared with anyone in Kory's real network.
2. **No Kory-CC on test sends.** Prod email rules CC `Kory.Mitchell@iconicfounders.com`
   on outbound. For [TEST] sends we verify the CC *logic* fires but the CC target for
   the test run must not spam Kory — first test send is inspected in **drafts/preview**
   before anything releases; if the CC can't be redirected cleanly, [TEST]-prefixed
   subjects make anything he might glimpse self-explanatory, and we sweep them in
   cleanup. (Decision item D-2 below.)
3. **`[TEST]` prefix on everything**: email subjects, calendar events/holds, Asana task
   names, HubSpot note bodies, memory facts. Nothing test-created exists without the
   prefix.
4. **Asana writes only in "Anju - CEO executive tools"** — members are only Anjana,
   Heidi, Kory. That project is both the sandbox for generic Asana CRUD *and* the
   target for proving the new any-project feature (it is not the NON-IFG board, so a
   successful write there proves the guard was lifted). Real IFG projects: reads only.
5. **HubSpot is one shared portal** (owner id is a property, not a partition): test
   contact = realistic-looking name + `anjanakummetha+hs@gmail.com`, created →
   verified → **archived in the same session** (recycle bin; **never** GDPR-delete).
   Notes/associations only on that test contact.
6. **Calendar writes land on Kory's real calendar** (that's the point — verifying the
   real surface) but: [TEST]-prefixed, placed in slots that his real rules permit
   (never over lunch/workout/weekend), invitee only Anjana's gmail, and **deleted with
   verification (0 remaining) before the session ends** — same discipline as RUN 14's
   E-4 check.
7. **Don't touch live state:** proposal **6218** (real, awaiting Kory), the 27
   Aug-3 staged-ask threads, any real triaged email. Never approve old proposal
   numbers; never re-approve after a timeout without checking the DB first.
8. **Teams pings:** interactive-card and command tests run in the **test/Anjana
   conversation**, not Kory's chat. `lexi_escalate_to_kory` genuinely pings Kory —
   see decision D-1 before running it.
9. **Prod box etiquette:** Graph is already throttling the poll — keep test volume
   low and spaced; never reboot `srv1686061`; never touch co-tenant containers;
   deploys only via `scripts/deploy_lexi.sh`.

## 2. Realism rules (tests mirror Kory's actual workflow)

Every scenario is something Kory would actually do, on plausible dates (week of
Aug 10–21, 2026, America/Denver):

- **Scheduling asks** look like his real inbound: a YPO-forum member asking for an
  intro coffee; a referral ("Travis said we should connect") wanting 30 min
  (coffee still books 60 — ruling #8); an East-Coast contact needing an early call
  (Tue/Thu 7:00 only on an East-Coast cue — ruling #7); an "urgent" ask that must
  escalate rather than bypass gates (ruling #6); a lunch ask that must route to Kory,
  never auto-book (ruling #2).
- **Slot expectations** verified against his real rules: 8:00 virtual / 9:30
  in-person floor after the trainer block, no weekends, no visible coffee buffer but
  nothing within 30 min after a coffee, Matt not defaulting onto coffees.
- **Asana tasks** phrased as his real usage (personal/exec follow-ups, e.g.
  "[TEST] Send Q3 planning pre-read", due dates this/next week).
- **HubSpot flows** mirror real use: look up a contact before a meeting, log a
  meeting note after, prebrief enrichment for tomorrow's meetings.
- **Briefs** are judged for accuracy against the *real* calendar/inbox content they
  summarize — that's the read verification.

## 3. Verification standard (what "verified" means)

A tool passes only when **both** of these hold:

1. **Tool-level:** the MCP tool returns the correct result (shape + content), invoked
   the way the Teams chat invokes it.
2. **Ground truth:** the effect/content is confirmed on the actual surface —
   - Outlook: message/event visible in the real mailbox/calendar (Graph read or
     Anjana's gmail for the receiving side), sent-items checked for sends.
   - Asana: task state confirmed via a direct Asana read (and spot-check in the UI).
   - HubSpot: confirmed via `HUBSPOT_READ_CONTACT` (the only true by-id read — search
     indexes lag) and portal spot-check.
   - DB/audit: `data/lexi.db` proposal rows + `decision_source`; `logs/lexi.log`
     entries (never journalctl).
3. Writes additionally require **cleanup verified** (artifact gone / archived, 0 residue).

Defects found during execution get fixed as found (standing rule), logged in
`SCHEDULING_LIVE_TEST_PLAN.md` OPEN ISSUES, with regression tests where practical.

---

## 4. Phases

### Phase 0 — Preflight (read-only)
- Restart CLI session if needed so `.claude/settings.json` ssh/scp allowlist is live.
- `scripts/go_live_readiness.py` (read-only, ~15s) + `curl http://127.0.0.1:8780/api/health`.
- Confirm prod flags unchanged (writes ON, BCC off, campaigns off, approval required).
- Snapshot baselines: DB proposal counts, Composio usage %, outlook_poll error count
  (also serves item "verify the stagger" from the open work list).
- Check whether Kory has interacted since 08-08 03:55 (resume-phrase step) — if he
  has, log his change requests first; they take priority over this sweep.
- Note suite state: 626 with 3 known stale-data failures (aging out; not blockers).

### Phase 1 — FEATURE: Asana any-project writes (build → test → live-verify)
Build first per handoff §0. Not started until this plan is approved.
1. **Design decision to record (D-3):** what "any project" means for the approval
   gate — proposal: writes allowed to any project Kory can access, but the existing
   approval requirement stays uniform, and destructive ops (delete, complete, move)
   keep the same gating as today; the scope guard in `app/integrations/asana_manager.py`
   becomes advisory context in the approval card ("target: <project>") instead of a
   hard block.
2. Code change + unit tests (extend the existing manager tests; keep suite green).
3. Deploy via `deploy_lexi.sh`.
4. **Live-verify in "Anju - CEO executive tools" only:** create → read-back → update
   (due date, notes) → comment → move (section/board) → complete → delete, each
   verified in Asana directly. A write landing there proves the NON-IFG-only guard is
   lifted, with zero exposure beyond Anjana/Heidi/Kory.
5. **Negative checks:** reads of real IFG projects still correct; no test writes
   attempted outside the Anju project.

### Phase 2 — Read sweep (no side effects; ~40 tools)
Grouped; each group cross-checked against ground truth:

| Group | Tools | Ground-truth check |
|---|---|---|
| Mail reads | `search_inbox`, `get_thread`, `get_thread_context`, `inbox_review`, `unanswered_brief`, `get_inbound_reply_queue` | Compare ids/counts/senders against the real mailbox via Graph; queue vs DB rows |
| Calendar reads | `today_calendar`, `list_calendars`, `get_calendar_availability`, `summarize_calendar_window`, `check_time_slot`, `find_slots`, `find_meeting_times`, `get_family_calendar_status` | Real calendar for the same window; hard-block conformance (trainer floor, lunch, weekends); family reader returns Do-Not-Move only |
| Asana reads | `list_asana_projects`, `list_asana_boards`, `list_asana_tasks`, `search_asana_tasks` | Direct Asana comparison; ownership filter correct (9-bug history here — recheck the silent-wrong-answer cases from the 07-28 findings) |
| HubSpot reads | `hubspot_status`, `hubspot_find_contacts`, `hubspot_recent_changes`, `hubspot_deals_snapshot`, `hubspot_compare_books`, `hubspot_health_report`, `hubspot_duplicate_merges` (read/report mode), `lookup_person`, `hubspot_outreach_candidates` | Known real contacts via true by-id read; remember EQ filters are instant, free-text lags minutes |
| Briefs | `today`, `prebrief`, `meeting_brief`, `precall_brief`, `hubspot_prebrief_enrich` | Content accuracy vs the real calendar/inbox they summarize; prebrief scope = new people + research (dashboard owns the morning brief) |
| Research | `research_person`, `web_search`, `search_flights`, `search_hotels`, `search_maps`, `search_news`, `fetch_url_content`, `recipient_timezone` | Sanity of results; research target = a public figure or Anjana, not a real contact of Kory's |
| System/session | `get_system_status`, `get_scheduling_context`, `get_scheduling_session`, `get_lexi_pending_queue_tool`, `get_pending_decisions`, `list_kory_memory`, `lexi_list_calendars` | vs DB + health endpoint; pending queue must show 6218 and nothing test-created yet |

### Phase 3 — Write sweep (containment rules §1 apply to every item)

**3a. Email lifecycle** (`begin_draft_reply`, `draft_reply_for_email`,
`draft_outbound_email`, `preview_scheduling_email`, `send_outbound_email`):
- Anjana sends a realistic scheduling request from gmail → confirm triage picks it up.
- Draft path first: inspect draft for Lexi voice, greeting, non-bold sign-off, CC
  behavior **before any send** (D-2 gate).
- One live send to `anjanakummetha@gmail.com` with `[TEST]` subject → verify arrival
  in gmail + presence in sent-items → sweep in cleanup.

**3b. Full scheduling lifecycle** (`start_scheduling`, `find_slots`,
`preview_schedule`, `propose_schedule`, `validate_slots`, `validate_scheduling_cases`,
`upsert_scheduling_session`, `place_calendar_hold`, `update_proposal_draft`,
`retry_scheduling`, `begin_reoffer`, `decline_inbound_reply`, `execute_outlook_action`):
- One end-to-end [TEST] run: inbound ask from gmail → slots (verify rule-conformant)
  → holds placed (verify on real calendar) → typed `approve #N` → offer lands in
  gmail → Anjana replies accepting → confirm → invite arrives in gmail → then
  **cancel** → invite retracted, holds deleted, verify 0 [TEST] events remain.
- Branch tests on separate [TEST] proposals: counter-offer/reoffer, decline, retry,
  and the E-6 conflict-at-confirm guard (create a clash, confirm must refuse, holds
  kept).
- Reminder/expiry (`create_reservation_reminder` + E-3/E-4 behavior) re-verified only
  if touched by recent fixes; otherwise cite RUN 14.

**3c. Calendar direct ops** (`accept_calendar_invite`, `decline_calendar_invite`,
`add_conflict_calendar`):
- Anjana sends a [TEST] invite from gmail to Kory's mailbox → accept via tool →
  verify on calendar → decline/cleanup a second one → both deleted at the end.
- `add_conflict_calendar`: add + verify + **remove** (restore config exactly).

**3d. Asana CRUD** — already fully exercised in Phase 1 (Anju project). Re-run only
`comment` + `update` here through the *Teams command path* (3f) to prove chat-level
parity.

**3e. HubSpot writes** (`hubspot_meeting_note`, `hubspot_enrich_contacts`,
`hubspot_cleanup_proposals`; contact create via the standard flow):
- Create test contact (realistic name, `anjanakummetha+hs@gmail.com`) → verify via
  true by-id read → meeting note with correct `hs_note_body`/`hs_timestamp`/
  `associations` shape → verify note on portal → enrich → verify → **archive contact,
  verify archived**.
- `hubspot_duplicate_merges`/`cleanup_proposals`: run in report/propose mode only —
  no merges executed against real records.
- Payload shapes validated against Composio `input_parameters`, never HubSpot docs.

**3f. Teams surface** (`register_teams_conversation`, `handle_teams_command`,
`handle_teams_card_submit`, `approve_decision`, `modify_and_approve_decision`,
`reject_decision`, `execute_lexi_approval_tool`):
- In the test conversation: command roundtrip, interactive card render + submit,
  typed approval, modify-and-approve, reject — each against **[TEST] proposals only**.
- `escalate_to_kory`: see D-1 — default is to verify routing in logs without a live
  ping, unless Anjana okays one labeled ping.

**3g. Memory** (`remember_kory_fact`): store one obviously-[TEST] fact → `list` shows
it → remove it (restore-SQL pattern from RUN transcripts) → list confirms gone. (The
Tuesday-8:30 artifact from last time is the cautionary tale.)

### Phase 4 — Cleanup + residue audit (blocking; session doesn't end without it)
- Delete every [TEST] calendar event/hold → count-verify 0 (Outlook, both mailboxes).
- Sweep [TEST] emails + Lexi drafts from Kory's and Lexi's mailboxes; delete the
  gmail-side copies is optional (Anjana's own inbox).
- Anju-project test tasks deleted (or one kept only if Anjana wants a demo artifact).
- HubSpot test contact archived (verified), zero test notes on real records.
- Test memory facts removed; DB [TEST] proposals closed with audit entries
  (`cleanup_stale_test_threads.py` pattern); queue back to 6218-only.
- Re-run `go_live_readiness.py`; compare baselines from Phase 0.

### Phase 5 — Report
- Pass/fail matrix for all 86 tools appended to `SCHEDULING_LIVE_TEST_PLAN.md` as a
  new RUN; defects → OPEN ISSUES (fixed-as-found noted with commits).
- Update `SESSION_HANDOFF.md` §0 + memory. Feed anything Kory-visible (e.g. new
  Asana scope) into his change-request log/user guide.

---

## 5. Decision items — ALL RESOLVED by Anjana 2026-08-08

- **D-1 — `lexi_escalate_to_kory`:** live escalation ping approved (Anjana operates
  Kory's account/Teams herself). Send one clearly-[TEST] labeled escalation.
- **D-2 — Kory CC on [TEST] sends:** keep the CC — [TEST]-prefixed copies land in
  his mailbox and get swept in Phase 4 cleanup. No CC-suppression code.
- **D-3 — Any-project Asana approval semantics:** Phase 1 proposal confirmed —
  uniform approval requirement, target project named on the approval card,
  destructive-op gating unchanged.
- **D-4 — Teams card tests location:** **Kory's chat** — Anjana tests logged in as
  Kory; there is no separate conversation. Disciplines: (a) minimal cards — each
  card *type* exercised once, everything else via typed commands (cards are
  permanent; activity ids were never stored); (b) all [TEST]-labeled plus a closing
  housekeeping note marking the sweep's cards inert; (c) proposal hygiene — 6218 is
  live in that same chat; only [TEST] proposal numbers ever get approved, DB-checked
  first.

## 6. Explicitly out of scope for this sweep
Outreach campaigns (parked), HubSpot BCC (parked, broken HubSpot-side), emailing
lexi@ ingress (scrapped), morning briefing content M-2 (Anjana's separate check),
M-3 redo, per-tool deadline work, HubSpot sign-off package — all remain on the open
work list in `SESSION_HANDOFF.md` §3.
