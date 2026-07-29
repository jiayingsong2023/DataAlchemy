import os
import sqlite3

from config import DEFAULT_TENANT_ID
from utils.auth import get_password_hash

DB_PATH = "data/users.db"

def init_user_db():
    from config import DISABLE_DEFAULT_ADMIN
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            hashed_password TEXT NOT NULL,
            full_name TEXT,
            disabled BOOL DEFAULT 0,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            role TEXT NOT NULL DEFAULT 'user'
        )
    """)

    columns = {row[1] for row in cursor.execute("PRAGMA table_info(users)")}
    if "tenant_id" not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'")
    if "role" not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")

    # Add a default admin user if not exists and not disabled
    if not DISABLE_DEFAULT_ADMIN:
        cursor.execute("SELECT username FROM users WHERE username = 'admin'")
        if not cursor.fetchone():
            print("[Auth] Creating default admin user...")
            hashed_pw = get_password_hash("admin123")
            cursor.execute(
                "INSERT INTO users (username, hashed_password, full_name, tenant_id, role) VALUES (?, ?, ?, ?, ?)",
                ("admin", hashed_pw, "System Administrator", DEFAULT_TENANT_ID, "admin"),
            )

    conn.commit()
    conn.close()

def get_user(username: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT username, hashed_password, disabled, tenant_id, role FROM users WHERE username = ?", (username,)
    )
    user = cursor.fetchone()
    conn.close()
    if user:
        return {
            "username": user[0],
            "hashed_password": user[1],
            "disabled": bool(user[2]),
            "tenant_id": user[3],
            "role": user[4],
        }
    return None

if __name__ == "__main__":
    # Allow running as a script to init
    import sys
    sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
    init_user_db()
