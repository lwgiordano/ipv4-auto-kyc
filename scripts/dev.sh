#!/usr/bin/env bash
# One-command local stack: ephemeral Postgres → migrations → API (+ /ui ops
# console) → dev worker (fixture adapters) → fake platform callback receiver.
#
#   bash scripts/dev.sh          # Ctrl-C tears everything down
#
# Needs: postgres binaries (initdb/pg_ctl), the project venv (./manage.sh setup).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$ROOT"
# Local secrets and overrides. .env is git-ignored; export every line of it so variables the
# tool reads straight from the environment (CH_API_KEY, ARIN_API_KEY) reach the workers too —
# the settings loader only reads the KYC_-prefixed ones from the file by itself. One KEY=value
# per line; quote a value that contains spaces. Anything this script exports below still wins.
if [ -f .env ]; then set -a; . ./.env; set +a; fi
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || { echo "no .venv — run ./manage.sh setup first" >&2; exit 1; }

PGBIN=""
if command -v initdb >/dev/null 2>&1; then
  PGBIN="$(dirname "$(command -v initdb)")"
fi
if [ -z "$PGBIN" ]; then  # not on PATH — probe the usual apt / Homebrew keg / Postgres.app homes
  for candidate in /usr/lib/postgresql/16/bin /usr/lib/postgresql/15/bin \
      /opt/homebrew/opt/postgresql@17/bin /opt/homebrew/opt/postgresql@16/bin \
      /opt/homebrew/opt/postgresql@15/bin \
      /Applications/Postgres.app/Contents/Versions/latest/bin \
      /usr/local/bin /usr/bin; do
    [ -x "$candidate/initdb" ] && PGBIN="$candidate" && break
  done
fi
[ -n "$PGBIN" ] || { echo "postgres binaries not found (apt install postgresql, or brew install postgresql@16)" >&2; exit 1; }

PGDIR="$(mktemp -d /tmp/kyc-dev-pg.XXXXXX)"
PGPORT="${KYC_DEV_PG_PORT:-55433}"
API_PORT="${KYC_DEV_API_PORT:-8080}"
RECEIVER_PORT="${KYC_DEV_RECEIVER_PORT:-9099}"
PIDS=()

cleanup() {
  echo; echo "shutting down…"
  # ${PIDS[@]:-} on an empty array is an unbound-variable error under `set -u` in bash 3.2,
  # which is what macOS ships -- and it aborts cleanup, leaving Postgres running.
  for pid in ${PIDS[@]+"${PIDS[@]}"}; do kill "$pid" 2>/dev/null || true; done
  as_pg_user "$PGBIN/pg_ctl -D $PGDIR/data -m immediate stop" 2>/dev/null || true
  rm -rf "$PGDIR"
}
trap cleanup EXIT INT TERM

as_pg_user() {  # postgres refuses root; hop to ubuntu when needed
  if [ "$(id -u)" = "0" ]; then su -s /bin/bash ubuntu -c "$1"; else bash -c "$1"; fi
}

echo "→ ephemeral postgres on :$PGPORT"
[ "$(id -u)" = "0" ] && chown -R ubuntu:ubuntu "$PGDIR"
as_pg_user "$PGBIN/initdb -D $PGDIR/data -U kyc -A trust" >/dev/null
as_pg_user "$PGBIN/pg_ctl -D $PGDIR/data -o '-p $PGPORT -k $PGDIR -c fsync=off' -l $PGDIR/log start" >/dev/null
sleep 1
as_pg_user "$PGBIN/createdb -h 127.0.0.1 -p $PGPORT -U kyc kyc_dev"

export KYC_DATABASE_URL="postgresql+psycopg://kyc@127.0.0.1:$PGPORT/kyc_dev"
export KYC_PLATFORM_CALLBACK_URL="http://127.0.0.1:$RECEIVER_PORT"
export KYC_PLATFORM_HMAC_SECRET="${KYC_PLATFORM_HMAC_SECRET:-dev-secret}"
# ui_enabled defaults to false in Settings; the console this stack advertises needs it on
export KYC_UI_ENABLED="${KYC_UI_ENABLED:-true}"

echo "→ migrations"
"$ROOT/.venv/bin/alembic" upgrade head >/dev/null

# Scoring, brokers and Salesforce mappings are editable in the console only after the drained
# activation production uses: bundle pinning epoch, then live configuration revision 1. A fresh
# database with nothing running yet IS the drained state, so this stack activates on every
# start. Every process below inherits the flag and the credential; the console sends the
# credential with each save (the app's proxy pre-fills it; by hand, paste it under Options).
export KYC_UI_ADMIN_TOKEN="${KYC_UI_ADMIN_TOKEN:-dev-admin}"
export KYC_ENFORCE_BUNDLE_PINNING=true
echo "→ live configuration (bundle pinning epoch, revision 1)"
{ read -r BUNDLE_HASH; read -r ENGINE; } < <("$PY" -c 'from kyc_tool.config import get_settings
from kyc_tool.domain.engine import ENGINE_BUILD_ID
from kyc_tool.policy.loader import load_policy
print(load_policy(get_settings().policy_dir).bundle_hash); print(ENGINE_BUILD_ID)')
"$PY" -m kyc_tool.ops.seed_policy_bundle --expect-hash "$BUNDLE_HASH" >/dev/null
"$PY" -m kyc_tool.ops.activate_bundle_pinning_epoch \
  --expect-bundle-hash "$BUNDLE_HASH" --expect-engine "$ENGINE" >/dev/null
"$PY" -m kyc_tool.ops.activate_live_configuration \
  --apply --attest-writers-stopped --operator-label dev-stack >/dev/null

echo "→ fake platform receiver on :$RECEIVER_PORT"
"$PY" scripts/dev_receiver.py "$RECEIVER_PORT" & PIDS+=($!)

echo "→ dev worker (fixture adapters: Acme Networks Ltd walks to approve)"
"$PY" -m kyc_tool.workers.dev_worker & PIDS+=($!)

echo "→ API + ops console on :$API_PORT"
"$ROOT/.venv/bin/uvicorn" kyc_tool.api.app:create_app --factory \
  --host 127.0.0.1 --port "$API_PORT" --log-level warning & PIDS+=($!)
sleep 2

cat <<EOF

  ┌─────────────────────────────────────────────────────┐
  │  Ops console   http://127.0.0.1:$API_PORT/ui              │
  │  API           http://127.0.0.1:$API_PORT               │
  │  Callbacks     .substrate/state/callbacks.log        │
  │  Editing       Scoring, Brokers, Mappings are live;  │
  │                credential $KYC_UI_ADMIN_TOKEN        │
  │                                                     │
  │  Try: Composer → send kyb.run_requested (template   │
  │  is Acme) → open the case → watch checks/score/     │
  │  gates → email.verified → org_id.submitted → ✅     │
  └─────────────────────────────────────────────────────┘
  Ctrl-C stops everything and removes the ephemeral DB.

EOF
wait
