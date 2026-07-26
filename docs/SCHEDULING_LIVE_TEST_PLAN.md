# Lexi Scheduling — Comprehensive Live Test Plan

**Goal:** fully validate the scheduling feature end-to-end against real inbound email (sent from `anjanakummetha@gmail.com` to Kory's inbox), real Composio reads of Kory's Outlook/calendar, and the live Teams approval loop — then finalize the feature and re-open approved sends permanently.

**Tester setup:** Anjana sends test emails from `anjanakummetha@gmail.com` → `kory.mitchell@iconicfounders.com`, and has access to Kory's Outlook and the Lexi/Hermes Teams chat. Production box: `ssh root@srv1686061.hstgr.cloud` (multi-tenant — never force-restart the VPS, never touch Sujash's containers).

**Ground rules for every test:**
- Run tests **serially, one thread at a time**, each with a **unique subject** (e.g. `Coffee chat — LT-C1`) so DB rows, Teams cards, and log lines are unambiguous.
- Orchestrator cycle is 30s and webhook delivery adds latency — allow **~1–2 minutes** after each email before judging "nothing happened."
- Asana and HubSpot are **Kory's REAL accounts** — live writes must stay `false` throughout (reads/staging only).
- After every phase, record evidence (§ Evidence kit) and mark the tracker (§ Tracker).

---

## 0. Decision points — resolve BEFORE testing

These are product/config decisions the tests depend on. Confirm with Kory (or decide) first:

