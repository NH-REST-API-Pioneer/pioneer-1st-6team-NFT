"""SQLite 연결과 스키마."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

# 확인 토큰 표. 용도에 'strategy'가 늘어 옛 DB(M2)는 `_migrate`가 이 표를 지우고 다시 만든다.
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
"""

# 백테스트 기록 표. 종목·간격 칸(M3)이 흐름(`streams`, 정렬 JSON)과 모델 해시로 바뀌어 옛 DB는
# `_migrate`가 이 표를 지우고 다시 만든다. model_hash는 모델 파일을 쓰지 않은 실행이면 NULL이다.
BACKTEST_RUNS = """
CREATE TABLE IF NOT EXISTS backtest_runs (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    market TEXT NOT NULL CHECK (market IN ('domestic', 'overseas')),
    streams TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    model_hash TEXT,
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
    ON backtest_runs (user_id, market, streams, code_hash, status);
"""

# 사용자가 올린 모델 파일(M4a). 내용은 DB가 아니라 모델 폴더의 <사용자>/<sha256> 파일에 있다.
MODELS = """
CREATE TABLE IF NOT EXISTS models (
    user_id INTEGER NOT NULL REFERENCES users(id),
    sha256 TEXT NOT NULL,
    name TEXT NOT NULL,
    size INTEGER NOT NULL,
    uploaded_at TEXT NOT NULL,
    PRIMARY KEY (user_id, sha256)
);
"""

# 틱·호가 저장 설정(M4a). 저장한 틱은 ticks.db에 따로 있다. 서버를 다시 띄우면 이 표대로 다시 연다.
TICK_RECORDINGS = """
CREATE TABLE IF NOT EXISTS tick_recordings (
    user_id INTEGER NOT NULL REFERENCES users(id),
    symbol TEXT NOT NULL,
    stream TEXT NOT NULL CHECK (stream IN ('tick', 'book')),
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, symbol, stream)
);
"""

SCHEMA = _SCHEMA_HEAD + CONFIRM_TOKENS + _SCHEMA_TAIL + BACKTEST_RUNS + MODELS + TICK_RECORDINGS


def connect(path: Path | str) -> sqlite3.Connection:
    # SQLite가 직렬화 모드(threadsafety 3)라 연결 하나를 여러 스레드가 같이 써도 된다.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    # 옛 표를 먼저 지운다. 그래야 SCHEMA가 새 모양으로 만들고, 새 색인이 옛 표에 걸리지 않는다.
    _migrate(conn)
    conn.executescript(SCHEMA)
    return conn


def _table_sql(conn: sqlite3.Connection, name: str) -> str | None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return None if row is None else str(row[0])


# 모양이 바뀐 표 → 새 모양의 CREATE 문에만 있는 글자. 없으면 옛 표다.
# - confirm_tokens(M2): 용도 CHECK에 'strategy'가 없다. 토큰은 2분(링크는 10분)짜리라 잃는 것은
#   진행 중인 토큰뿐이다.
# - backtest_runs(M3): 종목·간격 칸이고 `streams`가 없다. M3 기록은 옛 계약(봉 하나에 신호 하나)의
#   코드라 새 계약에서 preview_strategy의 근거가 될 수 없고, 배포 서버에는 M3가 올라간 적이 없어
#   남길 기록이 없다(M4a 설계 8절).
_NEW_SHAPE = {"confirm_tokens": "'strategy'", "backtest_runs": "streams"}


def _migrate(conn: sqlite3.Connection) -> None:
    """옛 DB에서 모양이 바뀐 표만 지운다(SCHEMA가 새로 만든다). 다른 표는 건드리지 않는다."""
    stale = [
        name
        for name, marker in _NEW_SHAPE.items()
        if (sql := _table_sql(conn, name)) is not None and marker not in sql
    ]
    if not stale:
        return
    # executescript는 먼저 커밋하므로 쓰지 않고, 문장을 하나씩 한 트랜잭션에 묶는다.
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        for name in stale:
            conn.execute(f"DROP TABLE {name}")


def stamp(moment: datetime) -> str:
    """시각을 저장용 문자열로. 항상 UTC·같은 자릿수라 문자열 비교가 시각 순서와 같다."""
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def parse_stamp(text: str) -> datetime:
    return datetime.fromisoformat(text)
