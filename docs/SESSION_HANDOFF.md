# Lexi + CEO Dashboard — Session Handoff (updated 2026-08-03)

**Resume phrase:** *"continue the scheduling test plan at Group C."*

Two repos, one box:
- **Lexi** `~/AI_Scheduling_Agent` → `/home/lexi/AI_Scheduling_Agent` (services `lexi-hermes` **and** `lexi-api`)
- **Dashboard** `CEO_Executive_Dashboard--main/` (its own git repo, gitignored by the outer one) → `/opt/ceo-dashboard` (service `ceo-dashboard`, branch `deploy-prep-phase1`)

`srv1686061.hstgr.cloud` is **multi-tenant** — never reboot it, never restart another tenant's units. The co-tenant's "Hermes" is a **Docker container** (`hermes-agent-teuw-hermes-agent-1`, UID 10000, `/opt/hermes`, ports 8642/32768); Lexi's is a **systemd unit**. They share only the name. `systemctl restart lexi-hermes` cannot touch it — different user, different ports, EPERM. Confirm after any restart by checking the container's uptime is unchanged.

**Deploy with `scripts/deploy_lexi.sh`** — it restarts **both** services. `lexi-api` is a separate uvicorn on `:8081`; restarting only `lexi-hermes` leaves `api_v1.py` changes serving stale code, so a new endpoint 404s and looks undeployed.

---

## 1. WHERE THE TEST PLAN STANDS (`docs/SCHEDULING_LIVE_TEST_PLAN.md`)

**Phase 0 preflight — 8/8 ✅. Group A (ingestion/triage/notify/dedupe) — 5/5 ✅. Group B (delegation) — 4/4 ✅.**
Roughly 17 of ~60 tests done. Sends stayed CLOSED throughout; **approve has still never been tapped.**

**Next: Group C** (slot proposal accuracy) — the largest untested group, pure draft inspection, and the direct beneficiary of the window work below. Then S, L, O-draft, I, J/K, G. Full remaining outline is in the test plan.

### Three blockers still gating Phase 3
1. **Teams card buttons are dead.** Now *the* blocker — every approve/confirm/edit in Groups D/E/H is a card tap. Unchanged. Agreed direction: `LEXI_TEAMS_TEXT_ONLY=true` + execution-backed confirmations. **Not implemented.**
2. **A plain "Reply" is invisible.** Mail goes out from lexi@, so a normal Reply lands only in Lexi's mailbox, which nothing ingests (`LEXI_POLL_LEXI_MAILBOX=false`, deliberately — a Lexi-mailbox trigger reintroduces the Graph-id 404s fixed in `17e9043`). Guests must Reply All; they won't. Affects every Group H test.
3. **Stale conclusions without calling a tool.** SOUL.md rules added; never stress-tested on the scheduling path.

### Open decisions, Kory's/yours
- **Ladder distance cap.** A request for "the week of the 17th" is currently answered with **September 8** — the ladder tries +1w/+2w/+3w and takes the first that fits, with no notion of "too far to be useful". Options: leave it (Kory rejects the card), cap at +2w and escalate beyond, or always ask when it fires. Recommendation: cap at +2w.
- **OB-3** — `priority@example.com` / "Demo placeholder priority contact" is still in the production `priority_contacts_config`. The priority-contacts feature is effectively unconfigured.
- **Subject-over-body intent weighting.** B-3's "Coffee or a call" classified as coffee at 0.72 confidence despite the body saying "30 minutes… call". Body should probably win for meeting *format*.
- **Dashboard briefing line.** Lexi's `/api/v1/unanswered-scheduling` is live; the dashboard side is deliberately deferred until Groups B/C are through (it needs a new API client, a rebuild, and a live-service swap).

### Deferred by decision
**Group N** — email as a general command channel (11 tests). Capability largely unbuilt; CG-1…CG-4 documented in the plan. Direction settled if resumed: in-repo LLM tool loop over a read-and-stage tool subset, with a hard sender allowlist evaluated *before* the model sees any text.

---

## 2. CURRENT PRODUCTION STATE

```
LEXI_ENV=production           LEXI_WRITE_MODE=kory        LEXI_DRY_RUN=false
LEXI_KORY_OUTBOUND_BLOCKED=true     ← CLOSED for the test window (Phase 2 re-opens)
LEXI_REQUIRE_KORY_APPROVAL=true     ← nothing sends without approval
LEXI_ASANA_LIVE_WRITES_ENABLED=false     ← OFF for the test window (was true)
LEXI_HUBSPOT_LIVE_WRITES_ENABLED=false   ← OFF
LEXI_HUBSPOT_BCC_ENABLED=false           ← OFF until the scheduler is done
LEXI_TEAMS_INBOUND_NOTIFY_MODE=delegation_and_followups  ← reverted from `important`
LEXI_SIGNATURE_EMBED_LOGO=true           ← was pinned false, overriding the code default
LEXI_TEAMS_TEXT_ONLY=false               ← still using cards (see blocker 1)
LEXI_ORCHESTRATOR_BACKUP_POLL_MINUTES=5  ← test-window value (was 30)
```
**451 Lexi tests pass** (~4s). Prod HEAD `6cbc108`.

