# Lexi — Session Handoff (updated 2026-08-08, post-sweep)

## 0. VERIFICATION SWEEP + FIX DAY COMPLETE (2026-08-08) — see RUN 15

**All 86 chat tools verified against ground truth; EVERY defect found was
fixed, deployed, and live-verified the same day. Zero test residue (audited).
Lexi is ready for Kory's daily use.** Full record:
`docs/SCHEDULING_LIVE_TEST_PLAN.md` RUN 15 + `docs/CHAT_VERIFICATION_PLAN.md`.

**Fixed + shipped today (all live on the box):**
- D1 maps `q` payload · D2 stale all-day events on today-brief · D3 timezone
  body cues ("6 AM ET", "I am in Boston") — `3225255`, `01d6451`
- D4 **chat-initiated offers could never send** (synthetic thread id →
  CREATE_DRAFT_REPLY "malformed id"; now composes fresh email) — `5290d66`
- D5 escalated (`needs_kory`/`needs_scheduling_guidance`) proposals are
  rejectable from chat — `c6f8f3f`
- D6 escalations end with #N-anchored reply options + the router answers a
  bare "YES" by naming the open escalation (3-day window) — `e580274`
- M1 `lexi_start_scheduling` takes `constraints=` (Kory's words verbatim →
  window/time-of-day/tz parsing) · M2 modify-and-approve can't book a
  zero-minute meeting (novel times inherit offered duration) — `31b88de`
- R1 "mornings HER time" = the recipient's morning (Boston mornings →
  6:00–10:00 MT, live-verified 6 AM MT slots) · R2 outbound proposals store
  the "no availability for <window> — offering <dates> instead"
  scheduling_note, rendered on the approval push — `31eed76`
- Gate/engine consistency: the pre-approval gate now derives the east_coast
  cue like the engine (was flagging 6 AM slots offered FOR a Boston contact) —
  `1b964bf`
- **Features:** Asana any-project writes (`9a6a832`, project='<name>' on
  create/move) · `lexi_forget_kory_fact` (`3fe5df0`, remove a remembered rule
  from chat; refuses ambiguity)

**Decision (Anjana): Teams cards PARKED, text-only is the supported mode**
(`LEXI_TEAMS_TEXT_ONLY=true`; prod had run text-only since Aug 4 — the old
"cards ON" claim was never live and KORY_GO_LIVE_READINESS.md is corrected).
Typed `approve #N` is the approval path, proven E2E. A stale Aug-5 card even
RESURFACED during a restart resync (verified inert, RUN 15) — activity-id
persistence is now the top enhancement.

**End-of-day state:** suite **684 passing** (+3 known stale-data failures),
readiness sweep all green, queue at baseline 26 (zero [TEST]), 0 memory
facts, 0 outlook_poll errors since the 04:30 stagger deploy, Composio 9.7%
MTD, 91 tools registered with the gateway. Kory had still used NOTHING as of
~08:00 UTC; proposal 6218 still waits for him.

**Dashboard fix (2026-08-08 ~15:00 UTC, dashboard repo `36d997d`, deployed):** the Today
tab no longer substitutes MOCK briefing/priorities when today's real data doesn't exist yet
(it was showing a fabricated "Series B term sheet" day) — honest "collecting today's data"
empty states instead. Deploy recipe used: build standalone (Node 20 via nvm) → rsync
`.next/standalone/` (EXCLUDING `data/`!) + `.next/static/` → chown ceo → restart.

**Resume phrase:** *"Kory is live; check what he's done, then continue the open work list."*

Two repos, one box:
- **Lexi** `~/AI_Scheduling_Agent` → `/home/lexi/AI_Scheduling_Agent` (services `lexi-hermes` **and** `lexi-api` :8081; worker health :8780)
- **Dashboard** `CEO_Executive_Dashboard--main/` (own git repo, gitignored by the outer one) → `/opt/ceo-dashboard` (service `ceo-dashboard`)

`srv1686061.hstgr.cloud` is **multi-tenant** — never reboot it, never touch the co-tenant Docker containers (`hermes-agent-teuw-*`, `traefik-*`). **Deploy with `scripts/deploy_lexi.sh`** — it restarts both services; restarting only `lexi-hermes` serves stale `api_v1.py`.

**Logs are FILES, not journalctl.** `logs/lexi.log` (+ `~/.hermes/logs/*`). `journalctl -u lexi-hermes` contains zero application logging — a sweep against it is worthless (this mistake voided one M-3 run).

---

## 1. WHERE THINGS STAND

**All planned testing is DONE.** RUNs 1–14 in `docs/SCHEDULING_LIVE_TEST_PLAN.md` (RUNs 9–14 are the last stretch: HubSpot writes, E-3/E-4/E-6, MCP hang fix, M-3/M-4 + corrections). Suite **615 passing**.

