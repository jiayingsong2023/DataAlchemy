"""Apply DataAlchemy PostgreSQL migrations."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.storage.postgres import PostgresDatabase


def main() -> None:
    database = PostgresDatabase(os.environ.get("DATABASE_URL", ""))
    for migration in database.migrate():
        print(f"applied {migration}")


if __name__ == "__main__":
    main()
