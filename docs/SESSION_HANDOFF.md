# Lexi + CEO Dashboard — Session Handoff (updated 2026-07-30)

**Resume phrase:** *"test the scheduling email + Outlook features end to end."*

Two repos, one box:
- **Lexi** `~/AI_Scheduling_Agent` → `/home/lexi/AI_Scheduling_Agent` (services `lexi-hermes`, `lexi-api`)
- **Dashboard** `CEO_Executive_Dashboard--main/` (its own git repo, gitignored by the outer one) → `/opt/ceo-dashboard` (service `ceo-dashboard`, branch `deploy-prep-phase1`)

`srv1686061.hstgr.cloud` is **multi-tenant** — never reboot it, never restart another tenant's units.

---

## 1. NEXT UP — scheduling email / Outlook features

This is the priority and the only thing between Lexi and daily use. **Lifetime proposals: `no_reply_needed` 4,242 · `rejected` 6 · `executed` 1.** Triage works; the execution half has never had one clean loop.

### Three blockers, in order
1. **Teams card buttons are dead.** The Hermes gateway (`hermes_cli/gateway.py`, `microsoft_teams` SDK) has no Adaptive Card `Action.Submit` handling, so taps get a conversational reply and `handle_teams_card_submit` never runs. Confirmed repeatedly. Agreed direction: `LEXI_TEAMS_TEXT_ONLY=true` + execution-backed confirmations (only claim success when a tool returned success). **Not implemented.**
2. **A plain "Reply" is invisible.** Mail goes out from lexi@, so a normal Reply lands only in Lexi's mailbox, which nothing ingests (`LEXI_POLL_LEXI_MAILBOX=false`, deliberately). Guests must Reply All today — they won't.
3. **Stale conclusions without calling a tool** — Lexi has insisted a thread wasn't visible while describing an offer she'd already sent. SOUL.md rules added; never stress-tested on the scheduling path.

### Also open on scheduling
- Approval cards go stale (`teams_approval_notified_at` never re-pushed; the card carries its own draft copy, so approving a stale card overwrites the good draft).
- The Teams push claims delivery before delivering — a failed delivery is silently permanent.
- A transient send failure parks a proposal in `needs_kory` with no retry path from Teams.
- Holds are created `showAs=busy`, not tentative.
- Hold/event titles use the mashed name ("Intro: Anjanakummetha <> Kory Mitchell").

### Untested scheduling paths
Hold reminder before release (E-3), expiry release (E-4), Friday cleanup (E-5), counter-proposal to a **busy** time (H-4 — safety-critical, must ask, never auto-book), reject-all → re-offer (H-5), vague reply (H-6), thread-context retention (H-7), 24h nudge (M-1). Closed-posture: escalation when nothing fits (I-1/I-2), email-to-Lexi commands (J-1/J-2/J-3), `remember` in Teams (K-1), Kory-voice drafting (L-1/L-2). Plan: `docs/SCHEDULING_LIVE_TEST_PLAN.md`.

Test mail comes from **anjanakummetha@gmail.com** (the only address available). G-1 (unknown-TZ disclosure) isn't testable from it — its Date header reveals MT.

---

## 2. CURRENT PRODUCTION STATE

```
LEXI_ENV=production           LEXI_WRITE_MODE=kory        LEXI_DRY_RUN=false
LEXI_KORY_OUTBOUND_BLOCKED=false    ← real sends possible
LEXI_REQUIRE_KORY_APPROVAL=true     ← nothing sends without approval
LEXI_ASANA_LIVE_WRITES_ENABLED=true      ← ON (Kory NON-IFG only)
LEXI_HUBSPOT_LIVE_WRITES_ENABLED=false   ← OFF
LEXI_HUBSPOT_BCC_ENABLED=false           ← OFF until the scheduler is done
LEXI_TEAMS_TEXT_ONLY=false               ← still using cards
LEXI_ORCHESTRATOR_BACKUP_POLL_MINUTES=5  ← test-window value (was 30)
```
398 Lexi tests pass (~4s, nothing deselected). Dashboard: `tsc`, `eslint`, `next build`, read-only guard, `npm run test:email` all clean.

---

## 3. WHAT SHIPPED THIS SESSION

**HubSpot reads fixed** (`6694cbd`→`8ecbeba`). Every read omitted `properties`, so HubSpot returned its 6-field default and company/jobtitle/lifecycle/lead-status/source/last-contacted were empty everywhere. Consequences: outreach fell back to `contacts[:limit]` with **no Do-Not-Contact check anywhere** (62 of Kory's); cleanup read 50 of 2,153 and recommended archiving ~79% of the book. IFG uses a **custom lifecycle pipeline with numeric stage ids**, so the default-name guards matched nothing. Cleanup is now a read-only report; `propose_lead_source_fills` retired (`hs_analytics_source` is OFFLINE for every contact). Writes still off, guards built and tested.

**Pre-call briefs rebuilt** (`d627d11`→`6b9c56a`). `prebrief` lists today instantly; `prebrief <meeting>` researches every external attendee concurrently; `prebrief <person>` works by name or email, looks 30 days ahead, parses dates, and asks when a name is ambiguous. Research + introducer always run — no modes.

**4:45 AM briefing email is LIVE** (`4ed8911`, first real send 2026-07-30). Dashboard **composes**, Lexi **sends** — the dashboard cannot send by design (`check-no-write-slugs.mjs` fails its build on any non-read Composio slug). `lexi-morning-briefing.timer` (`America/Denver`, enabled, survives reboot), retry ×3 / 2 min, `OnFailure` → Teams alert. Lexi's own 4:45 Teams push and `briefing` command were removed.

**Dashboard**: Asana now reads all 8 projects filtered to Kory's tasks (was pinned to `Kory NON-IFG`, hiding 20 of 33); tasks grouped board → section; Key Insights and the morning summary rewritten with real criteria; colleagues no longer researched for bios; Lexi Assistant panel removed.

---

## 4. BUGS WORTH REMEMBERING — nearly all were confident wrong answers, not errors

- `normalize_due_on` threw **today's date a year forward** (MT vs local across midnight) — with Asana writes ON.
- Pipeline `isClosed` arrives as the **string** `"false"`, which is truthy in Python.
- HubSpot stage-history requests **cap at 50 objects**; asking for 100 failed and was swallowed into "no deals moved" (there were 97).
- Signature parsing pulled **Kory's own quoted footer** out of reply chains onto other people's records.
- `today` showed **6:30 AM as 12:30 AM** — Graph sends a naive dateTime beside its zone, already converted; the code assumed naive meant UTC and converted twice. Same command printed attendees as **raw Graph dicts** (a regression from selecting attendees for the prebrief).
- A **correct, tested tool the model never calls is not a working feature** — "tell me about X" went to inbox search until the tool was renamed and given trigger phrasings.

---

## 5. DEPLOY MECHANICS

**Lexi:**
```bash
ssh -i ~/.ssh/lexi_vps_ed25519 root@srv1686061.hstgr.cloud
cd /home/lexi/AI_Scheduling_Agent
G="git -c safe.directory=/home/lexi/AI_Scheduling_Agent"   # dubious-ownership guard
$G stash push -q data/kory_voice_profile.json; $G fetch origin main -q
$G merge --ff-only origin/main; $G stash pop -q
systemctl restart lexi-hermes.service
```

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
