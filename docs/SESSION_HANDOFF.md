# Lexi Production Deployment — Session Handoff (2026-07-24)

Lexi (AI executive assistant for Kory Mitchell) + the CEO Dashboard were **deployed to production and verified live** this session. The full scheduling pipeline is proven working; live sends are currently dialed back OFF pending four bug fixes that are implemented locally but not yet deployed.

---

## 1. CURRENT PRODUCTION STATE (as of end of session)

**Everything is deployed, live, and safe.** Lexi runs in a **sends-CLOSED posture** (reads/triages/drafts/briefs; no live email sends or calendar holds until re-enabled).

| Component | Where | State |
|---|---|---|
| Lexi worker + Teams gateway | `lexi-hermes.service` (combined: worker :8780 + hermes gateway :3978) | ✅ live, healthy |
| Read-only API (dashboard data) | `lexi-api.service` :8081 | ✅ live |
| CEO Dashboard | `ceo-dashboard.service` :3000 (Node 20 static at `/opt/node20`) | ✅ live |
| Watchdog (5-min health) + hourly DB backup | `lexi-watchdog.timer`, `lexi-backup.timer` | ✅ active |
| Public routing / TLS | Sujash's **Traefik** via `/docker/traefik/dynamic/lexi.yml` (one added file) | ✅ valid Let's Encrypt |

- **Public URL:** `https://srv1686061.hstgr.cloud` → dashboard; `/api/messages` → Teams gateway; `/webhooks/composio` → worker.
- **Dashboard login:** username `kory` / password `KoryLexi2026`.
- **Lexi code:** `/home/lexi/AI_Scheduling_Agent` — now a **git checkout of `main`** (6434c62). Runs as user `lexi`.
- **DB:** `/home/lexi/AI_Scheduling_Agent/data/lexi.db` — **WAL mode enabled** this session (fixes lock contention).

**Live safety posture (in `/home/lexi/AI_Scheduling_Agent/.env`):**
```
LEXI_ENV=production
LEXI_WRITE_MODE=kory
LEXI_DRY_RUN=false
LEXI_KORY_SPACE_READ_ONLY=false     # calendar-hold capability on...
LEXI_KORY_OUTBOUND_BLOCKED=true     # ...but sends BLOCKED (holds are coupled to send, so nothing writes)
LEXI_REQUIRE_KORY_APPROVAL=true
LEXI_ALLOW_IMMEDIATE_SEND=false
LEXI_AUTO_EXECUTE_ENABLED=false
LEXI_HEIDI_ESCALATION_ENABLED=false
# Asana/HubSpot/outreach live-writes all false
```
On-server env backups: `.env.closed-backup`, `.env.rung1-backup`, `.env.closed-posture-*`, `.env.pre-redeploy-*`.

---

## 2. KEY FACTS DISCOVERED (differed from the prior handoff)

- **The "stale June build" was actually LIVE at Rung 3** on Kory's real accounts (WRITE_MODE=kory, DRY_RUN=false, outbound unblocked), running ~3 days — NOT read-only/harmless as the prior handoff claimed. Brought to closed posture during redeploy.
- **The VPS is multi-tenant.** It also hosts **Sujash's** (`barmansujash4@` on the Tailscale net) work: a Hostinger "Hermes Agent" Docker container, a separate `/opt/hermes` gateway, an IFG-wide dashboard, **Traefik** (owns 80/443), and Tailscale. The user owns Lexi; Sujash owns the surrounding infra. Deploy was done to **coexist** — Sujash's components untouched.
- **Architecture on the box** ≠ the repo's `deploy/` split-unit design. It uses the **combined** `hermes gateway run` + `hermes_mcp_server.py` model at `/home/lexi/AI_Scheduling_Agent` (NOT `/opt/lexi`, NOT Caddy). We updated in place and added `lexi-api` + Traefik route rather than re-architecting.
- **The local `.env` Anthropic key was unfunded** ("credit balance too low"); the user's funded prod key is now in the server `.env` (verified live on `claude-haiku-4-5` + `claude-sonnet-5`).
- **Delegation is CC-based**: Lexi actively schedules only when a thread is delegated to it (CC `lexi@iconicfounders.com`), default on (`LEXI_DELEGATION_CC_ONLY=true`). Non-delegation inbound is triaged and deferred to Kory (`no_reply_needed`).
- **Holds are coupled to Send** in the Teams card ("Holds are placed on Calendar after you send") — so holds-only (Rung 1) isn't reachable via the card; enabling holds requires enabling sends.

**Connection IDs / addresses (all in server `.env`):** Outlook(Kory) `ca_qORrE-NzPib2`, Lexi mailbox `ca_4BTJ6d0O8sSZ` (lexi@iconicfounders.com), Asana `ca_cISuS3L6HDZn` (project GID 1211141447026980), HubSpot `ca_jdY18Wb0L46M`, LinkedIn `ca_c_o9UMJRZkMe`; Composio entity `Kory`. Composio Outlook trigger `ti_PCV0xB_btFwV`. Teams app id `f284770c-60f5-4bb6-ae15-655baed26a6a`.

---

## 3. DECISIONS MADE