| # | Decision | Why it matters | Recommendation |
|---|---|---|---|
| D-1 | **Cold-inbound Teams notifications.** Current `LEXI_TEAMS_INBOUND_NOTIFY_MODE=delegation_and_followups` means a scheduling email to Kory that does NOT CC lexi@ is triaged and staged but **never pings Teams** (`app/agents/inbound_filter.py:151`). The stated desired behavior — "Kory gets a notification when a scheduling email comes in" — requires widening this. | Half the requested flow (notify → Kory decides) is invisible under the current mode. | Set `LEXI_TEAMS_INBOUND_NOTIFY_MODE=important` for the test window; keep or revert based on Kory's noise tolerance. |
| D-2 | **HubSpot BCC during testing.** Prod has `LEXI_HUBSPOT_BCC_ENABLED=true`. Every Lexi email to an outside address (Anjana's gmail) will BCC `242757246@bcc.na2.hubspot.com` and **log test emails as real activity in Kory's HubSpot**, likely creating a contact for anjanakummetha@gmail.com. | Pollutes real CRM data. | Set `LEXI_HUBSPOT_BCC_ENABLED=false` for the test window; re-enable at sign-off. Alternatively accept + delete the contact/activities afterwards. |
| D-3 | **When to re-open sends.** `LEXI_KORY_OUTBOUND_BLOCKED=true` today. Phase 1 runs closed; Phase 2+ needs it `false`. | Nothing sends/holds until flipped. | Flip only after Phase 1 passes (procedure in §Phase 2). |

---

## Phase 0 — Preflight (read-only, 15 min)

All on the box unless noted. Every item must pass before Phase 1.

| ID | Check | How | Pass criteria |
|---|---|---|---|
| P0-1 | Service health | `curl -s http://127.0.0.1:8780/api/health` | `ok`; `systemctl status lexi-hermes` active |
| P0-2 | Safety posture | `cd /home/lexi/AI_Scheduling_Agent && LEXI_ENV=production .venv/bin/python -c "import app.config as c,json;print(json.dumps(c.safety_posture_summary(),indent=2))"` | `WRITE_MODE=kory`, `DRY_RUN=false`, `OUTBOUND_BLOCKED=true` (for now), `REQUIRE_KORY_APPROVAL=true`, auto-execute/immediate-send false, Heidi escalation false, Asana/HubSpot live writes false |
| P0-3 | Webhook ingress live | Anjana sends a throwaway email to Kory; `journalctl -u lexi-hermes --since "-5 min" \| grep -i webhook` | Webhook event received + enqueued (not just backup poll) |
| P0-4 | Calendar reads | Via Hermes/Teams: ask Lexi to list calendars (or `lexi_list_calendars` tool) | "Kory Master Calendar (ALL)" + "Calendar" + family calendar readable. Note which of the optional group calendars (IFG Team, Kory & Heidi only, Deal Activity, Daily CEO Update) are still missing (known gap B-01) — record, since missed conflicts trace back to this |
| P0-5 | Teams roundtrip | In Teams: `help`, `today`, `pending` | Sensible responses; `today` matches Kory's actual Outlook calendar for today (spot-check 2–3 events + times in MT) |
| P0-6 | DB backup fresh | `systemctl list-timers \| grep lexi` and check newest file in backup dir | Backup < 2h old (rollback insurance before we start writing) |
| P0-7 | Baseline DB snapshot | `sqlite3 /home/lexi/AI_Scheduling_Agent/data/lexi.db "select max(id) from proposals; select max(id) from holds; select max(id) from audit_log;"` | Record the high-water marks — everything created during testing is above these IDs (makes cleanup exact) |
| P0-8 | Apply D-1/D-2 decisions | Edit `.env`, `systemctl restart lexi-hermes.service`, re-run P0-1/P0-2 | Posture reflects decisions; health ok |

---

## Phase 1 — Closed-posture tests (sends still BLOCKED — zero external writes)

Everything here is safe: ingestion, triage, delegation detection, slot computation, draft quality, cards, escalation, memory. Approving a send is expected to be **blocked** — that's itself a test (S-1).

### Group A — Ingestion, notification & triage

| ID | Scenario | Steps | Expected |
|---|---|---|---|
| A-1 | Cold scheduling email notifies Kory | Anjana → Kory, subject `Intro call? — LT-A1`, body asking for 30 min next week. No CC. | Per D-1 decision: with `important` mode, Teams ping/card appears; proposal staged `awaiting_reply_prompt` or drafted. Nothing auto-sends. |
| A-2 | Non-scheduling email stays quiet | Anjana → Kory, subject `Article you might like — LT-A2`, no ask. | Triage → `no_reply_needed` (or no proposal). **No** Teams noise. |
| A-3 | Duplicate delivery dedupe | After A-1 settles, forward/resend the identical email (same thread if possible); also note webhook + backup poll overlap. | Exactly one proposal for the thread (`_thread_already_ingested` / `_thread_has_active_proposal`). No duplicate cards. |
| A-4 | Ingress fallback | (Optional) During a quiet moment note a `backup poll` log line. | Backup poll runs on its 30-min cadence without creating duplicates of already-webhooked mail. |
| A-5 | Kory-inbox reality check | While tests run, watch for REAL inbound to Kory. | Real senders are triaged correctly and (critically) **nothing is sent to a real contact** during the whole test window. Any card for a real thread: leave un-actioned or `reject #N — testing`. |

### Group B — Delegation (the primary flow)

| ID | Scenario | Steps | Expected |
|---|---|---|---|
| B-1 | Kory replies + CCs Lexi (Sent-folder detection — Rung-1 fix (d)) | Anjana emails Kory (`LT-B1`). From Kory's Outlook, reply to Anjana, CC `lexi@iconicfounders.com`: "Looping in Lexi to find us a time." | Delegation detected from Kory's reply (inbox AND sent-items copy → still ONE proposal). Draft is **to Anjana** (greets her by name), Lexi voice, approval card in Teams. No phantom "Heidi has been flagged" text anywhere (Rung-1 fix (c)). |
| B-2 | Sender CCs Lexi directly | Anjana → Kory with CC lexi@ on a fresh thread `LT-B2`, "Would love 30 min with Kory." | Same delegation path; counterpart = Anjana (not Kory, not Lexi). |
| B-3 | Delegation phrasing without CC | From Kory's Outlook, reply on a thread: "My assistant Lexi will send times" (no CC). | Delegation still detected via phrase match. |
| B-4 | Follow-up on tracked thread | On the `LT-B1` thread, Anjana sends another message ("bump — any times?") before approval. | No duplicate proposal; existing staged proposal reopened/refreshed (`_handle_delegation_followup`), Teams reflects it. |

### Group C — Slot proposal accuracy (inspect the DRAFT, nothing sends yet)

For each case read the draft in the Teams card and cross-check against Kory's **Master Calendar (ALL)** in Outlook.

| ID | Scenario | Expected |
|---|---|---|
| C-1 | Standard ask (30-min virtual, `LT-C1`) | **2–3 slots**, every one genuinely free on Master + work Calendar + family calendar; earliest ≥ now+2h; all within working hours; dates/day-of-week/times internally consistent (say the slot out loud vs a real calendar). |
| C-2 | Hard blocks | Ask for "early morning" meetings: no virtual slot before **8:00** on Mon/Wed/Fri (trainer), no in-person before 9:30 those days; nothing Mon 1:15–2:15 (Doug). |
| C-3 | Lunch exception-only | Ask "lunch sometime?" (`LT-C3`): no lunch slot proposed by default — expect escalation/alternatives instead (ties to I-1). |
| C-4 | Requested-window respect | "Tuesday afternoon or Wednesday next week?" → slots actually fall in that window. Also re-verify the Rung-0 finding: a travel-shift/window conflict should produce a partial offer or an escalation with reasoning — not a silent over-defer. |
| C-5 | Vague day/period reply — Rung-1 fix (b) | "How about sometime Thursday?" → no **fabricated conflict** claim; Lexi either offers valid Thursday times or says Thursday is genuinely full (verify against the real calendar). |
| C-6 | Meeting-type shaping | "Coffee near Cherry Creek?" → 8:30/9:00-style starts, 90-min block, in-person location default. Optional: happy hour / dinner asks respect weekly caps. |

### Group G — Timezone (draft-level, no send needed)

| ID | Scenario | Expected |
|---|---|---|
| G-1 | Unknown TZ → MT default, disclosed | Plain gmail with no signature (`LT-G1`): draft quotes times in **Mountain Time and says so explicitly** (with ET/CT/PT equivalents), per the unknown-TZ rule. No silent Eastern assumption. |
| G-2 | TZ from signature | Anjana adds a fake signature block: "New York, NY \| (212) 555-0100" (`LT-G2`): draft quotes **Eastern first, MT in parentheses**. |
| G-3 | TZ learned + reused from history | After G-2, start a NEW thread from the same address with no signature: TZ should come from `recipient_profiles` / prior-thread scan — still Eastern-first. `sqlite3 … "select * from recipient_profiles where email like '%anjana%';"` shows the learned TZ. |

### Group I — Escalation to Kory (works closed — it's a Teams message)

| ID | Scenario | Expected |
|---|---|---|
| I-1 | No compliant slots | Ask for something impossible (e.g. "only free weekdays 6–7 AM" or lunch per C-3, `LT-I1`). | Teams message to Kory naming the actual blocker with 2–3 concrete options / a question — not a generic defer, **no Heidi mention** (escalation is Heidi-disabled). Proposal → `needs_kory` / `needs_scheduling_guidance`. |
| I-2 | Kory guidance unblocks | Reply in Teams with guidance ("offer 7 AM Tuesday, it's fine"). | Scheduler retries **with the guidance applied**; new draft/card honors it. |

### Groups J/K — Email-to-Lexi commands & Remember

| ID | Scenario | Expected |
|---|---|---|
| J-1 | "Don't schedule with X" | From Kory's Outlook, email lexi@ directly: "Don't schedule anything with anjanakummetha@gmail.com." | Ack; memory fact saved (`sqlite3 … "select * from kory_memory;"`). Then Anjana sends a new ask → Lexi does NOT propose times (staged/declined path), surfaces it to Kory instead. **Then remove the fact** (J-4) before continuing other tests. |
| J-2 | "Remember that …" via email | Kory → lexi@: "Remember that I'm OK with lunch meetings on Fridays." | Fact upserted; a following Friday-lunch ask now offers lunch (preference override wired through `load_scheduling_preferences`). |
| J-3 | "Remind me to …" | Kory → lexi@: "Remind me to review the term sheet Friday." | Routed to the Asana/task intent, **staged only** (Asana live writes disabled) — verify no real Asana task was created. |
| K-1 | Remember via Teams | In Teams: "remember: no meetings before 9 AM on Tuesdays." | Fact stored; next proposal on a Tuesday respects it. Then test **updating** the same rule ("actually 8:30 is fine") → fact updated, not duplicated. |
| K-2 | Memory survives restart | `systemctl restart lexi-hermes.service`; re-check facts + a fresh proposal. | Facts persist (SQLite) and still apply. |

### Group L — Kory-voice drafting (draft-level)

| ID | Scenario | Expected |
|---|---|---|
| L-1 | Draft in Kory's voice on request | In Teams: "draft a reply to Anjana about the intro call in my voice." | Draft card in **Kory's voice**, sign-off "Let's Win" (not bold), never "Best/Warmly/Regards", no YPO mention. Editable like any card. |
| L-2 | Default voice is Lexi | Confirm all delegation/offer drafts from A–C signed as Lexi, Lexi voice. | No accidental Kory-voice on auto-drafts. |

### Group S — Safety while closed

| ID | Scenario | Expected |
|---|---|---|
| S-1 | Approve is blocked while closed | On a staged card (`LT-C1`), tap Approve / `approve #N`. | Send **refused** by the outbound gate with a clear message; proposal not stuck in a broken state; NO email in Anjana's inbox, NO hold on the calendar. |
| S-2 | Audit trail | `sqlite3 … "select action,log_level,created_at from audit_log where id > <P0-7 mark> order by id;"` | Every action above audited; no `TypeError`s in `journalctl` (regression check on `a9da8d2`). |

**GATE:** All of Phase 1 passes (or failures are understood + fixed + re-run) → proceed.

---

## Phase 2 — Re-open approved sends

1. `ssh root@srv1686061.hstgr.cloud`
2. `sed -i 's|^LEXI_KORY_OUTBOUND_BLOCKED=.*|LEXI_KORY_OUTBOUND_BLOCKED=false|' /home/lexi/AI_Scheduling_Agent/.env`
3. Confirm `LEXI_KORY_SPACE_READ_ONLY=false` (holds must be able to land), `LEXI_REQUIRE_KORY_APPROVAL=true` still set.
4. `systemctl restart lexi-hermes.service` → re-run P0-1/P0-2.
5. Sanity: `LEXI_ALLOW_IMMEDIATE_SEND=false`, `LEXI_AUTO_EXECUTE_ENABLED=false` — approval remains the only path to a send.

---

## Phase 3 — Live E2E (sends OPEN, everything goes to Anjana's gmail)

### Group D — Approval UX (edit / send / disregard)

| ID | Scenario | Expected |
|---|---|---|
| D-1 | Edit → send | New ask `LT-D1` → card → **edit the draft text** in the card (add a marker sentence like "P.S. see you soon"), Save, then Approve. | Email arrives at Anjana's gmail containing the **edited** text; Lexi voice; sign-off correct. |
| D-2 | Send + hold atomicity — Rung-1 fix (a) | On approve: check calendar + DB immediately. | Offer email sent AND 2–3 **tentative holds** land on the work "Calendar" (never Master), AND proposal → `offer_sent` — all together; a re-tap of Approve does NOT double-send (idempotency guard). |
| D-3 | Disregard | New ask `LT-D3` → `reject #N — not now`. | Nothing sent, no holds, status `rejected`, audit row with the reason. |
| D-4 | Bare `send` | With exactly one pending approval, type `send`. | Resolves to that proposal and sends. With 2+ pending, asks which. |
| D-5 | CC & headers on the sent mail | Inspect the D-1 email in Anjana's gmail (Show original). | **CC rule:** Kory CC'd only when he's not already on the thread (delegation reply where he's a participant → no duplicate CC; cold offer → CC `Kory.Mitchell@iconicfounders.com`). HubSpot BCC per D-2 decision. From = Lexi mailbox. |

