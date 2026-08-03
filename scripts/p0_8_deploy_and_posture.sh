#!/usr/bin/env bash
# P0-8: deploy the signature/logo fix + apply the Phase 1 test-window posture.
# Read-only until step 0 completes; every mutation is backed up first.
set -uo pipefail

APP=/home/lexi/AI_Scheduling_Agent
G="git -c safe.directory=$APP"
cd "$APP" || exit 1
STAMP=$(date +%Y%m%d-%H%M%S)

echo "########## 0. BACKUPS ##########"
cp .env ".env.bak.phase0.$STAMP" && echo "env  -> .env.bak.phase0.$STAMP"
sqlite3 data/lexi.db ".backup data/lexi-prephase1-$STAMP.db" && echo "db   -> data/lexi-prephase1-$STAMP.db"

echo
echo "########## 1. DEPLOY latest main ##########"
echo "before: $($G log --oneline -1)"
STASHED=0
if ! $G diff --quiet -- data/kory_voice_profile.json 2>/dev/null; then
  $G stash push -q -- data/kory_voice_profile.json && STASHED=1 && echo "stashed local kory_voice_profile.json"
fi
$G fetch origin main -q
if ! $G merge --ff-only origin/main; then
  echo "!!! MERGE FAILED — aborting before any posture change. Nothing else was touched."
  [ "$STASHED" = "1" ] && $G stash pop -q
  exit 1
fi
[ "$STASHED" = "1" ] && $G stash pop -q && echo "restored kory_voice_profile.json"
echo "after:  $($G log --oneline -1)"

echo
echo "########## 2. ENV POSTURE ##########"
setenv() {
  k="$1"; v="$2"
  if grep -qE "^${k}=" .env; then
    sed -i "s|^${k}=.*|${k}=${v}|" .env
  else
    printf '%s=%s\n' "$k" "$v" >> .env
    echo "  (key was absent — appended)"
  fi
  grep -nE "^${k}=" .env | sed 's/^/  /'
}
setenv LEXI_SIGNATURE_EMBED_LOGO      true    # PF-2: explicit false was overriding the new default
setenv LEXI_KORY_OUTBOUND_BLOCKED     true    # D-5: close sends for Phase 1, re-open at Phase 2
setenv LEXI_ASANA_LIVE_WRITES_ENABLED false   # D-6: Kory's real Asana — staging only this window
setenv LEXI_TEAMS_INBOUND_NOTIFY_MODE important  # D-1: cold inbound must reach Teams

echo
echo "########## 3. CLEAR STALE TEST PROPOSALS (D-7) ##########"
SEL="status in ('pending_approval','awaiting_reply_prompt') and created_at < '2026-08-01'"
echo "to clear: $(sqlite3 data/lexi.db "select count(*) from proposals where $SEL;")"
sqlite3 data/lexi.db "update proposals set status='rejected' where $SEL;"
echo "still actionable after cleanup (expect empty):"
sqlite3 -header -column data/lexi.db \
  "select status, count(*) n from proposals where status in ('pending_approval','awaiting_reply_prompt') group by status;" | sed 's/^/  /'

echo
echo "########## 4. RESTART (lexi-hermes only) ##########"
systemctl restart lexi-hermes.service
sleep 8
echo "is-active: $(systemctl is-active lexi-hermes.service)"

echo
echo "########## 5. RE-VERIFY P0-1 / P0-2 ##########"
curl -s --max-time 15 http://127.0.0.1:8780/api/health; echo
LEXI_ENV=production .venv/bin/python -c \
  "import app.config as c,json;print(json.dumps(c.safety_posture_summary(),indent=2))"

echo
echo "########## 6. NEW BASELINE MARKS (P0-7 refresh) ##########"
sqlite3 data/lexi.db \
  "select 'proposals', coalesce(max(id),0) from proposals
   union all select 'holds', coalesce(max(id),0) from holds
   union all select 'audit_log', coalesce(max(id),0) from audit_log;" | sed 's/^/  /'
date -u '+  marked at %Y-%m-%d %H:%M:%SZ'

echo
echo "########## 7. SUJASH'S CONTAINER — UNTOUCHED? ##########"
docker ps --format '  {{.Names}}  {{.Status}}' | grep -E 'hermes-agent|traefik' || echo "  !! container not found — investigate"