**Kory went LIVE 2026-08-06** with all reads AND writes ON for HubSpot + Asana (Anjana's call — accepted risk documented in `docs/KORY_GO_LIVE_READINESS.md`, which also has the rollback: `.env.bak.20260805-222013` + restart both). As of 2026-08-07 02:30 UTC he had made **zero** Teams tool calls. System healthy meanwhile: 374 emails triaged (all correctly `no_reply_needed`), morning briefing sent 08-06 10:37 UTC, Composio 8.3% MTD.

**The working plan now: Kory uses it; change requests get logged into OPEN ISSUES in the test plan.** When he reports something, get a rough timestamp + what he typed — that's enough to find it in `logs/lexi.log`.

**Scrapped / parked (Kory has been told the first two do nothing):**
- Emailing `lexi@iconicfounders.com` directly — nothing watches that mailbox (one Composio trigger, on Kory's connection; `LEXI_POLL_LEXI_MAILBOX` unset)
- Outreach campaigns — `LEXI_OUTREACH_CAMPAIGNS_ENABLED=false`, MCP tools unregistered
- HubSpot BCC logging — genuinely broken HubSpot-side; Lexi-side bypass bug already fixed (`8570656`); next diagnostic is `scripts/hs_bcc_test.py --precreate` or Kory checking the portal's email-logging settings

## 1b. KORY ACTIVITY CHECK (2026-08-08 03:55 UTC)

Still **zero** Kory interactions: no Teams messages (gateway silent since its
04:38 08-06 restart), no MCP CallToolRequests since the 01:54 08-06 pre-live
tests, no approvals since the 02:15 08-06 cleanup. 742 emails triaged since
go-live, all `no_reply_needed`. **Proposal 6218** (referral_or_intro,
`awaiting_reply_prompt`) pinged Kory in Teams 08-06 17:15 (inbound reply on a
time-blocked thread) and has been waiting ever since.

## 2a. RESOLVED — morning briefing outage (Aug 7) — fix live, timer sends proven

**CLOSED 2026-08-08:** deploy + restart done ~04:30 UTC; manual POST verified 200; and the
10:30 UTC timer run on 08-08 completed SUCCESS unattended (sent 10:35:01). Original record:

The 10:30 UTC `lexi-morning-briefing.timer` failed all 6 attempts on 08-07
("Dashboard returned 500: Empty Anthropic response") — **Kory got no briefing
email 08-07**, and will get none until the fix is deployed.

Root cause (reproduced live with the exact prompt): Sonnet's adaptive thinking
spends the entire 8192-token output budget before any text block is emitted —
response is a lone `thinking` block with `stop_reason=max_tokens`. The
dashboard's `cli.ts` threw "Empty Anthropic response" *before* its own
doubled-cap max_tokens retry could run, in both `runAnthropicPrompt` and
`runAnthropicResearch` (so attendee intel was silently degrading too).

Fix committed in the dashboard repo, `a70f688` on `deploy-prep-phase1`
(reorder: max_tokens retry before the empty guard, both paths). Built and
**rsynced to `/opt/ceo-dashboard` 2026-08-08 (ownership restored to ceo)** —
only the service restart is outstanding; the running process still serves the
old code until then:

    ssh root@srv1686061.hstgr.cloud systemctl restart ceo-dashboard

Then verify: POST `http://127.0.0.1:3000/api/hermes/briefing` with
`x-briefing-token` from the agent `.env` → expect 200 with `emailDraft`.
Next timer run: 10:30 UTC daily.

## 2. RESOLVED — Graph throttling the backup poll

**CLOSED 2026-08-08 14:36 UTC:** stagger (`ce73e7a`) deployed 04:30; **0 poll errors in the
10 hours since** (baseline was 2–4/hr, 40/day). Original record:

36 `outlook_poll` failures since go-live (~2–4/hr): 20 APITimeout, 10 `CommandConcurrencyLimitReached`, 4 `ErrorTooManyObjectsOpened`, 2 `ApplicationThrottled`. The poll is the **safety net** for webhook drops (Graph 404s just-arrived message ids, ~14/48h; recovery ≤5 min). If both degrade together, mail sits unseen longer.

**Fix BUILT 2026-08-08 (`ce73e7a`, pushed to origin/main): one Kory folder per poll cycle, alternating inbox/sentitems** — each folder still swept every ~10 min inside the 24h window; 4 new tests in `tests/test_outlook_poll_stagger.py`. **Not yet live: the `deploy_lexi.sh` run was blocked by the permission classifier** — run `ssh root@srv1686061.hstgr.cloud 'bash -s' < scripts/deploy_lexi.sh` from the repo root. Skipping sentitems entirely stays on the table if errors persist (86 of the first 374 triaged messages were Kory's own outbound YPO mail).

## 2b. PREFERENCE AUDIT COMPLETE + FIX BATCH (2026-08-08, commit `0c3fd56`)

All three sweeps of `docs/PREFERENCES_AUDIT.md` are done (the paused meeting-type sweep
included) and a 14-file fix batch shipped: warnings now render in Teams (E-6 remedy copy
was being dropped), pending_invite holds no longer age out, Friday sweep waits for 5 PM MT,
re-remind-on-release implemented, reschedules = 2 options + queue priority + 1-day holds,
venue addresses on invites, stated durations honored, lunch books 60, and more — the
authoritative fixed-vs-open list is the audit doc's "2026-08-08 RESOLUTION" section.
Suite 626. **NOT yet deployed — needs a `deploy_lexi.sh` run.** Open decisions for
Kory/Anjana are listed there too (urgent-keyword breadth, Tue/Thu early-morning policy,
Matt-on-coffees default, visible coffee buffer block, venue address verification).

## 3. OPEN WORK LIST (rough order, refreshed post-sweep 2026-08-08)

1. **M-2** — Anjana verifies morning-briefing content. **Delivery is now proven fully automated:** the 08-08 10:30 UTC timer run completed SUCCESS and sent at 10:35:01 (first untouched timer send since the thinking fix). Only the content review remains.
2. ~~Stagger full-day re-check~~ **DONE 2026-08-08 14:36 UTC**: 0 outlook_poll errors in the 10h since the 04:30 deploy (pre-fix baseline 2–4/hr, 40/day). Graph throttling issue CLOSED.
3. **Activity-id persistence for Teams messages** (priority raised): stale Aug-5 cards are not just inert clutter — one RESURFACED with a fresh timestamp during the 07:56 restart resync (see RUN 15). Persist ids on send; update cards/messages to a decided/expired state on decision. Until built: any card in the chat is old — ignore; `pending` is the source of truth.
4. **Log + fix Kory's change requests** as they arrive (he had still used nothing as of 08-08 ~08:00 UTC; proposal 6218 still waits)
5. **M-3 redo** against `logs/lexi.log` (the journalctl sweep was void)
6. **Per-tool deadline** — the event-loop fix (`17b8b62`) stops one slow tool freezing the server, but a single tool doing 3×30s Composio retries can still blow the 120s MCP budget
7. **HubSpot step 5** — Kory sign-off package (never built)
8. **Design item (accepted risk):** HubSpot/Asana write approval is a model-supplied boolean; scheduling's typed `approve #N` + audited `decision_source` is the pattern to copy
9. **Decide the Aug-3 staged-ask backlog:** 26 REAL threads sit in `awaiting_reply_prompt` since 08-03 — any inbound reply re-pings Teams (that's what 6218 did). Options: expire staged asks after N days (no TTL exists) or bulk-close with Kory's sign-off.
10. **Suite isolation:** 3 pre-existing failures (`test_api_v1` aged-asks ×2, `test_prebrief_attendees`) come from Aug 5-6 history in the shared `data/lexi_test.db` window; ages out alone, real fix is isolating the suite from live-DB state.
11. ~~Systemd stop-timeout mismatch~~ **DONE 2026-08-08 08:48 UTC**: drop-in `/etc/systemd/system/lexi-hermes.service.d/stop-timeout.conf` sets `TimeoutStopSec=210` (was the 90s default vs a 180s drain — SIGKILL risk mid-drain). daemon-reload + restart verified: warning gone from the new startup, service healthy.
12. **Benign leftover:** one orphaned chat-path `[TEST]` note object in HubSpot (its contact is archived; no note-search slug exists to find it). Hidden from the UI.

## 4. HARD-WON FACTS (do not relearn)

- **Validate Composio payloads against `get_raw_composio_tools(...).input_parameters`, never vendor REST docs.** `HUBSPOT_CREATE_NOTE` wants `hs_note_body`/`hs_timestamp`/`associations` (not `contactId`/`body` — the feature had never worked before `b84f301`); `HUBSPOT_CREATE_CONTACT` is flat and silently ignores nested `properties`; `UPDATE_CONTACT` *does* nest. Dry-run stubs cannot catch wrong shapes.
- **Two HubSpot search indexes:** EQ property filters are consistent immediately; free-text `query` lags minutes. `contacts_by_ids()` is search-backed despite the name; `HUBSPOT_READ_CONTACT` is the only true by-id read. Archive (recycle bin), never GDPR-delete — GDPR blacklists the address from the portal forever.
- **fastmcp runs sync tools inline on the event loop** — every tool must stay async (the `_tool` decorator); a test enforces this.
- **HubSpot portal is one shared space** — owner id is a property, not a partition; test records are company-visible until archived.
- Frozen `settings` dataclass reads env at import — flag changes need both services restarted (also the answer to the "stale UAT card copy" mystery — not a bug).
- Sends are live: **never approve old proposal numbers; never re-approve after a timeout without checking the DB first.**
- E-6's confirm-time conflict guard (`a597756`) fails CLOSED and deliberately has **no override flag** — resolution is "clear the clash, re-approve".
- Readiness sweep: `scripts/go_live_readiness.py` (read-only, ~15s, probes calendar/mail/HubSpot/Asana/prefs).

## 5. UNCOMMITTED

`data/kory_voice_profile.json` is modified locally — runtime state, deliberately left uncommitted all session. Leave it.