### Group E — Holds lifecycle

| ID | Scenario | Expected |
|---|---|---|
| E-1 | Holds visible & correct | After D-2, in Kory's Outlook: holds show as **tentative**, on the work Calendar, titles/timezones sane, exactly matching the offered slots. |
| E-2 | Confirm → cleanup | (After H-1 acceptance) invite confirmed: chosen slot's tentative hold replaced by the **real event**; the OTHER holds are removed. |
| E-3 | 3-day no-reply reminder | Ignore an offer thread (`LT-E3`). Fast-forward instead of waiting: `sqlite3 … "update holds set created_at = datetime(created_at,'-2 days'), expires_at = datetime(expires_at,'-2 days') where proposal_id=<N>;"` then wait a cycle. | **Before** release: Teams reminder card with a drafted nudge to Anjana, requiring Kory approval (notification-before-action requirement). Approving sends the follow-up. |
| E-4 | Expiry release + notify | Push the same holds past expiry (`-4 days`). | Holds deleted from Outlook; Teams note "hold released (no reply)"; status consistent. |
| E-5 | Friday next-week cleanup | (Observational / optional) If a test spans Friday, confirm the weekly cleanup only touches stale next-week holds. |

### Group H — Recipient replies & counter-proposals

| ID | Scenario | Expected |
|---|---|---|
| H-1 | Accepts an offered slot | Anjana replies "Tuesday 9 works." | Slot matched → `pending_invite` → **invite card** to Kory; on approve, calendar event created with Anjana as attendee. |
| H-2 | Teams meeting link | For a virtual meeting, the confirmed event is an **online Teams meeting** (link present on the invite Anjana receives). In-person variant: location populated (Cherry Creek default or stated venue). |
| H-3 | Counter-proposes a FREE time | Anjana: "None of those work — could we do Thursday at 2?" (pick a genuinely free time). | Validated against calendar + rules → invite card for Thursday 2 (no re-offer loop). Kory approves → event. |
| H-4 | Counter-proposes a BUSY time | Anjana proposes a time that's booked. | No fabricated availability: Lexi finds compliant same-day alternatives and **asks Kory in Teams** ("they asked for a booked time; you're open at X — offer those?"). Never auto-books over a conflict. |
| H-5 | Rejects all → re-offer | Anjana: "None of these work, following week?" | Holds released, status `pending_reoffer`, fresh compliant slots in the requested window → new approval card → send → new holds. |
| H-6 | Vague reply mid-thread | Anjana: "Maybe later in the week?" | Handled gracefully (clarify or offer late-week slots) — again no invented conflicts (Rung-1 fix (b) under real send conditions). |
| H-7 | Thread context retention | In her reply, Anjana references earlier details ("like I said, I'm near downtown Denver"). | Later drafts reflect thread history (location/TZ/context carried, not re-asked). |

