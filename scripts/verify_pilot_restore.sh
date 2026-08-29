#!/usr/bin/env bash
set -euo pipefail

: "${PILOT_DATABASE_URL:?source PostgreSQL URL is required}"
: "${PILOT_RESTORE_DATABASE_URL:?pre-created isolated restore PostgreSQL URL is required}"

archive=$(mktemp "${TMPDIR:-/tmp}/dataalchemy-pilot-restore.XXXXXX.dump")
trap 'rm -f "$archive"' EXIT
pg_dump --format=custom --file="$archive" "$PILOT_DATABASE_URL"
archive_sha256=$(sha256sum "$archive" | cut -d ' ' -f 1)
pg_restore --clean --if-exists --no-owner --dbname="$PILOT_RESTORE_DATABASE_URL" "$archive"
psql "$PILOT_RESTORE_DATABASE_URL" --no-align --tuples-only --command \
  "SELECT extname FROM pg_extension WHERE extname = 'vector';" | grep -qx vector
psql "$PILOT_RESTORE_DATABASE_URL" --no-align --tuples-only --command \
  "SELECT count(*) >= 0 FROM connector_cursors;" | grep -qx t
psql "$PILOT_RESTORE_DATABASE_URL" --no-align --tuples-only --command \
  "SELECT count(*) >= 0 FROM connector_runs;" | grep -qx t
psql "$PILOT_RESTORE_DATABASE_URL" --no-align --tuples-only --command \
  "SELECT count(*) >= 0 FROM audit_events;" | grep -qx t
psql "$PILOT_RESTORE_DATABASE_URL" --no-align --tuples-only --command \
  "SELECT count(*) >= 0 FROM memory_policy_events;" | grep -qx t
psql "$PILOT_RESTORE_DATABASE_URL" --no-align --tuples-only --command \
  "SELECT count(*) >= 0 FROM release_records;" | grep -qx t
if [[ -n "${RESTORE_EVIDENCE_PATH:-}" ]]; then
  mkdir -p "$(dirname "$RESTORE_EVIDENCE_PATH")"
  RESTORE_ARCHIVE_SHA256="$archive_sha256" python3 - "$RESTORE_EVIDENCE_PATH" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

Path(sys.argv[1]).write_text(
    json.dumps(
        {
            "schema_version": "pilot_restore_evidence.v1",
            "status": "passed",
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "commit": os.getenv("GITHUB_SHA", "local"),
            "archive_sha256": os.environ["RESTORE_ARCHIVE_SHA256"],
        },
        sort_keys=True,
    )
    + "\n",
    encoding="utf-8",
)
PY
fi
echo "pilot restore verification passed"