**Test-window deviations to revert at sign-off:** `LEXI_ASANA_LIVE_WRITES_ENABLED` back to `true`, `LEXI_HUBSPOT_BCC_ENABLED` back to `true`, `LEXI_ORCHESTRATOR_BACKUP_POLL_MINUTES` back to `30`. Backups on the box: `.env.bak.phase0.20260803-140555`, `.env.bak.deploy.*`.
398 Lexi tests pass (~4s, nothing deselected). Dashboard: `tsc`, `eslint`, `next build`, read-only guard, `npm run test:email` all clean.

---

## 3. WHAT SHIPPED THIS SESSION (2026-08-03)

**The sign-off/logo change finally deployed** (`f2e5ed4`). Prod `.env` pinned `LEXI_SIGNATURE_EMBED_LOGO=false`, which beat the new code default — deploying the code alone would not have shown the logo. Verified in the prod environment: sign-off passes the verify gate, one inline `cid:ifg-logo.png` attachment, `needs_draft=True`. Only the real-inbox render is left (Group O, Phase 3).

**Teams stopped notifying on cold inbound** (`5934c49`). Reverted `important` → `delegation_and_followups`. The principle: *Teams is for decisions only Kory can make.* A cold-inbound card is duplicate signal (the mail is already in his inbox), carries no action Lexi can complete alone, and — measured — produced four real cards in fourteen minutes. That spends card-fatigue budget on the same channel that carries approvals, which are the one thing that genuinely blocks Lexi. Verified in the live flow: `Auto-skipped … delegation_and_followups_cold_inbound` on Anjana's cold mail, then `Posted Lexi approval card` only once Kory's CC reply delegated it. Added read-only `/api/v1/unanswered-scheduling` so aged asks can be batched into the morning brief instead (dashboard side deferred).

**The B-1 defect chain — a request for "the week of the 10th" answered with Aug 18 / Aug 26 / Sep 2, while the gate reported the slots "match requested window".** Seven linked causes, each of which looked correct alone:

1. `infer_scheduling_window` understood only *relative* phrases — every calendar date (`week of the 10th`, `August 10-14`, `August 12`, `next Tuesday`) returned `None`, which the engine reads as "no constraint" over a 60–120 day horizon. (`d22cd20`)
2. The window was only enforced when it hung off `plan.window`, but the engine infers its own whenever the plan lacks one — unchecked on exactly the path that parsed it. (`d22cd20`)
3. `schedule_from_context` set `window_expanded=True` *because* slots fell outside the window, converting a violation into an accepted expansion. (`d22cd20`)
4. The gate claimed *"match requested window"* unconditionally whenever there were no warnings — asserting a check it never ran. **This is why the defect survived earlier runs.** (`d22cd20`)
5. **The actual root cause:** `"check-in"` was a travel keyword, so `IFG + Sujash | Check-in (Mon+Wed+Fri)` (26 events) and the biweekly check-ins were travel — **33 of 41** travel-classified events, **36 travel days**, blanketing the requested week so the shift logic (correctly) moved past it. Narrowed to `hotel/flight check-in`. **41 → 10 events, 36 → 8 days.** (`f694a86`)
6. A *second* window override: `propose_meeting_slots` walks +1w/+2w/+3w/no-window when the requested week yields <2 slots, and suppressed the gate's window check. Not blocked — Kory has ~1 coffee slot a week — but now surfaced as a gate warning on the card. (`3a9c2e9`)
7. That ladder was a **no-op**: it strips the window from the plan, but `find_valid_slots` re-infers one from the email text. Fixed with an `open_horizon` marker (`c1cd259`) — and even then the ladder searched blind, because `resolve_calendar_horizon_days` trims the horizon to the window end (22 days), so +1w/+2w/+3w had no calendar data. Added three weeks of headroom (`378e9c2`). **This last one was a regression I caused**: before the parser understood calendar dates, the trim never applied.

**Names have one source of truth** (`c735240`). A Teams escalation reached Kory titled `(Anjanakummetha)` while the profile store held "Anjana Kummetha" — because **four** independent "name from email" implementations existed (`teams_format`, `teams_labels`, `briefings`, `introducer`). Collapsed onto `display_name_for_email`. **`calendar_title` and `email_format` still carry their own copies — likely E-9's cause.**