### Group M — Background jobs & ops (observational, during the window)

| ID | Scenario | Expected |
|---|---|---|
| M-1 | 24h Kory nudge | Leave one card unactioned ~24h (or backdate `teams_approval_notified_at`). | One nudge in Teams, not spam. |
| M-2 | 4:45 AM MT briefing | Next morning. | Briefing arrives once, mentions the test threads appropriately. |
| M-3 | Stability | `journalctl -u lexi-hermes --since "-1 day" \| grep -iE "error|traceback" \| grep -v <known-noise>` | No new tracebacks; watchdog + hourly backup timers still firing; health `ok` throughout. |
| M-4 | Composio budget | Check `composio_call_daily` table after the window. | Call volume sane (webhook-driven, not runaway polling). |

---

## Phase 4 — Fix → re-test → sign-off

1. Log every failure in the tracker with: test ID, expected vs actual, `journalctl` excerpt, relevant `proposals`/`holds`/`audit_log` rows, Teams screenshot.
2. Fix on a branch (`bugfix/live-scheduling` pattern), run the local suite (30/30 + bugfix tests), PR → merge → deploy per the handoff procedure (stash voice profile → ff-merge → restart → health).
3. **Re-run the failed test AND its group** after each deploy (regressions cluster within a group).
4. Sign-off = all P/A–M tests green in one continuous posture, then finalize `.env` (D-1 notify mode decision made permanent, HubSpot BCC re-enabled if it was disabled, sends stay open).

