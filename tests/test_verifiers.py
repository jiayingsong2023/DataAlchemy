import os

import pytest

from src.storage.postgres import DatabaseError, PostgresDatabase

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"), reason="PostgreSQL integration database is required"
)


def test_verifier_transaction_rejects_writes():
    database = PostgresDatabase(os.getenv("VERIFIER_DATABASE_URL", os.environ["TEST_DATABASE_URL"]))
    identity = {"username": "alice", "tenant_id": "acme", "role": "user"}

    with pytest.raises(DatabaseError):
        with database.transaction(identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute("CREATE TABLE verifier_must_not_write (id integer)")
