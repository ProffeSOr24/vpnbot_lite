import secrets
import sqlite3
import time
import uuid
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    name        TEXT UNIQUE NOT NULL,
    uuid        TEXT NOT NULL,
    hy_pass     TEXT NOT NULL,
    token       TEXT UNIQUE NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    expire_at   INTEGER,
    limit_bytes INTEGER,
    up          INTEGER NOT NULL DEFAULT 0,
    down        INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class DB:
    def __init__(self, path: Path):
        self.path = path
        self.conn: sqlite3.Connection | None = None
        self.open()

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        if self.conn:
            self.conn.close()
            self.conn = None

    # --- settings ---
    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    # --- users ---
    def users(self, only_enabled: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM users"
        if only_enabled:
            sql += " WHERE enabled=1"
        return self.conn.execute(sql + " ORDER BY name").fetchall()

    def user(self, user_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()

    def user_by_name(self, name: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM users WHERE name=?", (name,)).fetchone()

    def user_by_token(self, token: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone()

    def add_user(self, name: str, expire_at: int | None = None, limit_bytes: int | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO users(name, uuid, hy_pass, token, expire_at, limit_bytes, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            (name, str(uuid.uuid4()), secrets.token_urlsafe(16), secrets.token_urlsafe(16),
             expire_at, limit_bytes, int(time.time())),
        )
        return cur.lastrowid

    def update_user(self, user_id: int, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE users SET {cols} WHERE id=?", (*fields.values(), user_id))

    def delete_user(self, user_id: int) -> None:
        self.conn.execute("DELETE FROM users WHERE id=?", (user_id,))

    def add_traffic(self, name: str, up: int, down: int) -> None:
        self.conn.execute("UPDATE users SET up=up+?, down=down+? WHERE name=?", (up, down, name))

    def backup_to(self, dest: Path) -> None:
        target = sqlite3.connect(dest)
        with target:
            self.conn.backup(target)
        target.close()

    @staticmethod
    def is_valid_backup(path: Path) -> bool:
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            conn.close()
            return {"users", "settings"} <= tables
        except sqlite3.Error:
            return False