### Cleanup checklist (after sign-off)
- [ ] Delete all `LT-*` holds/events from Kory's calendar (query `holds` above the P0-7 mark for event ids).
- [ ] Close/reject any leftover test proposals; clear test rows only if desired (audit trail is fine to keep).
- [ ] Remove test memory facts (`kory_memory`): the J-1 don't-schedule fact, K-1 rule if not wanted, etc.
- [ ] Delete the HubSpot contact/activities for anjanakummetha@gmail.com if BCC was left on.
- [ ] Re-verify final posture (P0-2) and record it in `SESSION_HANDOFF.md`.
- [ ] Rotate the secrets that transited chat (still pending from the handoff).

---

## Evidence kit (copy-paste)

```bash
# health + posture
curl -s http://127.0.0.1:8780/api/health
cd /home/lexi/AI_Scheduling_Agent && LEXI_ENV=production .venv/bin/python -c "import app.config as c,json;print(json.dumps(c.safety_posture_summary(),indent=2))"

# live logs while testing
journalctl -u lexi-hermes -f

# DB state (adjust the id from P0-7)
DB=/home/lexi/AI_Scheduling_Agent/data/lexi.db
sqlite3 $DB "select p.id,p.status,t.subject,p.voice_mode,p.is_delegation,p.recipient_timezone from proposals p left join email_threads t on t.thread_id=p.thread_id where p.id > <MARK> order by p.id desc;"
sqlite3 $DB "select id,proposal_id,event_id,slot_start,slot_end,expires_at from holds where id > <MARK>;"
sqlite3 $DB "select id,step_name,log_level,message,timestamp from audit_log where id > <MARK> order by id desc limit 40;"
sqlite3 $DB "select fact_key,fact_value,source from kory_memory;"
sqlite3 $DB "select * from recipient_profiles;"
```

---

## RUN 1 RESULTS — 2026-07-26 (sends CLOSED)

Config applied: `LEXI_HUBSPOT_BCC_ENABLED=false` (D-2 decision), notify mode left `delegation_only` (D-1 decision: cold inbound intentionally silent). `.env` backup: `.env.bak.testwindow.20260726-183148`. Baseline marks: proposals>3379, holds>0, audit>15723.

**Phase 2 NOT executed — blocked by D-A below.**

