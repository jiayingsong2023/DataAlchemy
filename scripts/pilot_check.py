"""Fail-fast local pilot diagnostics without exposing credentials."""

from __future__ import annotations

import os

from src.storage.postgres import PostgresDatabase


def main() -> None:
    missing = [name for name in ("DATABASE_URL", "REDIS_URL", "S3_ENDPOINT") if not os.getenv(name)]
    if missing:
        raise SystemExit("missing configuration: " + ", ".join(missing))
    with PostgresDatabase(os.environ["DATABASE_URL"]).transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT extname FROM pg_extension WHERE extname = 'vector'")
            if cursor.fetchone() is None:
                raise SystemExit("pgvector extension is not installed")
            cursor.execute("SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1")
            migration = cursor.fetchone()
    latest_migration = migration["version"] if migration else None
    print({"postgres": "ok", "pgvector": "ok", "migration": latest_migration})


if __name__ == "__main__":
    main()
