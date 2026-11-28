"""SQLite 연결과 스키마."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

# 확인 토큰 표. 용도에 'strategy'(M3)와 'combo'(M6)가 늘어 옛 DB는 `_migrate`가 이 표를 지우고 다시
# 만든다.
CONFIRM_TOKENS = """
CREATE TABLE IF NOT EXISTS confirm_tokens (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    purpose TEXT NOT NULL CHECK (purpose IN ('order', 'limits', 'strategy', 'combo')),
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
    option_premium_daily TEXT,
    option_max_position INTEGER,
    option_max_short INTEGER,
    option_max_combo_loss TEXT,
    PRIMARY KEY (user_id, market)
);
"""

# 주문 표. run_id(M4b, 자동 주문을 낸 전략 실행, 수동은 NULL)와 combo_id(M6, 다리 주문이 속한 묶음,
# 묶음 밖 주문은 NULL)는 맨 뒤 칸이다. 옛 DB는 `_migrate`가 행을 지우지 않고 칸만 더한다
# (`_NEW_COLUMNS`).
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
    updated_at TEXT NOT NULL,
    run_id TEXT,
    combo_id TEXT
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

# 자동 실행(M4b). 실행 하나가 한 줄이고 켠 뒤에 바뀌는 칸은 state·reason·stopped_at·session_id·
# last_event_at뿐이다. 상태는 runner/machine.py의 전이 표로만 바꾼다. session_id는 서버가 다시 뜰 때
# 남은 전략 컨테이너를 닫으려고 둔다. strategy_events는 실행마다 최근 것만 남기는 기록이다.
STRATEGY_RUNS = """
CREATE TABLE IF NOT EXISTS strategy_runs (
    run_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    market TEXT NOT NULL CHECK (market IN ('domestic', 'overseas')),
    streams TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    params TEXT NOT NULL,
    model_hash TEXT,
    scope TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('starting', 'running', 'paused', 'stopped')),
    reason TEXT,
    started_at TEXT NOT NULL,
    stopped_at TEXT,
    session_id TEXT,
    last_event_at TEXT
);
CREATE INDEX IF NOT EXISTS strategy_runs_by_user_state ON strategy_runs (user_id, state);
CREATE TABLE IF NOT EXISTS strategy_events (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES strategy_runs(run_id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    at TEXT NOT NULL,
    kind TEXT NOT NULL
        CHECK (kind IN ('output', 'order', 'fill', 'stop', 'warning', 'suggestion')),
    symbol TEXT,
    detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS strategy_events_by_run ON strategy_events (user_id, run_id, id);
"""

# 사용자가 등록 화면에서 켠 기능 묶음('auto_run', 'options', 'option_sell'). 행이 있으면 켜진 것이고
# 기본은 꺼짐이다.
USER_FEATURES = """
CREATE TABLE IF NOT EXISTS user_features (
    user_id INTEGER NOT NULL REFERENCES users(id),
    feature TEXT NOT NULL,
    enabled_at TEXT NOT NULL,
    PRIMARY KEY (user_id, feature)
);
"""

# 옵션 보유 기록(M5, store/option_holdings.py). 종목·방향마다 한 줄이고 닫힌 줄도 남는다.
# close_reason이 NULL이면 보유 중, 'closed'는 만기 전에 잔고에서 사라짐, 'exercised_or_expired'는
# 만기일이 지나 사라짐(행사 또는 소멸). 옛 DB에는 이 표가 없어 SCHEMA가 새로 만든다.
OPTION_HOLDINGS = """
CREATE TABLE IF NOT EXISTS option_holdings (
    user_id INTEGER NOT NULL REFERENCES users(id),
    market TEXT NOT NULL CHECK (market IN ('domestic', 'overseas')),
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity INTEGER NOT NULL,
    expiry TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    closed_at TEXT,
    close_reason TEXT CHECK (close_reason IN ('closed', 'exercised_or_expired')),
    CHECK ((closed_at IS NULL) = (close_reason IS NULL)),
    PRIMARY KEY (user_id, market, symbol, side)
);
"""

# 옵션 묶음(M6, store/combos.py). 묶음 하나가 한 줄이다. request·plan은 서비스가 쓴 JSON이고 상태는
# 다리 주문(orders.combo_id)에서 계산한다. lease_until은 실행이 묶음을 쥐고 있는 동안만 채운다.
# 옛 DB에는 이 표가 없어 SCHEMA가 새로 만든다.
COMBOS = """
CREATE TABLE IF NOT EXISTS combos (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    market TEXT NOT NULL CHECK (market IN ('domestic', 'overseas')),
    request TEXT NOT NULL,
    plan TEXT NOT NULL,
    created_at TEXT NOT NULL,
    lease_until TEXT
);
CREATE INDEX IF NOT EXISTS combos_by_user ON combos (user_id);
"""

SCHEMA = (
    _SCHEMA_HEAD
    + CONFIRM_TOKENS
    + _SCHEMA_TAIL
    + BACKTEST_RUNS
    + MODELS
    + TICK_RECORDINGS
    + STRATEGY_RUNS
    + USER_FEATURES
    + OPTION_HOLDINGS
    + COMBOS
)


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
# - confirm_tokens(M2·M5): 용도 CHECK에 'combo'가 없다(M2는 'strategy'도 없다). 토큰은 2분(링크는
#   10분)짜리라 잃는 것은 진행 중인 토큰뿐이다.
# - backtest_runs(M3): 종목·간격 칸이고 `streams`가 없다. M3 기록은 옛 계약(봉 하나에 신호 하나)의
#   코드라 새 계약에서 preview_strategy의 근거가 될 수 없고, 배포 서버에는 M3가 올라간 적이 없어
#   남길 기록이 없다(M4a 설계 8절).
_NEW_SHAPE = {"confirm_tokens": "'combo'", "backtest_runs": "streams"}

# 늘어난 칸 (표, 칸 이름, 칸 타입). 옛 표면 ALTER TABLE로 이 순서대로 맨 뒤에 더한다(행은 그대로).
# - orders(M4b): run_id가 없다. 주문 행은 지우지 않는다. 오늘의 UNKNOWN·SUBMITTED 행이 그 종목의
#   늘리는 주문을 막고 있을 수 있다(불변식 5). 옛 행은 모두 수동 주문이라 NULL이 맞다.
# - orders(M6): combo_id가 없다. 옛 행은 모두 묶음 밖 주문이라 NULL이 맞다. run_id 뒤에 더한다.
# - risk_limits(M5): 옵션 한도 칸 셋이 없다. 셋 다 NULL이면 옵션 한도가 없는 것이고, 선물 한도는
#   그대로 둔다.
# - risk_limits(M6): 묶음 최대 손실 칸이 없다. NULL이면 묶음 한도가 없는 것이고, 다른 한도는
#   그대로 둔다.
_NEW_COLUMNS = (
    ("orders", "run_id", "TEXT"),
    ("orders", "combo_id", "TEXT"),
    ("risk_limits", "option_premium_daily", "TEXT"),
    ("risk_limits", "option_max_position", "INTEGER"),
    ("risk_limits", "option_max_short", "INTEGER"),
    ("risk_limits", "option_max_combo_loss", "TEXT"),
)


def _columns(conn: sqlite3.Connection, name: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({name})").fetchall()}


def _migrate(conn: sqlite3.Connection) -> None:
    """옛 DB에서 모양이 바뀐 표는 지우고(SCHEMA가 새로 만든다), 칸이 늘어난 표는 칸을 더한다.

    다른 표는 건드리지 않는다.
    """
    stale = [
        name
        for name, marker in _NEW_SHAPE.items()
        if (sql := _table_sql(conn, name)) is not None and marker not in sql
    ]
    missing = [
        (name, f"{column} {kind}")
        for name, column, kind in _NEW_COLUMNS
        if _table_sql(conn, name) is not None and column not in _columns(conn, name)
    ]
    if not stale and not missing:
        return
    # executescript는 먼저 커밋하므로 쓰지 않고, 문장을 하나씩 한 트랜잭션에 묶는다.
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        for name in stale:
            conn.execute(f"DROP TABLE {name}")
        for name, definition in missing:
            conn.execute(f"ALTER TABLE {name} ADD COLUMN {definition}")


def stamp(moment: datetime) -> str:
    """시각을 저장용 문자열로. 항상 UTC·같은 자릿수라 문자열 비교가 시각 순서와 같다."""
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def parse_stamp(text: str) -> datetime:
    return datetime.fromisoformat(text)
