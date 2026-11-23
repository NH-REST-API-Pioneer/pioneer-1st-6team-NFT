"""SQLite 연결과 스키마."""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    google_sub TEXT NOT NULL UNIQUE,
    email TEXT NOT NULL,
    suspended INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS nh_keys (
    user_id INTEGER NOT NULL REFERENCES users(id),
    market TEXT NOT NULL CHECK (market IN ('domestic', 'overseas')),
    sealed_app_key BLOB NOT NULL,
    sealed_app_secret BLOB NOT NULL,
    app_key_last4 TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    last_used_at TEXT NOT NULL,
    PRIMARY KEY (user_id, market)
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    at TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    # SQLite가 직렬화 모드(threadsafety 3)라 연결 하나를 여러 스레드가 같이 써도 된다.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn
