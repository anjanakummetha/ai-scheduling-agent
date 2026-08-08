# Lexi — Session Handoff (updated 2026-08-07)

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

## 2. ACTIVE ISSUE — Graph throttling the backup poll

36 `outlook_poll` failures since go-live (~2–4/hr): 20 APITimeout, 10 `CommandConcurrencyLimitReached`, 4 `ErrorTooManyObjectsOpened`, 2 `ApplicationThrottled`. The poll is the **safety net** for webhook drops (Graph 404s just-arrived message ids, ~14/48h; recovery ≤5 min). If both degrade together, mail sits unseen longer.

Proposed fix (not built): stagger the inbox / sentitems polls across cycles instead of same-cycle; consider skipping sentitems entirely — 86 of the 374 triaged messages were Kory's own outbound YPO mail (he runs YPO outreach by hand; Lexi triages every copy).

## 3. OPEN WORK LIST (rough order)

1. **M-2** — Anjana verifies morning-briefing content (08-06 and 08-07 sends in Kory's inbox)
2. **Throttling fix** (§2)
3. **Log + fix Kory's change requests** as they arrive
4. **M-3 redo** against `logs/lexi.log` (the journalctl sweep was void)
5. **Per-tool deadline** — the event-loop fix (`17b8b62`) stops one slow tool freezing the server, but a single tool doing 3×30s Composio retries can still blow the 120s MCP budget
6. **HubSpot step 5** — Kory sign-off package (never built)
7. **Design item (accepted risk, worth fixing not re-raising):** HubSpot/Asana write approval is a model-supplied boolean; scheduling's typed `approve #N` + audited `decision_source` is the pattern to copy
8. **Cleanup leftovers:** delete Aug 10 (#7041) + declined Aug 24 (#6861) test meetings via calendar; sweep `[TEST]` emails + Lexi drafts. (#6235/#6481/#7181 already rejected; queue was 0 proposals / 0 holds / 0 memory facts at go-live.)

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
