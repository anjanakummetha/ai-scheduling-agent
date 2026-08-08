# Lexi — Technical Handover Document

**For:** whoever maintains this after Anjana (e.g., the next intern)
**As of:** 2026-08-08 — Lexi is **LIVE in production** for Kory Mitchell (CEO, Iconic Founders Group)
**Read this first, then:** `docs/SESSION_HANDOFF.md` (living state doc) and `docs/SCHEDULING_LIVE_TEST_PLAN.md` (the complete test history, RUNs 1–15).

> ⚠️ **This system sends real email from the CEO's real mailbox and books real meetings on his
> real calendar.** Treat every change as production. The safety rails are real but they assume
> you don't disable them. When in doubt: §6 (kill switches) and §11 (safe testing).

---

## 1. What this system is

Two products on one server:

1. **Lexi** — an AI executive assistant living in Kory's Microsoft Teams chat. She watches his
   real Outlook inbox, schedules meetings end-to-end (find slots → draft offer → his approval →
   send → parse the reply → his approval → calendar invite), manages Asana tasks, reads/writes
   HubSpot, does web research, and keeps his standing preferences in memory. ~91 tools; every
   outbound action is gated on his typed `approve #N`.
2. **CEO Dashboard** — a read-only Next.js web app (day at a glance, triaged inbox, tasks,
   meeting prep, AI prioritization) that also generates his **morning briefing email**
   (daily timer, 10:30 UTC ≈ 4:30 AM Mountain). It has **no write path by design**.

User-facing docs (give these to any new stakeholder):
- `docs/LEXI_COMPLETE_GUIDE.html` — everything Kory can do, every command, every limit
- `docs/LEXI_SYSTEM_MAP.html` — visual architecture + flows

---

## 2. Infrastructure

### The server
- **Host:** `srv1686061.hstgr.cloud` (Hostinger VPS, Ubuntu, root access via SSH key)
- **SSH:** `ssh -i ~/.ssh/lexi_vps_ed25519 root@srv1686061.hstgr.cloud`
  — the private key lives with Anjana; a new maintainer needs their own key added to
  `root`'s `authorized_keys` (do NOT share the existing private key file).
- ⚠️ **The VPS is MULTI-TENANT.** It also runs an unrelated company's containers
  (`hermes-agent-teuw-hermes-agent-1`, `traefik-traefik-1`). **NEVER reboot the box. NEVER
  stop/restart/touch the Docker containers.** Traefik (theirs) is also the HTTPS front for our
  webhook — killing it kills our mail ingress *and* their product.

### Services (systemd)
| Unit | What | Ports | Working dir |
|---|---|---|---|
| `lexi-hermes.service` | Gateway (Teams bot) + MCP server + background worker — the whole agent | 3978 (Teams webhook `/api/messages`), 8780 (worker health + Composio webhook) | `/home/lexi/AI_Scheduling_Agent` |
| `lexi-api.service` | Read-only REST API consumed by the dashboard | 8081 | same repo, separate uvicorn |
| `ceo-dashboard.service` | Next.js dashboard | 3000 | `/opt/ceo-dashboard` |
| `lexi-morning-briefing.timer` | Fires the briefing build daily 10:30 UTC (allows 900s) | — | — |

- `lexi-hermes` has a systemd drop-in: `/etc/systemd/system/lexi-hermes.service.d/stop-timeout.conf`
  (`TimeoutStopSec=210`) — the gateway needs up to 180s to drain gracefully. Don't remove it.
- **Restarting only `lexi-hermes` serves stale `api_v1.py`** — always restart both app services
  (the deploy script does).

