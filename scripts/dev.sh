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
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || { echo "no .venv — run ./manage.sh setup first" >&2; exit 1; }

PGBIN=""
for candidate in /usr/lib/postgresql/16/bin /usr/lib/postgresql/15/bin /usr/local/bin /usr/bin; do
  [ -x "$candidate/initdb" ] && PGBIN="$candidate" && break
done
[ -n "$PGBIN" ] || { echo "postgres binaries not found" >&2; exit 1; }

PGDIR="$(mktemp -d /tmp/kyc-dev-pg.XXXXXX)"
PGPORT="${KYC_DEV_PG_PORT:-55433}"
API_PORT="${KYC_DEV_API_PORT:-8080}"
RECEIVER_PORT="${KYC_DEV_RECEIVER_PORT:-9099}"
PIDS=()

cleanup() {
  echo; echo "shutting down…"
  for pid in "${PIDS[@]:-}"; do kill "$pid" 2>/dev/null || true; done
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

echo "→ migrations"
"$ROOT/.venv/bin/alembic" upgrade head >/dev/null

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
  │                                                     │
  │  Try: Composer → send kyb.run_requested (template   │
  │  is Acme) → open the case → watch checks/score/     │
  │  gates → email.verified → org_id.submitted → ✅     │
  └─────────────────────────────────────────────────────┘
  Ctrl-C stops everything and removes the ephemeral DB.

EOF
wait
