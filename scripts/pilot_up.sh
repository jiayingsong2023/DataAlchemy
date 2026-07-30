#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "DATABASE_URL is required; use the Helm-generated application URL." >&2
  exit 2
fi

python scripts/migrate_postgres.py
python scripts/pilot_check.py
helm upgrade --install data-alchemy deploy/charts/data-alchemy
