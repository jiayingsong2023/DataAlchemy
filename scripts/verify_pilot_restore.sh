#!/usr/bin/env bash
set -euo pipefail

: "${PILOT_DATABASE_URL:?source PostgreSQL URL is required}"
: "${PILOT_RESTORE_DATABASE_URL:?pre-created isolated restore PostgreSQL URL is required}"

archive=$(mktemp "${TMPDIR:-/tmp}/dataalchemy-pilot-restore.XXXXXX.dump")
trap 'rm -f "$archive"' EXIT
pg_dump --format=custom --file="$archive" "$PILOT_DATABASE_URL"
pg_restore --clean --if-exists --no-owner --dbname="$PILOT_RESTORE_DATABASE_URL" "$archive"
psql "$PILOT_RESTORE_DATABASE_URL" --no-align --tuples-only --command \
  "SELECT extname FROM pg_extension WHERE extname = 'vector';" | grep -qx vector
psql "$PILOT_RESTORE_DATABASE_URL" --no-align --tuples-only --command \
  "SELECT count(*) >= 0 FROM connector_cursors;" | grep -qx t
psql "$PILOT_RESTORE_DATABASE_URL" --no-align --tuples-only --command \
  "SELECT count(*) >= 0 FROM connector_runs;" | grep -qx t
echo "pilot restore verification passed"