- **In-place update, not clean re-deploy** — update the live checkout to latest `main`, keep the proven combined-gateway architecture, add the dashboard + read-only API alongside. Full backups taken first (DB + configs + code tree, on VPS and the Mac).
- **Coexist with Sujash's stack** — expose via his Traefik with one additive file; never edit his containers/routes; never force-restart the VPS.
- **Fresh DB** kept (June DB backed up); **WAL enabled** for concurrency.
- **Enablement ladder walked to approved-sends and proven, then dialed back to closed** while bugs are fixed (user-endorsed "prove once, then dial back").
- **Every write stays human-approved in Teams** — immediate/auto send never enabled; the approval tap stays the user's action (not automated).
- **Keys that transited chat** (Anthropic, Composio, dashboard secrets) should be **rotated** — still pending.

---

## 4. WHAT WAS PROVEN WORKING (live)

Health 200 · Teams bidirectional round-trip (`today` → reply) · email ingestion (webhook + 30-min poll) · dashboard over HTTPS with real data + AI briefing · watchdog + backups · **full delegation → propose (correct Tue slots in Kory's TZ) → approve → real email SENT** to a controlled test address. On Composio timeout mid-send, Lexi correctly bailed with **no partial write** (safety behavior confirmed).

---

## 5. BUGS FOUND (fixed locally, NOT yet deployed)

Implemented on branch **`bugfix/live-scheduling`**; hermetic suite passed **308**. Uncommitted, not merged, not deployed. Details in `docs/`-adjacent scratchpad `rung1-bugs-found.md` and memory `lexi-prod-live-state.md`.

1. **Send/hold/status atomicity (most serious):** email sent but holds + status writes failed under SQLite lock → email out, calendar unprotected, status stuck `pending_approval` (duplicate risk). Fix: transition proposal out of pending the instant send succeeds; resilient hold placement; cut excessive calendar-discovery calls. (WAL already mitigates the lock.)
2. **CC-poll miss:** Kory's delegation replies (CC lexi@ in his Sent folder) not detected (`is_delegation=0`) because `OUTLOOK_GET_MESSAGE`/sent-folder poll didn't fetch CC/To recipients. Fix: fetch ccRecipients/toRecipients through the whole chain.
3. **Time misparse:** a vague "Tuesday/Wednesday afternoon" reply became a fake "Friday 00:27 MT" conflict ping. Fix: don't fabricate a specific time / conflict when only day/period preferences are present.
4. **False "Heidi has been flagged":** LLM wording in a Kory-facing message while Heidi escalation is OFF (verified NO email went to Heidi). Fix: prompt instruction + hard scrub of any "Heidi" mention when the gate is off.

---

## 6. ACTION ITEMS / NEXT STEPS (in order)

1. **Review the diff** on `bugfix/live-scheduling`, then commit + push + merge to `main` (agent repo `anjanakummetha/AI_Scheduling_Agent-`). Confirm CI green.
2. **Deploy fixes to server:** `ssh root@srv1686061.hstgr.cloud`, then as lexi: `cd /home/lexi/AI_Scheduling_Agent && git pull` → `systemctl restart lexi-hermes.service` → verify health.
3. **Re-test** the delegation → approve → send/hold flow; confirm all four bugs fixed (holds land, status updates, no false conflict, no Heidi mention, Kory-sent delegation detected).
4. **Re-open approved sends** (`LEXI_KORY_OUTBOUND_BLOCKED=false`) once verified; keep it there for daily use.
5. **Rotate secrets** that transited chat (Anthropic + Composio keys, dashboard password/token); update server `.env` + dashboard `.env.production`; restart; re-verify.
6. **Optional:** rebuild Kory voice profile against real Outlook history (`rebuild_voice_profile()`); update `KORY_USER_GUIDE.html` with live URL + login.

---

## 7. ACCESS & KEY COMMANDS

- **SSH:** `ssh root@srv1686061.hstgr.cloud` (key `~/.ssh/lexi_vps_ed25519` on the Mac; already installed on the VPS). IP 2.24.111.64.
- **Health:** `curl -s http://127.0.0.1:8780/api/health` (on the box).
- **Logs:** app → `/home/lexi/AI_Scheduling_Agent/logs/lexi.log`; gateway → `/home/lexi/.hermes/logs/gateway.log`; `journalctl -u lexi-hermes`.
- **Posture check:** `LEXI_ENV=production .venv/bin/python -c "import app.config as c,json;print(json.dumps(c.safety_posture_summary()))"`
- **Enable approved sends (Rung 3):** `sed -i 's|^LEXI_KORY_OUTBOUND_BLOCKED=.*|LEXI_KORY_OUTBOUND_BLOCKED=false|' .env && systemctl restart lexi-hermes.service`
- **Rollback:** `cp .env.closed-backup .env && systemctl restart lexi-hermes.service` (or restore DB via `deploy/restore_lexi_db.sh`).
- **Teams commands:** `today`, `brief`, `pending`, then `send`/`approve #N` to approve. Delegation trigger = CC `lexi@iconicfounders.com` on a scheduling thread.
- **Backups:** on VPS `/home/lexi/backups/`; copies on Mac scratchpad `vps-backup-20260723-pre-redeploy/`.

---

## 8. NET STATUS

Lexi + CEO Dashboard are **deployed, live, HTTPS, and safe**, coexisting cleanly with Sujash's stack. The end-to-end assistant pipeline is **proven**. Four rough edges from live UAT are **fixed and tested locally, awaiting review → deploy → re-test → re-open sends**. Resume phrase for next session: **"finish the Lexi bug-fix deploy."**
