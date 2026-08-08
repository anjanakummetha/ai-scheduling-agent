# Kory Preferences vs. Code — Audit

Status 2026-08-08: ALL THREE sweeps complete (time/day rules; blocks/holds/reschedules;
meeting types/venues — the paused sweep was finished today). A fix batch shipped as
`0c3fd56`; see "2026-08-08 RESOLUTION" at the bottom for what was fixed vs. what
remains a Kory/Anjana decision. Headline flags below are the ORIGINAL Aug-5 findings —
several are now fixed; the resolution section is authoritative.

## Headline flags (things that disagree with Kory's written preferences)

1. **CONTRADICTED — Tue/Thu 7:00/8:00 AM are routine, not occasional.** slot_engine adds
   07:00+08:00 to every Tue/Thu candidate list unconditionally; rules.py declares
   `earliest_default: "09:00"` for Tue/Thu but NO code reads that key.
2. **CONTRADICTED — the "7:00 floor" ruling can block 6:00 AM East-Coast starts.** When a
   time-of-day window is parsed from the email ("we could do 6 AM ET"), scheduling_plan/
   scheduling_window clamp to 7:00, making the 6 AM Tue/Thu East-Coast lane unreachable.
   Without a stated time preference, 6 AM works. V-1 ruling vs. preference conflict — needs Kory.
3. **CONTRADICTED — "no minimum notice" isn't true.** Hard 2-hour lead PLUS the candidate
   loop starts at day_offset=1, so TODAY is never offered. Effective minimum = next day.
4. **MISSING — weekend family-calendar exception.** Weekends are correctly blocked by default,
   but the exception path (weekend OK only during a window when Bridget/Maclain are busy —
   check family calendar first) does not exist. Worse: DINNER intents are exempt from the
   weekend block entirely, with no family check. Family calendar read only ingests
   "Do Not Move" events as busy blockers — ordinary family events are invisible.
5. **MISSING — 30-min-Teams batching.** Selector deliberately picks one slot per day and
   prevents adjacency; "batch 30-min Teams back-to-back, max 2h block" has no implementation.
   The 2h-chain/30-min-break check exists but only for virtual informal intents, only against
   existing calendar events (never across offered slots — `all_batch` is a dead variable).
6. **PARTIAL — drive-time rules mostly unimplemented.** Only Cherry Creek 15 min is enforced.
   Downtown/DTC/Littleton/DEN times, the DEN leave-45 rule, and calls-during-drives exist
   only as prompt prose. (TRAVEL_TIMES consulted nowhere else.)
7. **DEFECT — reschedule prospects never get the follow-up reminder.** Reschedule holds
   expire at 1 day (correct) but the reminder is computed on the 2-day default and the
   release stamps the hold `released`, which the reminder query excludes. Net: reminder
   can never fire for reschedules; only Kory gets a Teams notice.
8. **DEFECT — outbound-initiated offers place NO holds.** `_insert_outbound_holds` in
   outbound_agent.py is dead code (zero call sites). Inbound offers place holds after
   send (all-or-nothing); outbound path doesn't.
9. **MISSING — reschedules are not prioritized.** Queue is priority_tier then FIFO;
   nothing bumps intent == "reschedule". Prompt-only.
10. **PARTIAL — Capital Demolition.** The 7:00 Thursday rule blocks EVERY Thursday
    (bi-weekly not modeled = over-blocking, safe direction), and the current real 8:00 AM
    occurrence is only protected when the calendar event exists.
11. **PARTIAL — soft blocks (Patrick/WOB/3PM review) protected only when the event exists
    on the calendar; SOFT_BLOCKS data feeds prompts only. Effectively as hard as hard
    blocks — no "ask Kory to move it" workflow exists in code (prompt-only). Agent has no
    capability to move existing meetings at all (update_calendar_event: zero call sites).
12. **PARTIAL — urgent exception.** The "never move YPO/boards/Doug/workouts/CapDemo/HRT"
    guarantee holds. But urgent=True also relaxes lunch, travel days, and 6 AM — broader
    than the stated "ask me to move other meetings" intent; the "within a week" scope and
    the ask-Kory flow are unmodeled.
13. **CAVEAT — Friday hold sweep fires at ANY hour Friday** (not end of day), and silently
    (no Kory/prospect notification). A Wednesday-offered hold for next week can vanish
    Friday morning ~2 days after offer.
14. **NOTE — M/W/F formats.** 8:00 vs 9:30 split enforced, but ambiguous emails default to
    "virtual", so an undeclared in-person could slip into the 8:00 lane. Untested.
15. **NOTE — many rules.py keys are decorative** (read by nothing): Tue/Thu DAILY_AVAILABILITY,
    SOFT_BLOCKS timings, default_buffer, wob/inbox-counts-as-break flags, TRAVEL_TIMES (except
    Cherry Creek), DRIVE_TIME_RULE, HARD_NO/YES_DEFAULTS, URGENT_EXCEPTION. The 6 PM cutoff
    works but is a hardcoded 18, not read from rules.py.