### Repos
| Repo | Local path (Anjana's Mac) | Deploys to | Notes |
|---|---|---|---|
| `AI_Scheduling_Agent` | `~/Downloads/IFG 2026 Summer Internship/AI_Scheduling_Agent` | `/home/lexi/AI_Scheduling_Agent` | GitHub: `anjanakummetha/ai-scheduling-agent` (old URL redirects). Branch `main`. Box pulls **ff-only from origin/main** — commit+push before deploying. |
| `CEO_Executive_Dashboard--main` | nested inside the above (gitignored by it — it's its **own** git repo) | `/opt/ceo-dashboard` | Working branch `deploy-prep-phase1`. Deploy = build + rsync + `systemctl restart ceo-dashboard` (ownership must end up as the `ceo` user). |

### Stack
- Python 3.12 in `.venv` **on the box** (local Mac: use `./.venv` too — the older `./venv` is a
  different Python without pytest).
- **hermes-agent 0.16.0** — the gateway/agent framework (Teams adapter, session store,
  context compressor, agent memory). Config: `/home/lexi/.hermes/config.yaml`; its state lives
  under `/home/lexi/.hermes/` (sessions, `state.db`, `memories/MEMORY.md` + `USER.md`,
  `kanban.db`). The chat model is Anthropic Claude (see config).
- **fastmcp** MCP server — `hermes_mcp_server.py`, ~91 tools, stdio, spawned by the gateway.
- **Composio** — the integration layer for Outlook (mail+calendar), Asana, HubSpot, and search
  tools (web/news/maps/flights/hotels). All external API calls go through it. Monthly budget
  200k calls; usage on the health endpoint (was ~9.7% MTD at handover).
- **SQLite** — `data/lexi.db` on the box is THE production database.

### Mail ingress (how Lexi notices email)
`webhook_primary_backup_poll`: Composio pushes Outlook events to
`https://srv1686061.hstgr.cloud/webhooks/composio` (through the co-tenant's Traefik → :8780),
**plus** a backup poll every 5 minutes that alternates inbox/sentitems (staggered because MS
Graph throttled the old both-every-cycle version). Graph sometimes 404s just-arrived message
ids on the webhook (~14/48h); the poll is the safety net — recovery ≤5 min.

---

## 3. Code map (where things live)

```
hermes_mcp_server.py          ← all ~91 MCP tool definitions (thin wrappers; docstrings ARE
                                 the agent's instructions — wording changes = behavior changes)
app/assistant/actions.py      ← the action layer the tools call (one function per capability)
app/agents/
  comms_agent.py              ← execute_lexi_approval: THE approval executor (send_offer /
                                 send_invite phases, holds, conflict re-check, audit)
  outbound_agent.py           ← chat-initiated ("set up a call with X") scheduling
  inbound_reply.py            ← reply-queue drafting / decline
  scheduler_agent.py          ← calendar context loading
app/scheduling/
  slot_engine.py              ← candidate generation + scoring
  schedule_from_context.py    ← the unified engine path (plan → travel shift → engine → gate)
  scheduling_window.py        ← window parsing ("next week"), time-of-day ("mornings her time"
                                 shifts to recipient TZ), weekday parsing, East-Coast cue
  pre_approval_gate.py        ← verify_before_kory_approval (the disclosure/warning gate)
  timezone_intel.py           ← recipient timezone detection (body > signature > area code >
                                 prior threads > domain)
  kory_escalation.py          ← escalations to Kory (always end with #N-anchored options)
  hermes_compose.py           ← LLM composition (offer emails, guidance messages)
app/rules/validators.py       ← per-slot rule validation (the 12 rules; east_coast/urgent flags)
rules.py                      ← Kory's HARD_BLOCKS, meeting types, workout days
app/bot/
  teams_publisher.py          ← proactive Teams pushes (text mode; card code parked)
  teams_text.py               ← typed-command parsing (approve/reject/cancel/pending/bare-ack)
  teams_labels.py             ← human-phrase → proposal id resolution
app/teams/commands.py         ← handle_teams_command router (the DB-authoritative command path)
app/integrations/
  composio_client.py          ← execute_tool + retry + connection-role resolution
  outlook_email.py / outlook_calendar.py / outlook_actions.py
  hubspot_manager.py          ← search indexes, guardrails, staging, notes
  asana_manager.py            ← any-project create/move, ownership filter, date normalization
  composio_search.py          ← web/news/maps/travel (maps takes `q`, not `query`!)
app/storage/                  ← lexi_db (connections), kory_memory (remember/forget), sessions
app/safety/approval_gate.py   ← assert_kory_approved_write + flag logic
app/config.py                 ← FROZEN settings dataclass — reads env AT IMPORT (see §7)
scripts/
  deploy_lexi.sh              ← THE deploy (backup env+db, ff-only pull, restart both, verify)
  go_live_readiness.py        ← read-only live probe of all 5 systems (~15s)
  hs_write_test.py            ← HubSpot write harness (disposable contact, auto-archives)
  cleanup_stale_test_threads.py
tests/                        ← ~690 tests; run `./.venv/bin/python -m pytest tests -q`
```

The DB tables you'll actually query: `proposals` (status machine), `holds`, `approvals`
(decision + `decision_source` + who), `audit_log`, `email_threads`, `kory_memory`,
`scheduling_sessions`, `worker_heartbeat`, `hubspot_batches`, `llm_cost_log`, `composio_call_daily`.

---

## 4. Configuration & secrets

- **`.env` at the repo root ON THE BOX** is the production config. It is NOT in git.
  Timestamped backups sit next to it (`.env.bak.*` — the deploy script makes one every run).
- Secrets in it (values live only on the box — never commit, never paste into chat/docs):
  Anthropic API key, Composio API key + connected-account ids, Teams bot id/password,
  `LEXI_API_TOKEN` (dashboard→API auth), briefing token, DB paths.
- **Key behavior flags (production values at handover):**

| Flag | Value | Meaning |
|---|---|---|
| `LEXI_ENV` | `production` | |
| `LEXI_WRITE_MODE` | `kory` | acts on Kory's real accounts |
| `LEXI_DRY_RUN` | `false` | **`true` = global no-writes mode (best soft kill)** |
| `LEXI_REQUIRE_KORY_APPROVAL` | `true` | approval gate on |
| `LEXI_AUTO_EXECUTE_ENABLED` | `false` | never auto-executes |
| `LEXI_ALLOW_IMMEDIATE_SEND` | `false` | approve = stage/send with sign-off, no skip |
| `LEXI_KORY_OUTBOUND_BLOCKED` | `false` | `true` blocks sends from Kory's mailbox |
| `LEXI_ASANA_LIVE_WRITES_ENABLED` | `true` | |
| `LEXI_HUBSPOT_LIVE_WRITES_ENABLED` | `true` | |
| `LEXI_TEAMS_TEXT_ONLY` | `true` | **deliberate** — cards parked, typed approvals only |
| `LEXI_HUBSPOT_BCC_ENABLED` | `false` | broken HubSpot-side, parked |
| `LEXI_OUTREACH_CAMPAIGNS_ENABLED` | `false` | campaigns scrapped; tools unregistered |

---

## 5. Routine operations

### Deploy a change
```bash
# from the repo root on the Mac:
./.venv/bin/python -m pytest tests -q     # expect: all pass except 3 known (see §9)
git add … && git commit && git push origin main
ssh -i ~/.ssh/lexi_vps_ed25519 root@srv1686061.hstgr.cloud 'bash -s' < scripts/deploy_lexi.sh
```
The script: backs up `.env` and the DB → ff-only merge of origin/main → restarts **both**
services → health checks → prints safety posture → confirms co-tenant containers untouched.
**Don't deploy while someone is mid-interaction in the Teams chat** — the restart drops
in-flight taps/messages.

### Watch it
```bash
curl -s http://127.0.0.1:8780/api/health          # on the box: status, heartbeat, budget
cd /home/lexi/AI_Scheduling_Agent && LEXI_ENV=production .venv/bin/python scripts/go_live_readiness.py
tail -f logs/lexi.log                              # ← APPLICATION LOGS ARE FILES.
tail -f ~/.hermes/logs/gateway.log                 #    journalctl has NOTHING useful.
sqlite3 data/lexi.db "select id,status from proposals order by id desc limit 10"
```

### Flag change
Edit `.env` on the box → **restart BOTH services** (settings are frozen at import — a flag
change without a double restart silently does nothing; this has burned us).

### Rollback
- Code: revert the commit on main, push, deploy (ff-only means no force-push games).
- Config: `cp .env.bak.<timestamp> .env` then restart both. A known-good full backup from
  go-live is `.env.bak.20260805-222013`.
- DB: deploy-time snapshots at `data/lexi-deploy-<stamp>.db` (sqlite `.backup` copies).

---

## 6. HOW TO STOP IT (kill switches, graduated)

From gentlest to hardest. After any flag change, restart **both** services.

| Level | What you want | Do this |
|---|---|---|
| 0 | Quiet the Teams pings only | `LEXI_SUPPRESS_TEAMS_PUSH=true` in `.env` → restart both. Mail triage continues silently. |
| 1 | **Stop all external writes, keep it observing** (the go-to soft kill) | `LEXI_DRY_RUN=true` → restart both. Every send/booking/task/CRM write becomes a logged preview. Fully reversible. |
| 2 | Stop just one surface | `LEXI_ASANA_LIVE_WRITES_ENABLED=false` and/or `LEXI_HUBSPOT_LIVE_WRITES_ENABLED=false`, or `LEXI_KORY_OUTBOUND_BLOCKED=true` (no email leaves Kory's mailbox) → restart both. |
| 3 | **Stop the agent entirely** | `systemctl stop lexi-hermes lexi-api` — Teams goes silent, mail ingress stops, MCP dies. Add `systemctl disable …` to survive a box reboot. Dashboard keeps running (it's read-only) unless you also `systemctl stop ceo-dashboard`. |
| 4 | Stop the morning briefing | `systemctl stop lexi-morning-briefing.timer` (`disable` to make permanent). |
| 5 | Cut integrations at the source | Log into the **Composio dashboard** (Anjana's account) and disable/delete the connected accounts (Outlook / Asana / HubSpot). Instantly severs ALL access to Kory's data regardless of what the code does. This is the right move if you suspect compromise. |
| 6 | Cut Teams | Remove the Lexi app from Kory's Teams (or delete the Azure bot registration). Breaks the stored conversation reference — re-adding requires him to message the bot once to re-register. |
| 7 | Anthropic side | Revoke/rotate the Anthropic API key — the agent brain stops mid-sentence; tools/webhooks keep running but nothing intelligent happens. Prefer level 3 over this. |

**Emergency checklist if you suspect it sent something it shouldn't have:**
1. `systemctl stop lexi-hermes` (freezes everything in seconds; safe, reversible).
2. Evidence: `sqlite3 data/lexi.db "select * from audit_log order by id desc limit 50"` and
   `approvals` — every send has an audit row with `decision_source` and authorizer. Check
   Kory's Sent Items for the actual message.
3. Know this pattern: **Kory runs his own outreach sequences from his mailbox** — odd-hours
   outbound in Sent Items with zero Lexi audit rows is HIM/his tooling, not Lexi
   (this false alarm has happened; see RUN 15 notes).
4. Un-send isn't possible (it's email), but `cancel meeting #N` retracts invites cleanly.

**Never do, even in an emergency:** reboot the VPS; touch the Docker containers; GDPR-delete a
HubSpot contact (permanently blacklists the address from the portal — archive instead).

---

## 7. Hard-won facts (each of these cost real debugging — do not relearn)

1. **`app/config.py` settings are FROZEN at import.** Env changes require restarting both
   services. "The flag didn't work" = you didn't restart both.
2. **Validate Composio payloads against `input_parameters` from the Composio SDK, never the
   vendor's REST docs.** Proven wrong three separate times: `HUBSPOT_CREATE_NOTE` wants
   `hs_note_body`/`hs_timestamp`/`associations`; `HUBSPOT_CREATE_CONTACT` is flat (nested
   `properties` silently ignored) while `UPDATE_CONTACT` nests; `COMPOSIO_SEARCH_GOOGLE_MAPS`
   takes `q` while every other search tool takes `query`. Dry-run stubs cannot catch shapes.
3. **HubSpot has two search indexes.** EQ property filters are instantly consistent; free-text
   `query` lags minutes (a just-created contact "doesn't exist"). `HUBSPOT_READ_CONTACT` is the
   only true by-id read. **Archive, never GDPR-delete.** The portal is ONE shared space — owner
   id is a property, not a partition; test records are company-visible until archived.
4. **fastmcp runs sync tools inline on the event loop.** Every tool must stay async (the
   `_tool` decorator wraps via a worker thread); one sync tool froze all 87 for 120s. A test
   enforces this. Related: the gateway's MCP call budget is 120s per call — a tool doing 3×30s
   Composio retries can still blow it (open work).
5. **Graph/Outlook quirks:** `OUTLOOK_LIST_MESSAGES` ignores its `filter` param — scope
   client-side. Message/event ids are mailbox-scoped. `calendarView` returns all-day events
   created in other timezones that don't overlap your window — the today-brief filters by real
   local-day overlap (don't remove that). Webhook 404s brand-new ids; the poll catches them.
6. **Sends are LIVE. Never approve an old proposal number** from chat scroll; never re-approve
   after a timeout without checking the DB first (`pending` is the truth). The E-6
   conflict-at-confirm guard fails CLOSED and deliberately has **no override flag** — the fix
   is "clear the clash, re-approve", never a bypass.
7. **Escalations are proactive pushes** — Kory's reply arrives WITHOUT the push in the agent's
   context. That's why every escalation ends with `#N`-anchored reply options and the router
   resolves bare "YES" against open escalations. Don't regress this (D6).
8. **Chat-initiated proposals have synthetic thread ids** (`lexi-outbound-<hex>`) — there's no
   message to reply to; dispatch must compose a NEW email (D4). Their constraints ride the
   `constraints=` param into the engine body (M1) — the engine parses window/time-of-day/
   timezone cues from body text, including "mornings HER time" → recipient's clock (R1).
9. **Old Teams messages can't be edited/deleted** (activity ids were never stored) and stale
   cards can even be REDELIVERED by a Teams client resync after a restart, looking brand new.
   They're inert (executor refuses non-pending statuses). Fix on the roadmap: persist activity
   ids. Meanwhile: cards = old = ignore.
10. **The gateway agent keeps its own memory** at `~/.hermes/memories/MEMORY.md` (2200-char
    cap) — operational lessons like "as Lexi means her mailbox". `kory_memory` (DB) is separate
    and feeds the slot engine's preferences. Kory edits the latter via remember/forget in chat.
11. **The morning briefing** burns its whole token budget on thinking on the first attempt and
    succeeds on the doubled-cap retry (~6 min total; timer allows 900s). That's normal, not a
    hang. The dashboard's `cli.ts` retry-order fix (`a70f688`, dashboard repo) is what keeps it
    alive — don't reorder those guards.
12. **Suite state:** ~690 tests. 3 known failures are stale local-DB history aging through the
    tests' time windows (`test_api_v1` aged-asks ×2, `test_prebrief_attendees`) — they self-heal;
    the real fix is isolating tests from `data/lexi_test.db` history.

---

## 8. Kory-specific behavior (the product spec in one place)

- **His 12 scheduling rules** are enforced in `rules.py` + `app/rules/validators.py`; the
  human-readable version is §5 of `LEXI_COMPLETE_GUIDE.html`. Ruling history and rationale:
  memory files + `docs/PREFERENCES_AUDIT.md`. The load-bearing ones: training block until 8:00
  (virtual) / 9:30 (in-person); Tue/Thu 6 AM only for East-Coast contacts; lunch and weekends
  are exception-only and route to him; coffee books 60 min with an invisible 30-min buffer;
  "urgent" escalates instead of bypassing; travel weeks are guarded; next-day earliest.
- **Cold inbound never pings him** (`delegation_and_followups` notify mode) — it goes to
  briefs/queues. This is policy, not a bug.
- **Email voices:** "as me" = Kory's voice from his mailbox; "as Lexi" = assistant voice from
  `lexi@iconicfounders.com` (introduce as "Kory's assistant", never "Executive Assistant";
  sign-off not bold). Emailing lexi@ directly does nothing — no ingress on that mailbox.
- **Approvals are TYPED** (`approve #N` / `reject #N — reason` / `cancel meeting #N — reason`);
  natural language works because the agent resolves numbers itself. Cards are parked.

---

## 9. State at handover + open work (priority order)

Everything below is also in `docs/SESSION_HANDOFF.md` §3 (keep THAT file current as you work).

1. **M-2** — a human still needs to eyeball the morning-briefing content for accuracy.
2. **Stagger re-check** — poll errors went 2–4/hr → 0 after `ce73e7a`; confirm over a full day.
3. **Activity-id persistence** — store Bot Framework activity ids on every Teams send so old
   cards/messages can be retired; priority raised by the resurfacing-card incident.
4. **Kory's change requests** — he had used NOTHING as of handover; when he starts, log every
   request in `SCHEDULING_LIVE_TEST_PLAN.md` OPEN ISSUES (timestamp + what he typed is enough
   to find it in `logs/lexi.log`).
5. **M-3 redo** against `logs/lexi.log` (an old sweep ran against journalctl = void).
6. **Per-tool deadline** inside the 120s MCP budget.
7. **HubSpot "step 5" sign-off package** for Kory (never built).
8. **Typed approval for HubSpot/Asana writes** — currently a model-supplied boolean
   (accepted risk); scheduling's typed `approve #N` + audited `decision_source` is the pattern.
9. **Aug-3 staged-ask backlog** — 26 real threads in `awaiting_reply_prompt`; any reply
   re-pings Kory. Needs either a TTL feature or his one-time bulk sign-off. (Proposal 6218 is
   the live example sitting in his chat.)
10. **Suite isolation** from shared test-DB history (the 3 flaky-ish failures).
11. Cosmetic: orphaned `[TEST]` note object in HubSpot (contact archived; harmless, invisible).

**Verification history:** RUN 15 (2026-08-08) exercised all ~91 tools against live systems and
fixed every defect found the same day — D1–D6, M1/M2, R1/R2, plus the gate/engine cue mismatch.
If you change scheduling code, re-run the relevant slice of that playbook
(`docs/CHAT_VERIFICATION_PLAN.md` has the containment method).

---

## 10. Testing safely against production (the containment method)

There is no sandbox — you test against real systems with containment. The rules that kept
RUN 15 invisible to the outside world:

1. **The only external counterparty is a controlled address** (historically
   `anjanakummetha@gmail.com`; use your own). Never a real contact of Kory's.
2. **`[TEST]` prefix on everything** — subjects, event titles, task names, notes, memory keys.
3. **Asana writes** only in the "Anju — CEO Executive AI Tools (Summer 2026)" project
   (members: Anjana/Heidi/Kory) — or your equivalent private project.
4. **HubSpot**: disposable contact on your controlled address → verify → **archive same
   session** (`scripts/hs_write_test.py` is the proven harness; it auto-archives).
5. **Calendar**: [TEST] events in rule-legal slots, deleted after, count-verified zero.
6. **Never touch**: real proposals (check `pending` first), the staged-ask backlog, DNC
   contacts, other owners' CRM records.
7. **Clean up in the same session** and verify zero residue (RUN 15's Phase 4 script pattern:
   calendar sweep → mail sweep → DB closes with audit rows → memory → readiness re-run).
8. Standing authorization (from Kory via Anjana) covers exactly this envelope — sandbox-style
   testing with contained writes. It does NOT cover emailing real third parties.

---

## 11. Dashboard specifics

- Separate repo (`CEO_Executive_Dashboard--main`, branch `deploy-prep-phase1`) →
  `/opt/ceo-dashboard`, service `ceo-dashboard` (:3000), owned by the `ceo` user (rsync as
  root then fix ownership, or the service fails to read).
- **Read-only by design**: an allowlist restricts it to GET-style data; HubSpot was
  deliberately stripped from it; it must never gain write ability (that's a product promise
  made to Kory in writing).
- The **morning briefing** endpoint: `POST http://127.0.0.1:3000/api/hermes/briefing` with the
  `x-briefing-token` header (token in the agent `.env`). The systemd timer calls it daily
  10:30 UTC. First LLM attempt exhausts max_tokens on thinking; the built-in doubled-cap retry
  succeeds — total ~6 min is normal.
- It reads Lexi data via `lexi-api` (:8081, bearer `LEXI_API_TOKEN`).

---

## 12. Ownership & contacts

- **Built/operated by:** Anjana Kummetha (IFG 2026 summer internship).
- **User:** Kory Mitchell (CEO). Interface promises made to him live in
  `LEXI_COMPLETE_GUIDE.html` — don't break them silently.
- **Also affected by the box:** the co-tenant's Hermes Agent product and a company-wide
  dashboard behind Traefik. You are a guest on that machine; act like one.
- **Accounts you'd need transferred:** GitHub repo access, Hostinger/VPS SSH key, Composio
  account, Anthropic console, the Azure/Teams bot registration, and (for the dashboard repo)
  its GitHub access. All owned by Anjana at handover.

*Last full verification: 2026-08-08 (RUN 15). If the gap between then and your takeover is
long, run `scripts/go_live_readiness.py`, skim `audit_log` for surprises, and re-read
`SESSION_HANDOFF.md` §0 before touching anything.*