| Test | Result | Evidence |
|---|---|---|
| P0-1/2/6/7 | ✅ | health ok; posture correct; backups hourly; marks recorded |
| P0-3 webhook | ✅ | `webhook_ingress` audit rows on inbound |
| P0-5 Teams | ✅ | `help`, `today` respond |
| P0-4 calendars | ⚠️ | 2 consulted (Master + Calendar); **4 still unavailable** (IFG Team, Kory & Heidi only, Deal Activity, Daily CEO Update) — known B-01 |
| A-1 cold inbound | ✅ (by design) | proposal 3380 → `no_reply_needed`, `inbound_auto_skip (delegation_only_mode)` |
| A-3 dedupe | ✅ | `already ingested` + `Conversation … already tracked` both fired |
| B-1 delegation | ⚠️ works, **~20 min late** | reply landed 18:42:50; only ingested at 18:50 after poll forced. Draft correct: to Anjana, Lexi voice, 3 slots, signature ok |
| C-1 slots valid | ✅ | Aug 4 9/10/11 AM all genuinely free |
| C-4 requested window | ❌ **D-C** | asked "next week" (Jul 27–31); offered Aug 4 while *saying* "next week" |
| G-1 TZ | n/a | TZ was **learned** (`prior_email_area_code`→Denver), not the unknown path — retest with a fresh address |
| S-1 approve blocked | not run | deferred — bogus cards present |

### Defects found