**Coffee stays coffee** (`c1cd259`). The escalation model was improvising "ask if a call works instead". Kory's coffees are with people he already knows and must be booked as coffee; the prompt now rules out proposing a different meeting type. The **90-minute reserve was kept deliberately** — Monday Aug 10's only 60-minute gap ends exactly when a hard block starts, and being late back from an in-person meeting is a worse failure than offering fewer times.

## 4. BUGS WORTH REMEMBERING — nearly all were confident wrong answers, not errors

- `normalize_due_on` threw **today's date a year forward** (MT vs local across midnight) — with Asana writes ON.
- Pipeline `isClosed` arrives as the **string** `"false"`, which is truthy in Python.
- HubSpot stage-history requests **cap at 50 objects**; asking for 100 failed and was swallowed into "no deals moved" (there were 97).
- Signature parsing pulled **Kory's own quoted footer** out of reply chains onto other people's records.
- `today` showed **6:30 AM as 12:30 AM** — Graph sends a naive dateTime beside its zone, already converted; the code assumed naive meant UTC and converted twice. Same command printed attendees as **raw Graph dicts** (a regression from selecting attendees for the prebrief).
- **Two correct-looking fixes can cancel each other.** Teaching the window parser calendar dates made `resolve_calendar_horizon_days` start trimming the horizon, which blinded the widening ladder. The symptom survived two rounds of fixes because each layer looked right in isolation.
- **A gate that always claims success is worse than no gate.** `"...and match requested window"` was emitted unconditionally, so out-of-window offers passed review for weeks.
- **Duplicated helpers drift.** Four copies of "name from email" meant fixing one fixed exactly one, and the wrong one reached Kory's card.
- **`journalctl -u lexi-hermes` returns nothing** — the service logs to `logs/lexi.log`. Date-filtering it with `awk '$0 >= "<ts>"'` silently pulls in old tracebacks, because continuation lines carry no timestamp. Use `awk '/^<date>/{f=1} f'`.
- **Verify the rendered output, not the internal string.** I logged a defect for an escalation message leaking engine diagnostics; that string is internal and is reformatted before Teams. The message Kory actually received was fine.
- A **correct, tested tool the model never calls is not a working feature** — "tell me about X" went to inbox search until the tool was renamed and given trigger phrasings.

---

## 5. DEPLOY MECHANICS

**Lexi — use the script; it restarts BOTH services and backs up `.env` + the DB first:**
```bash
ssh -i ~/.ssh/lexi_vps_ed25519 root@srv1686061.hstgr.cloud 'bash -s' < scripts/deploy_lexi.sh
```
`lexi-api` is a separate uvicorn on `:8081`. Restarting only `lexi-hermes` leaves `api_v1.py` changes serving stale code — a new endpoint 404s and looks undeployed. The script also prints the co-tenant container's uptime so you can confirm it was untouched.

**Dashboard** (from `CEO_Executive_Dashboard--main/`, node 20):
```bash
export PATH=/Users/anjanakummetha/.nvm/versions/node/v20.20.0/bin:$PATH
rm -rf .next && node_modules/.bin/next build
rsync -az --exclude '.env*' --exclude 'data/' -e "$SSH" .next/standalone/ root@…:/opt/ceo-dashboard/
rsync -az --delete                          -e "$SSH" .next/static/    root@…:/opt/ceo-dashboard/.next/static/
ssh … 'chown -R ceo:ceo /opt/ceo-dashboard/.next && systemctl restart ceo-dashboard.service'
```
⚠️ The standalone rsync runs **without `--delete`** (so it can't wipe `data/` or `.next/static`) — **deleted files must be removed on the box by hand**. A stale compiled route survived a deploy this way.

---

## 6. KEY COMMANDS

```bash
# Lexi
.venv/bin/python -m pytest tests/ -q -p no:cacheprovider
.venv/bin/python scripts/morning_briefing_email.py --dry-run     # briefing without sending
systemctl list-timers lexi-morning-briefing.timer
journalctl -u lexi-morning-briefing.service -n 50

# Dashboard
npm run test:no-write-slugs && npm run test:email
```

---

## 7. STANDING RULES

- Never touch Kory's real surfaces without asking. Sandbox/Lexi-mailbox writes are pre-authorised.
- **HubSpot stays off the dashboard** — it's on his larger IFG board. Don't re-propose it.
- **Don't sync meeting data** between the dashboard and Lexi; one owner per capability.
- Asana writes stay on **Kory NON-IFG**; reads span all projects; changing someone else's task needs an acknowledgement naming them.
- **Verify against the live API, not the chat.** Almost every defect this month produced a plausible wrong answer rather than an error.
- Secrets that transited chat still need rotation. Local `.env` Anthropic keys are stale — prod has the working ones.
