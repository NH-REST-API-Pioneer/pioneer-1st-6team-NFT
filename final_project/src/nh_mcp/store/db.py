"""SQLite 연결과 스키마."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

# 확인 토큰 표. 용도에 'strategy'가 늘어 옛 DB는 `_migrate`가 이 표만 다시 만든다.
CONFIRM_TOKENS = """
CREATE TABLE IF NOT EXISTS confirm_tokens (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    purpose TEXT NOT NULL CHECK (purpose IN ('order', 'limits', 'strategy')),
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT
);
"""

_SCHEMA_HEAD = """
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
CREATE TABLE IF NOT EXISTS risk_limits (
    user_id INTEGER NOT NULL REFERENCES users(id),
    market TEXT NOT NULL CHECK (market IN ('domestic', 'overseas')),
    daily_loss TEXT NOT NULL,
    max_position INTEGER NOT NULL,
    max_order_qty INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, market)
);
"""

_SCHEMA_TAIL = """
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    market TEXT NOT NULL CHECK (market IN ('domestic', 'overseas')),
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    order_type TEXT NOT NULL CHECK (order_type IN ('limit', 'market')),
    quantity INTEGER NOT NULL,
    price TEXT,
    status TEXT NOT NULL,
    nh_order_no TEXT,
    msg_cd TEXT,
    msg_cntn TEXT,
    submitted_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS orders_by_user_status ON orders (user_id, status);
CREATE TABLE IF NOT EXISTS backtest_runs (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    market TEXT NOT NULL CHECK (market IN ('domestic', 'overseas')),
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL CHECK (interval IN ('minute', 'day')),
    code_hash TEXT NOT NULL,
    code TEXT NOT NULL,
    params TEXT NOT NULL,
    settings TEXT NOT NULL,
    requested_start TEXT NOT NULL,
    requested_end TEXT NOT NULL,
    first_bar_at TEXT,
    last_bar_at TEXT,
    bar_count INTEGER NOT NULL,
    status TEXT NOT NULL,
    result TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS backtest_runs_lookup
    ON backtest_runs (user_id, market, symbol, interval, code_hash, status);
"""

SCHEMA = _SCHEMA_HEAD + CONFIRM_TOKENS + _SCHEMA_TAIL


def connect(path: Path | str) -> sqlite3.Connection:
    # SQLite가 직렬화 모드(threadsafety 3)라 연결 하나를 여러 스레드가 같이 써도 된다.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """옛 DB의 confirm_tokens는 용도 CHECK에 'strategy'가 없다. 그 표만 지우고 새로 만든다.

    토큰은 2분(링크는 10분)짜리라 잃는 것은 진행 중인 토큰뿐이다. 다른 표는 건드리지 않는다.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'confirm_tokens'"
    ).fetchone()
    if row is None or "'strategy'" in str(row[0]):
        return
    # executescript는 먼저 커밋하므로 쓰지 않고, 문장을 하나씩 한 트랜잭션에 묶는다.
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DROP TABLE confirm_tokens")
        conn.execute(CONFIRM_TOKENS.replace("IF NOT EXISTS ", "").strip().rstrip(";"))


def stamp(moment: datetime) -> str:
    """시각을 저장용 문자열로. 항상 UTC·같은 자릿수라 문자열 비교가 시각 순서와 같다."""
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def parse_stamp(text: str) -> datetime:
    return datetime.fromisoformat(text)
