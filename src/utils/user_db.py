import sqlite3
import os
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
            disabled BOOL DEFAULT 0
        )
    """)
    
    # Add a default admin user if not exists and not disabled
    if not DISABLE_DEFAULT_ADMIN:
        cursor.execute("SELECT username FROM users WHERE username = 'admin'")
        if not cursor.fetchone():
            print("[Auth] Creating default admin user...")
            hashed_pw = get_password_hash("admin123")
            cursor.execute(
                "INSERT INTO users (username, hashed_password, full_name) VALUES (?, ?, ?)",
                ("admin", hashed_pw, "System Administrator")
            )
    
    conn.commit()
    conn.close()

def get_user(username: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT username, hashed_password, disabled FROM users WHERE username = ?", (username,))
    user = cursor.fetchone()
    conn.close()
    if user:
        return {"username": user[0], "hashed_password": user[1], "disabled": bool(user[2])}
    return None

if __name__ == "__main__":
    # Allow running as a script to init
    import sys
    sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
    init_user_db()
