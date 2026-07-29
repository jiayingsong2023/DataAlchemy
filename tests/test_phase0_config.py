from pathlib import Path

import pytest

from src import config
from src.utils import auth, user_db


def test_production_rejects_default_credentials(monkeypatch):
    monkeypatch.setattr(config, "ENVIRONMENT", "production")
    monkeypatch.setenv("AUTH_SECRET_KEY", config.DEFAULT_AUTH_SECRET)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "minioadmin")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "minioadmin")
    monkeypatch.setenv("DISABLE_DEFAULT_ADMIN", "false")

    with pytest.raises(RuntimeError, match="Invalid production configuration"):
        config.validate_config()


def test_production_accepts_explicit_credentials(monkeypatch):
    monkeypatch.setattr(config, "ENVIRONMENT", "production")
    monkeypatch.setenv("AUTH_SECRET_KEY", "a" * 32)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "storage-user")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "storage-password")
    monkeypatch.setenv("DISABLE_DEFAULT_ADMIN", "true")

    config.validate_config()


def test_user_database_migrates_tenant_and_role(tmp_path, monkeypatch):
    database = Path(tmp_path) / "users.db"
    monkeypatch.setattr(user_db, "DB_PATH", str(database))
    monkeypatch.setattr(config, "DISABLE_DEFAULT_ADMIN", False)

    user_db.init_user_db()

    admin = user_db.get_user("admin")
    assert admin["tenant_id"] == config.DEFAULT_TENANT_ID
    assert admin["role"] == "admin"


def test_token_carries_tenant_and_role():
    token = auth.create_access_token({"sub": "alice", "tenant_id": "acme", "role": "admin"})
    assert auth.decode_identity(token) == {"username": "alice", "tenant_id": "acme", "role": "admin"}
