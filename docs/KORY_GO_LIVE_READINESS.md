# Kory go-live readiness — prepared 2026-08-06

State of the system the night before Kory uses it directly. Written to be read by
someone who was not in the session.

---

## Posture at go-live

Reads and writes are ON for both HubSpot and Asana (Anjana's call, 2026-08-06 — she tested
Asana writes; HubSpot writes were tested in-session, see below). Two things are deliberately
scrapped for now and **Kory has already been told** they do nothing: emailing Lexi directly,
and outreach campaigns.

| Capability | State | Why |
|---|---|---|
| Scheduling (offer, hold, confirm, reschedule, cancel) | **ON** | Proven live, incl. E-3/E-4/E-6 |
| Email drafting + sending (Lexi voice and Kory voice) | **ON** | Proven live |
| Teams cards + approvals | **ON** | Proven live |
| HubSpot **reads** (lookups, pre-briefs, deals, health) | **ON** | Proven live |
| Asana **reads** | **ON** | Proven 2026-07-28 |
| Morning briefing email | **ON** | Sends 4:45 AM MT; content not yet eyeballed by a human |
| **HubSpot writes** (meeting notes) | **ON** | Live-tested 2026-08-05, all 4 guardrails pass |
| **Asana writes** | **ON** | Anjana tested these |
| **HubSpot BCC logging** | **OFF** | Does not work. Parked, see OPEN ISSUES #1 |
| **Outreach campaigns** | **OFF** | Scrapped for now; Kory informed |
| **Emailing lexi@ directly** | **N/A** | No ingress watches that mailbox; Kory informed |

---

## What was verified live, and when

- **E-6 conflict-at-confirm** (2026-08-06) — the guard did not exist and was written that
  session. A meeting booked over an accepted slot is now caught at confirm time: no invite,
  holds retained, audited. Verified against the real calendar.
- **E-3 hold reminder** (2026-08-06) — stages a drafted nudge for approval BEFORE any release,
  sends nothing on its own.
- **E-4 hold expiry** (2026-08-06) — releases after expiry and deletes the events from Outlook.
  Verified: 0 HOLD events left on the calendar.
- **HubSpot note write + all four guardrails** (2026-08-05) — unknown contact, unapproved,
  fuzzy match, and another owner's contact all correctly refused. The note itself lands with
  the right body on the right record.
- **MCP tools no longer block the event loop** (2026-08-06) — measured: a 0.5s blocking tool
  used to freeze the server completely; now it does not.

---

## Known rough edges Kory may hit

1. **Emailing `lexi@iconicfounders.com` directly does nothing.** No trigger and no poll watches
   that mailbox; every working flow runs through Kory's. Teams chat, or CC her on a mail to
   Kory. Kory has been told. (Scrapped rather than fixed for now.)
2. **Inbound mail can lag up to ~5 minutes.** The webhook occasionally drops a message because
   Microsoft Graph 404s on a just-arrived id (its own error says to retry later); the 5-minute
   backup poll recovers it. 14 such drops in 48h. Delay, not loss — verified that proposals
   continued to be created within minutes of each burst.
3. **A Teams command can still take a long time.** The event-loop fix stops one slow tool taking
   down everything else, but does not make Composio calls faster. A slot search doing several
   retrying calendar reads can still approach the 120s tool timeout.
4. **The morning briefing has never been read by a human.** It sends; nobody has checked the
   content is right. Anjana is verifying the 2026-08-06 send.

---

## The HubSpot-writes decision

If Kory wants to file meeting notes to HubSpot tomorrow, the flag must be flipped. Facts for
that decision:

**In favour:** every path was live-tested on 2026-08-05. The note lands correctly, and all four
guardrails refuse correctly, including writing to a contact owned by someone else.

**Against:** the approval is an argument the *model* supplies. `lexi_hubspot_meeting_note(confirm=...)`
flows straight into `assert_kory_approved_write(approved=confirm)`, and `owner_ack` is the entire
other-owner guard. Nothing independently verifies Kory said yes — unlike scheduling, which has a
proposals table, a typed `approve #N`, and an audited `decision_source`. Hermes has a logged
history of claiming things were done that were not. With the flag off, that risk is zero.

**Decision (Anjana, 2026-08-06): ON.** Both HubSpot and Asana writes are enabled. The residual
risk above is accepted and stands as a design item — worth replacing the model-supplied boolean
with a typed approval, as scheduling already has, rather than leaving it as the only thing
between Hermes and a CRM write.

**Rollback:** `cp .env.bak.20260805-222013 .env` then `systemctl restart lexi-hermes lexi-api`,
or set the two `*_LIVE_WRITES_ENABLED` flags back to `false` and restart both.

---

## Pre-flight state at handover (2026-08-06 ~02:20 UTC)

- `lexi-hermes` and `lexi-api`: **active**; worker health `ok`, heartbeat fresh, DB writable
- **0 actionable proposals, 0 live holds** — every `[TEST]` thread rejected, calendar clean
- `kory_memory`: **empty** — the Tuesday-8:30 fact was a test artifact and was removed
- Composio budget: **7.2%** month-to-date, no alarm
- Timers: watchdog (5 min), backup (hourly), morning briefing (4:45 AM MT) all firing
- Test suite: **615 passing**
- Posture: `LEXI_DRY_RUN=false`, `LEXI_REQUIRE_KORY_APPROVAL=true`,
  `LEXI_AUTO_EXECUTE_ENABLED=false`, `LEXI_HUBSPOT_LIVE_WRITES_ENABLED=true`,
  `LEXI_ASANA_LIVE_WRITES_ENABLED=true`, outreach flags false

## If something goes wrong tomorrow

- Health: `curl -s http://127.0.0.1:8780/api/health`
- **Real logs are files, not journalctl** — `logs/lexi.log` and `~/.hermes/logs/*.log`.
  `journalctl -u lexi-hermes` contains no application logging at all.
- Kill switch for outbound mail: `LEXI_KORY_OUTBOUND_BLOCKED=true`, then restart both services.
- Full stop: `systemctl stop lexi-hermes` — inbound processing and all sends cease.
