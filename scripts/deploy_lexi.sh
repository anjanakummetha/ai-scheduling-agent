#!/usr/bin/env bash
# Canonical Lexi deploy: fast-forward main, restart BOTH services, verify.
#
# Two services run this codebase and they restart independently:
#   lexi-hermes.service  gateway + MCP + worker           (:3978, :8780)
#   lexi-api.service     read-only API for the dashboard  (:8081, separate uvicorn)
#
# Restarting only lexi-hermes leaves api_v1.py changes serving stale code — a new
# endpoint 404s and looks like it was never deployed.
#
# Usage:  ssh root@<host> 'bash -s' < scripts/deploy_lexi.sh
#         ssh root@<host> 'NEW_SESSION=1 bash -s' < scripts/deploy_lexi.sh
#
# NEW_SESSION=1 clears the Hermes session store so the gateway comes up with a
# fresh tool list. Required whenever a deploy changes an MCP tool signature —
# see the HERMES SESSION block below. Off by default because it also drops the
# continuity of whatever Kory is mid-conversation about.
set -uo pipefail

APP=/home/lexi/AI_Scheduling_Agent
G="git -c safe.directory=$APP"
cd "$APP" || exit 1
STAMP=$(date +%Y%m%d-%H%M%S)
KEEP_DEPLOY_BACKUPS=3
SESSIONS=/home/lexi/.hermes/sessions/sessions.json

echo "########## BACKUP ##########"
cp .env ".env.bak.deploy.$STAMP" && echo "  env -> .env.bak.deploy.$STAMP"
sqlite3 data/lexi.db ".backup data/lexi-deploy-$STAMP.db" && echo "  db  -> data/lexi-deploy-$STAMP.db"

# Each deploy snapshots the whole DB (~90MB and growing). Without this, a heavy
# deploy day adds >1GB and never gives it back — 96 files reached 7.3GB before the
# first prune. Only the `.deploy.` env backups rotate here; named rollback
# checkpoints (.env.bak.<label>.<stamp>) are deliberate and must survive.
ls -t data/lexi-deploy-*.db 2>/dev/null | tail -n +$((KEEP_DEPLOY_BACKUPS + 1)) | xargs -r rm -f
ls -t .env.bak.deploy.*   2>/dev/null | tail -n +$((KEEP_DEPLOY_BACKUPS + 1)) | xargs -r rm -f
echo "  kept newest $KEEP_DEPLOY_BACKUPS deploy backups ($(df -h / | awk 'NR==2{print $4}') free)"

echo
echo "########## DEPLOY ##########"
echo "  before: $($G log --oneline -1)"
# data/kory_voice_profile.json is a regenerable cache — rebuilt from sent mail on
# first use — that used to be tracked. Discard any local edit so the merge can
# fast-forward. This replaced a stash/pop dance, which failed on the very commit
# that untracked the file: the pop conflicted, left it tracked and un-ignored,
# and stranded a stash entry on the box.
# Must be `checkout HEAD --`, not `checkout --`: the box carried a *staged*
# modification (git status "M " — index, not working tree), left behind by the
# old stash/pop path. `checkout --` restores the working tree from the index, so
# it cannot clear a staged change and the merge still aborted.
$G checkout -q HEAD -- data/kory_voice_profile.json 2>/dev/null || true
BEFORE_SHA=$($G rev-parse HEAD)
$G fetch origin main -q
if ! $G merge --ff-only origin/main; then
  echo "  !!! MERGE FAILED — nothing restarted."
  exit 1
fi
echo "  after:  $($G log --oneline -1)"
AFTER_SHA=$($G rev-parse HEAD)

echo
echo "########## HERMES SESSION ##########"
# A deploy alone is not enough for Lexi to notice a new or changed tool. A clean
# gateway restart RESUMES the previous session, tool list and all, so a tool
# shipped minutes ago is reported as not existing. Clearing the session store is
# the only thing that makes the new list take effect.
#
# Opt-in rather than automatic: clearing it also ends whatever thread Kory is in
# the middle of, which is not a price worth paying on a deploy that only touches
# orchestrator internals.
TOOL_SIGNATURES_CHANGED=0
if [ "$BEFORE_SHA" != "$AFTER_SHA" ] &&
   $G diff --name-only "$BEFORE_SHA" "$AFTER_SHA" | grep -q '^hermes_mcp_server\.py$'; then
  TOOL_SIGNATURES_CHANGED=1
fi

if [ "${NEW_SESSION:-0}" = "1" ]; then
  if [ -f "$SESSIONS" ]; then
    # Moved, not deleted — a bad deploy should be recoverable, and the file is tiny.
    mv "$SESSIONS" "$SESSIONS.bak.$STAMP"
    ls -t "$SESSIONS".bak.* 2>/dev/null | tail -n +$((KEEP_DEPLOY_BACKUPS + 1)) | xargs -r rm -f
    echo "  cleared -> sessions.json.bak.$STAMP (Kory's next message starts a new session)"
  else
    echo "  nothing to clear — $SESSIONS is absent"
  fi
elif [ "$TOOL_SIGNATURES_CHANGED" = "1" ]; then
  echo "  !!! hermes_mcp_server.py CHANGED but NEW_SESSION was not set."
  echo "  !!! The restart below will resume the OLD session with its stale tool list,"
  echo "  !!! and Lexi will tell Kory the new tools do not exist."
  echo "  !!! Re-run:  ssh root@<host> 'NEW_SESSION=1 bash -s' < scripts/deploy_lexi.sh"
else
  echo "  kept — no tool-signature change, so Kory's conversation continues"
fi

echo
echo "########## RESTART BOTH SERVICES ##########"
systemctl restart lexi-hermes.service
systemctl restart lexi-api.service
sleep 8
echo "  lexi-hermes: $(systemctl is-active lexi-hermes.service)"
echo "  lexi-api   : $(systemctl is-active lexi-api.service)"

echo
echo "########## VERIFY ##########"
curl -s --max-time 15 http://127.0.0.1:8780/api/health; echo
TOKEN=$(grep -E "^LEXI_API_TOKEN=" .env | cut -d= -f2- | tr -d '"' | tr -d "'")
if [ -n "$TOKEN" ]; then
  echo -n "  api /health: "
  curl -s -o /dev/null -w "HTTP %{http_code}\n" --max-time 8 \
    -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8081/api/v1/health
fi
LEXI_ENV=production .venv/bin/python -c "
import json, app.config as c
print('  posture:', json.dumps(c.safety_posture_summary()))
"

echo
echo "########## CO-TENANT UNTOUCHED ##########"
docker ps --format '  {{.Names}}  {{.Status}}' | grep -E 'hermes-agent|traefik' || echo "  !! container missing — investigate"
