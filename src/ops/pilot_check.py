"""Fail-fast pilot dependency checks."""

from __future__ import annotations

import os

from storage.postgres import PostgresDatabase


def check_environment(environment: dict[str, str]) -> dict[str, str | None]:
    missing = [
        name for name in ("DATABASE_URL", "REDIS_URL", "S3_ENDPOINT") if not environment.get(name)
    ]
    if missing:
        raise ValueError("missing configuration: " + ", ".join(missing))
    with PostgresDatabase(environment["DATABASE_URL"]).transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT extname FROM pg_extension WHERE extname = 'vector'")
            if cursor.fetchone() is None:
                raise RuntimeError("pgvector extension is not installed")
            cursor.execute("SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1")
            migration = cursor.fetchone()
    return {
        "postgres": "ok",
        "pgvector": "ok",
        "migration": migration["version"] if migration else None,
    }


def main() -> None:
    try:
        print(check_environment(dict(os.environ)))
    except (ValueError, RuntimeError) as error:
        raise SystemExit(str(error)) from error
