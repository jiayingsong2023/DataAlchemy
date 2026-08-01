"""Small PostgreSQL access layer with transaction-scoped tenant context."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

MIGRATIONS_DIR = Path(__file__).with_name("migrations")


class DatabaseError(RuntimeError):
    """Raised when PostgreSQL persistence cannot be used safely."""


class PostgresDatabase:
    """Open one short-lived, RLS-scoped transaction per operation.

    A connection pool is deliberately avoided here: ``SET LOCAL`` is cleared with
    the transaction, so this minimal adapter cannot leak tenant state between
    requests. Add a pool only after measured connection setup warrants it.
    """

    def __init__(self, database_url: str):
        self.database_url = database_url

    @staticmethod
    def _driver() -> Any:
        try:
            import psycopg
        except ImportError as error:  # pragma: no cover - depends on deployment image
            raise DatabaseError("psycopg is required for PostgreSQL persistence") from error
        return psycopg

    @contextmanager
    def transaction(
        self, identity: dict[str, str] | None = None, *, read_only: bool = False
    ) -> Iterator[Any]:
        """Yield a transaction with database-enforced request scope."""
        if not self.database_url:
            raise DatabaseError("DATABASE_URL is required")
        psycopg = self._driver()
        try:
            with psycopg.connect(
                self.database_url, row_factory=psycopg.rows.dict_row
            ) as connection:
                with connection.transaction():
                    if read_only:
                        with connection.cursor() as cursor:
                            cursor.execute("SET TRANSACTION READ ONLY")
                    if identity is not None:
                        required = {"tenant_id", "username", "role"}
                        if not required <= identity.keys():
                            raise DatabaseError("identity needs tenant_id, username and role")
                        with connection.cursor() as cursor:
                            for key, value in (
                                ("app.tenant_id", identity["tenant_id"]),
                                ("app.user_id", identity["username"]),
                                ("app.role", identity["role"]),
                            ):
                                cursor.execute("SELECT set_config(%s, %s, true)", (key, value))
                    yield connection
        except DatabaseError:
            raise
        except psycopg.Error as error:
            raise DatabaseError("PostgreSQL transaction failed") from error

    def migrate(self) -> list[str]:
        """Apply each versioned SQL migration exactly once."""
        applied: list[str] = []
        with self.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations "
                    "(version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
                )
                for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
                    version = migration.name
                    cursor.execute("SELECT 1 FROM schema_migrations WHERE version = %s", (version,))
                    if cursor.fetchone():
                        continue
                    cursor.execute(migration.read_text(encoding="utf-8"))
                    cursor.execute(
                        "INSERT INTO schema_migrations (version) VALUES (%s)", (version,)
                    )
                    applied.append(version)
        return applied