## Confirmed working (deterministically enforced, with evidence)
- Trainer M/W/F 6:30–8:00; Doug Mon 1:15; travel-day ban; weekend default-ban; 6 PM
  non-dinner cutoff + happy-hour 6 PM end + nothing-after-happy-hour; lunch exception-only;
  coffee 60-min offer + 90-min reserve + no-following-meeting; no default buffer otherwise;
  weekly caps (HH 2/dinner 1) exist in validators; 6 AM gated to Tue/Thu East-Coast/urgent;
  morning-preference scoring; no daily cap (as desired); YPO/boards/HRT/drop-off/pick-up/
  "Do Not Move" block WHEN the event is on a read calendar (+ unknown events block by
  default — safe); hold all-or-nothing on inbound offers; reminder at 2 days; release at
  3 days (1 day for reschedules); Friday next-week sweep exists; nobody books without
  approval (gates verified); no VIP auto-yes (priority contacts only raise triage priority).

## Still to audit (paused)
~~Meeting-type sweep~~ — COMPLETED 2026-08-08, findings folded into the resolution below.
(Also corrected: flag 8 was stale — outbound offers DO place holds via the shared
send_offer approval path; only the auto-execute branch, env-gated off, skips them.)

## 2026-08-08 RESOLUTION (fix batch `0c3fd56`; meeting-type sweep completed)

### Fixed in code
- **Warnings now render in Teams** (was: E-6 clash remedy, hold-placement alerts composed
  then dropped — nothing read `ExecutionResult.warnings`). Highest-leverage fix.
- **pending_invite holds no longer age out** — prospect-picked slot stays protected until
  Kory confirms; the false "no reply" notice for prospects who DID reply is gone.
- **Friday sweep waits for 5 PM MT** (was 00:00 Friday) and the release notice states the
  real held days (1 for reschedules).
- **Re-remind on release** implemented — expiry stages the prospect follow-up draft for
  approval (`stage_release_followups`); `re_remind_on_release` was config-only before.
- **Reschedules**: 2 options (was 3), jump the approval queue within tier (was FIFO),
  booked-meeting reschedules keep `intent='reschedule'` → 1-day holds (was 3).
- **Venues**: invite Location now "Name (street address)" per Heidi's convention
  (`VENUE_ADDRESSES` — addresses need one-time verification, only Aviano St. Paul is
  confirmed from a real invite); happy-hour annotation no longer leaks; Quality Italian
  gated behind its 4:00 opening (`VENUE_OPENS_AT`).
- **Durations**: stated 45-min intro honored (was cut to 30); approved lunch books 60
  (was 30 via fall-through); "coffee before the recording" is coffee, not podcast.
- **Coffee mornings protected**: only an explicit afternoon mention widens past 8:30/9:00
  ("flexible"/"either" no longer do).
- **Titles**: "Kory/Matt" only when the thread says Matt joins (matches attendee logic).
- **Copy**: offer email no longer claims a 30-min calendar block that doesn't exist.
- **Happy hour**: warning when Kory already has a later same-evening commitment
  (warn-not-block — the later event may be personal/family).
- Asana venue tasks carry "Request a bar booth"; canonical dinner/lunch intents included.
- Silent non-send hole closed (missing thread_id → needs_kory escalation).
- Dead `_insert_outbound_holds` deleted; `rules.py` docstring now says which keys are
  prompt-only.

### RULED 2026-08-08 (via Anjana) — and implemented same day
1. **Urgent keyword self-service — RULED: escalate, never auto-relax.** Urgency no longer
   bypasses lunch/travel-day/6 AM anywhere; validators warn "no rules were auto-relaxed",
   and an urgency-flagged request that fails the rules now calls `escalate_to_kory` with
   the exception offer (guidance reply → `kory_scheduling_guidance` → re-run). The
   ask-Kory-to-MOVE-a-meeting flow still doesn't exist (`update_calendar_event`: 0 sites).
2. **Tue/Thu early starts — RULED: only when the contact's schedule needs it.** 7:00/8:00
   candidates appear only for East-Coast senders or a stated early window; 6 AM is
   East-Coast-only; the 7:00 floor drops to 6:00 when an East-Coast cue is present, so
   "6 AM ET works" is finally reachable (amends ruling V-1).
3. **30-min coffee ask — RULED: keep booking 60** ("they run long — give them the room").
4. **Same-day scheduling — RULED: keep next-day earliest** ("no minimum notice" just
   means no multi-day lead requirement).
5. **Matt on coffees — RULED: not by default**; only with an explicit "Matt will join".
6. **Coffee buffer — RULED: stays invisible**; Lexi must simply never book the 30 min
   after a coffee (current behavior correct).

### Still open — needs Kory/Anjana decision (deliberately NOT changed)
1. **Weekend family-calendar exception** (flag 4) — family busy-read was ruled OUT
   (kory-scheduling-rulings); weekends stay blocked, dinner exemption stands. Revisit only
   if Kory wants weekend scheduling.
2. **30-min-Teams batching** (flag 5) — still unimplemented (selector prefers one slot/day).
3. **Drive-time rules** (flag 6) — only Cherry Creek 15-min enforced; DTC/Littleton/DEN
   pads and calls-during-drives remain prompt-only.
4. **Dinner/happy-hour same-evening stacking** (pref) — nothing searches for the stack;
   only permitted, not preferred, in code.
5. **Ask-Kory-to-move-a-meeting flow** — the urgent ruling routes exception requests to
   Kory, but Lexi still cannot MOVE an existing meeting (`update_calendar_event`: 0 sites).
6. **Buffer/coffee protection is subject-regex based** — a coffee titled without the word
   "coffee" gets no post-coffee buffer protection.
7. **Venue addresses** in `rules.py` `VENUE_ADDRESSES` — one-time human verification
   (only Aviano on St. Paul confirmed from a real Heidi invite).