**D-A (BLOCKER) — Composio `OUTLOOK_LIST_MESSAGES` silently ignores `filter`.**
Verified: requesting `conversationId eq '<X>'` returned 10 messages, **1** matching. `$filter`, `filter`, `filter_query`, `query` all ignored — no server-side filtering exists. Consequences:
1. `outlook_thread.py:36-47 _list_conversation_messages` → `fetch_conversation_context` stitches **4 random recent unrelated emails** into every `[Prior messages in this email chain]` block. Every triage + draft is contaminated.
2. Those contaminated bodies inherited "Looping in Lexi" from the test thread → `detect_delegation` `phrase_match`+`from_kory` → **5 false-positive delegation auto-drafts with Teams approval cards** (proposals 3381-3385) targeting real cold outreach contacts (YPO/podcast prospects) who never requested a meeting.
3. `outlook_email.py:176-203` anchor lookup uses the same broken filter. Mitigated by the `intended_recipient in to_emails` check (`_pick_lexi_delegation_anchor:131`) so it won't reply to the wrong *person*, but it can still anchor to the **wrong thread** for the same person.
**Fix:** filter client-side by `conversationId` after fetch in both call sites (Composio can't do it server-side).

**D-B (HIGH) — delegation replies wait up to 30 min.** The `OUTLOOK_MESSAGE_TRIGGER` webhook is registered on **Kory's connection only**, and his reply lands in Sent Items + Lexi's inbox — neither fires it. So the primary flow depends on `LEXI_ORCHESTRATOR_BACKUP_POLL_MINUTES=30`. **Fix:** register a trigger on Lexi's mailbox (and/or shorten backup poll).

**D-C (HIGH) — over-defers past the requested window.** "Next week" = Jul 27–31 had large compliant openings (Tue Jul 28 11:00–14:00 free; Mon Jul 27 08:00–09:00; Wed Jul 29 08:30–10:00 and 12:00–14:30). Thu/Fri were legitimately blocked ("Kory in CA"). Lexi skipped to Aug 4 — the last day of its 9-day horizon — while the email said "next week". Matches the known Rung-0 travel-shift over-deferral.

**D-D (MEDIUM) — greeting uses email local-part:** "Hi Anjanakummetha," because `recipient_profiles.display_name` is empty. Should parse the sender's display name / first name.

**D-E (LOW) — all 3 slots on one day** (Aug 4 9/10/11). Spread across days so one bad day doesn't kill the offer.

### Fix + deploy status (2026-07-26 19:2x)
PR #5 merged → `main` (`db373ed`), deployed to VPS, health ok, posture unchanged (**sends still CLOSED**). 313 tests pass.
- D-A **fixed + verified live**: same conversation read now returns 1 message / 0 leaks (was 10 / 9 leaks); thread context contains no unrelated mail. Also fixed the second half — delegation phrases now match only the sender's *new* text, so quoted history can't re-delegate.
- D-B **partially fixed**: Lexi's mailbox is now polled (delegation CCs land there), but it runs on the same backup cadence, so latency is governed by `LEXI_ORCHESTRATOR_BACKUP_POLL_MINUTES` — set to **5** for the test window (was 30). The real fix is registering `OUTLOOK_MESSAGE_TRIGGER` on Lexi's Composio connection; revisit the cadence before leaving it permanently (5 min ≈ 60k Composio calls/month against a 200k budget).
- D-C **latent hole fixed** (window no longer dropped when a plan carries none) — but whether that was *the* live cause is unconfirmed; re-test required.
- D-D **fixed**: Outlook display name recorded at ingest and preferred; run-together mailboxes fall back to "there".
- D-E **not fixed** (slots still may cluster on one day).

Cleared for re-test: proposals 3380-3385 all set to `rejected` (0 pending), and the learned `recipient_profiles` row for anjanakummetha@gmail.com deleted so the **unknown-TZ path (G-1)** can be exercised from the same address.

---

## RUN 2 RESULTS — 2026-07-26, after fixes (sends still CLOSED)

Deployed `main` @ `991979d`. Proposal **3395** is the live artifact (Teams card refreshed).

| Test | Result | Evidence |
|---|---|---|
| D-A false delegations | ✅ **fixed at scale** | 13 of Kory's sent emails ingested → all `no_reply_needed`, `is_delegation=0`. Zero cards. Body for 3395 is 82 chars, no stitched-in mail. |
| D-A conversation scoping | ✅ verified live | same read: 1 message / 0 leaks (was 10 / 9) |
| B-1 delegation | ✅ | one proposal, counterpart = Anjana, Lexi voice, card posted 19:45:44 (reply 19:40:45 → **~5 min**, = poll cadence) |
| D-B latency | ⚠️ 5 min, cadence-bound | Lexi-mailbox poll reverted to opt-in (caused 404s — Graph ids are mailbox-scoped; see `17e9043`). Kory's Sent Items is the ingress. Durable fix = register `OUTLOOK_MESSAGE_TRIGGER` on Lexi's connection. |
| **D-C over-deferral** | ✅ **fixed** — root cause found | Diagnostics showed the window replaced by `source="travel_shift"` → `week of August 3 (after travel)`. One travel day shifted the whole week. Now: window stays `next week`, slots **Tue Jul 28** 7:00/8:00/11:00, all free on the real calendar. Also fixed `travel_date_set` only marking a multi-day trip's first day. |
| D-D greeting | ✅ fixed | "Hi Anjana," — `display_name` "Anjana Kummetha" learned from Outlook |
| Slot ordering | ✅ fixed (new) | offer read "11:00, 7:00, 8:00" (score order) → now chronological |
| G-1 unknown TZ | ⚪ **not testable from this address** | Resolver returns `America/Denver`, confidence `inferred`, source `prior_email_header` — correctly derived from the sender's own Date offset. Needs a sender with no TZ signal (e.g. M365 rewriting offsets to +0000). |
| D-E slot spread | ❌ open | all three slots still land on one day (Tue Jul 28) |

**New open question for Kory:** the engine offered **7:00 AM** to an outside contact. Legal under the rules (trainer blocks only M/W/F) but aggressive for an intro — consider a per-intent earliest-hour floor for external meetings.

Tests: **315 pass**. Commits: `37aceb9`, `17e9043`, `8a0a55c`, `991979d`.

---

## RUN 3 — day spread + travel over-block (2026-07-26, sends still CLOSED)

Deployed `main` @ `f066583`. **322 tests pass.**

The Run-2 offer still put all three options on Tuesday. Cause was *not* the selection logic — Mon and Wed had **no valid candidate at all**, rejected by the `travel_day` validator. Two genuine bugs:

1. **`"check-in"` counted as a travel hint** (`validators.py:109`, meant for *hotel* check-in) → Kory's recurring **"IFG + Sujash | Check-in (Mon+Wed+Fri)"** marked Mon and Wed as travel days. **Wednesday had no travel event whatsoever.**
2. **Any travel event blocked its whole day** → a **21:05 flight** to Sioux City ruled out an **8:00 AM** meeting 13 hours earlier.

Now: a discrete trip leg blocks only a 3h buffer around itself; an all-day/6h+ block ("Kory in CA - All Day") still blocks the whole day; unparseable times stay conservative. Also fixed `used_days` being populated but never read in the selection loop (dead code), so offers prefer one slot per day and only double up when a single day is all that's open.

**Live result** (regenerated 3395 against the real calendar):
`Mon Jul 27 8:00` · `Tue Jul 28 11:00` · `Wed Jul 29 9:00` — three days, chronological, all free, in-window, Thu/Fri correctly skipped.

---

## ⚠️ STILL TO VERIFY — nothing below has been exercised live

### Blocking-ish / needs a decision from Kory
| # | Item |
|---|---|
| V-1 | **7:00 AM offered to an outside contact** is legal (trainer blocks are M/W/F only). Want a per-intent earliest-hour floor for external meetings? |
| V-2 | **4 conflict calendars still unavailable on Composio** (IFG Team, Kory & Heidi only, Deal Activity, Daily CEO Update). Conflicts living only there are invisible to every slot we offer. Known B-01. |
| V-3 | **Travel-week policy.** `rules.py` says travel weeks = "2–3 critical check-ins only". My fix makes non-travel days in a travel week bookable. Confirm that matches Kory's intent. |
| V-4 | **Poll cadence** is 5 min (was 30) ≈ 60k Composio calls/mo against a 200k budget. Durable fix: register `OUTLOOK_MESSAGE_TRIGGER` on Lexi's connection (needs dashboard access). |

### Never tested — requires sends OPEN (Phase 3)
- **D-1** edit draft in card → send · **D-2** send+hold atomicity + no double-send · **D-3** disregard · **D-4** bare `send` · **D-5** CC/BCC on real sent mail (Kory CC'd only when not already on thread)
- **E-1** holds land as tentative on the work Calendar (never Master) · **E-2** confirm → other holds removed · **E-3** 3-day reminder card *before* release · **E-4** expiry release + notify · **E-5** Friday next-week cleanup
- **H-1** accepts an offered slot → invite · **H-2** Teams meeting link on the invite · **H-3** counter-proposes a free time · **H-4** counter-proposes a busy time (must ask Kory, never auto-book) · **H-5** rejects all → re-offer · **H-6** vague mid-thread reply · **H-7** thread-history retention
- **M-1** 24h Kory nudge · **M-2** 4:45 AM MT briefing · **M-3** multi-day stability · **M-4** Composio budget after a full window

### Never tested — possible while sends stay CLOSED
- **S-1** approve while blocked is refused cleanly (we never tapped approve)
- **I-1** escalation when nothing fits → Teams message naming the blocker with options, no Heidi mention · **I-2** Kory replies with guidance → scheduler retries with it applied
- **J-1** email lexi@ "don't schedule with X" · **J-2** "remember that…" changes future scheduling · **J-3** "remind me to…" stages an Asana task without a real write
- **K-1** `remember` via Teams + updating an existing rule · **K-2** memory survives a restart
- **L-1** Kory-voice draft on request (sign-off "Let's Win", no YPO) · **L-2** auto-drafts stay Lexi-voice
- **C-2/C-3/C-6** hard blocks, lunch exception-only, coffee/happy-hour/dinner shaping — only C-1/C-4 exercised
- **A-2** a genuine non-scheduling *inbound* stays silent (only verified on Kory's own sent mail)
- **G-1** unknown-TZ disclosure — **not testable from anjanakummetha@gmail.com** (its own Date header reveals MT, confidence `inferred`). Needs a sender with no TZ signal.
- **One clean fresh-email round** with all fixes active from triage onward — the current 3395 draft was regenerated manually, not produced by a new inbound.

### Housekeeping
- `test_daily_briefing_includes_asana` takes **~69s** and looks like it hits the network; it makes the suite 15s → 75s+ and is a CI fragility risk. Pre-existing, not from these changes.
- Test-window `.env` deviations to revert at sign-off: `LEXI_HUBSPOT_BCC_ENABLED=false`, `LEXI_ORCHESTRATOR_BACKUP_POLL_MINUTES=5`. Backup: `.env.bak.testwindow.20260726-183148`.
- Secrets that transited chat still need rotation (carried over from the prior handoff).

### Left in place for inspection
Proposals **3381-3385** are `pending_approval` with live Teams cards (false positives from D-A). Sends are blocked so they are inert, but they must be rejected before sends re-open.

---

## Tracker

| ID | Status (pass/fail/blocked/n-a) | Notes / defect link |
|---|---|---|
| P0-1 … P0-8 | | |
| A-1 … A-5 | | |
| B-1 … B-4 | | |
| C-1 … C-6 | | |
| G-1 … G-3 | | |
| I-1 … I-2 | | |
| J-1 … J-3 | | |
| K-1 … K-2 | | |
| L-1 … L-2 | | |
| S-1 … S-2 | | |
| D-1 … D-5 | | |
| E-1 … E-5 | | |
| H-1 … H-7 | | |
| M-1 … M-4 | | |

**Known pre-existing gaps to keep in view (not new defects):** B-01 missing optional group calendars on Composio (conflicts only as good as the calendars readable); bi-weekly Capital Demolition relies on calendar merge, not validators; weekly happy-hour/dinner caps are soft.
