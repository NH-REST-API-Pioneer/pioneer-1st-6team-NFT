"""배포된 DB(옛 confirm_tokens CHECK)를 새 코드로 다시 열 때의 이주."""

import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from nh_mcp.domain import (
    AccountState,
    OptionLimits,
    OrderRequest,
    Position,
    RiskLimits,
    UserId,
)
from nh_mcp.store import combos, db, limits, option_holdings, orders
from nh_mcp.store.orders import Origin

NOW = db.stamp(datetime(2026, 12, 1, 1, 0, tzinfo=UTC))
LATER = db.stamp(datetime(2026, 12, 1, 1, 2, tzinfo=UTC))
OLD_CHECK = "CHECK (purpose IN ('order', 'limits'))"
M5_CHECK = "CHECK (purpose IN ('order', 'limits', 'strategy'))"
NEW_CHECK = "CHECK (purpose IN ('order', 'limits', 'strategy', 'combo'))"


def make_old_db(path: Path, old_check: str = OLD_CHECK) -> None:
    """지금 SCHEMA에서 confirm_tokens의 CHECK만 옛것으로 되돌려 만든다."""
    assert NEW_CHECK in db.SCHEMA
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA.replace(NEW_CHECK, old_check))
    conn.execute(
        "INSERT INTO users (id, google_sub, email, created_at) VALUES (1, 's', 'a@x.com', ?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO orders (user_id, market, symbol, side, order_type, quantity, status, "
        "submitted_at, updated_at) VALUES (1, 'domestic', 'A016C000', 'buy', 'market', 1, "
        "'accepted', ?, ?)",
        (NOW, NOW),
    )
    conn.execute(
        "INSERT INTO confirm_tokens VALUES ('h-old', 1, 'order', '{}', ?, ?, NULL)", (NOW, LATER)
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO confirm_tokens VALUES ('h-new', 1, 'combo', '{}', ?, ?, NULL)",
            (NOW, LATER),
        )
    conn.close()


def counts(conn: sqlite3.Connection) -> tuple[int, int, int]:
    def count(table: str) -> int:
        row: tuple[int] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return row[0]

    return count("users"), count("orders"), count("confirm_tokens")


def test_old_database_accepts_strategy_tokens_and_keeps_other_tables(tmp_path: Path) -> None:
    path = tmp_path / "nh.db"
    make_old_db(path)

    conn = db.connect(path)
    conn.execute(
        "INSERT INTO confirm_tokens VALUES ('h-new', 1, 'strategy', '{}', ?, ?, NULL)",
        (NOW, LATER),
    )

    users, orders, _ = counts(conn)
    assert (users, orders) == (1, 1)
    assert conn.execute("SELECT symbol FROM orders").fetchone() == ("A016C000",)
    assert conn.execute("SELECT email FROM users").fetchone() == ("a@x.com",)


def test_m5_database_accepts_combo_tokens_and_keeps_other_tables(tmp_path: Path) -> None:
    """M5 CHECK에는 'combo'가 없다. 표를 다시 만들어 진행 중인 토큰만 잃는다(2분짜리)."""
    path = tmp_path / "nh.db"
    make_old_db(path, M5_CHECK)

    conn = db.connect(path)
    conn.execute(
        "INSERT INTO confirm_tokens VALUES ('h-new', 1, 'combo', '{}', ?, ?, NULL)", (NOW, LATER)
    )

    assert counts(conn) == (1, 1, 1)
    assert conn.execute("SELECT token_hash FROM confirm_tokens").fetchall() == [("h-new",)]
    assert conn.execute("SELECT symbol FROM orders").fetchone() == ("A016C000",)


def test_opening_twice_deletes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "nh.db"
    make_old_db(path)
    first = db.connect(path)
    first.execute(
        "INSERT INTO confirm_tokens VALUES ('h-new', 1, 'strategy', '{}', ?, ?, NULL)",
        (NOW, LATER),
    )
    first.commit()
    first.close()

    second = db.connect(path)
    third = db.connect(path)

    assert counts(second) == (1, 1, 1)
    assert counts(third) == (1, 1, 1)


def test_fresh_database_needs_no_migration() -> None:
    conn = db.connect(":memory:")
    conn.execute(
        "INSERT INTO users (id, google_sub, email, created_at) VALUES (1, 's', 'a', ?)", (NOW,)
    )
    conn.execute(
        "INSERT INTO confirm_tokens VALUES ('h', 1, 'combo', '{}', ?, ?, NULL)", (NOW, LATER)
    )

    assert counts(conn) == (1, 0, 1)


# M3에 배포하려던 backtest_runs. 종목·간격 칸이고 흐름·모델 칸이 없다.
M3_BACKTEST_RUNS = """
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


def make_m3_db(path: Path) -> None:
    """지금 SCHEMA에서 backtest_runs만 M3 모양으로 되돌리고, 그 표에 성공 기록 하나를 넣는다."""
    assert db.BACKTEST_RUNS in db.SCHEMA
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA.replace(db.BACKTEST_RUNS, M3_BACKTEST_RUNS))
    conn.execute(
        "INSERT INTO users (id, google_sub, email, created_at) VALUES (1, 's', 'a@x.com', ?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO backtest_runs (user_id, market, symbol, interval, code_hash, code, params, "
        "settings, requested_start, requested_end, bar_count, status, created_at) VALUES "
        "(1, 'domestic', 'A016C000', 'day', 'h', 'c', '{}', '{}', '2026-11-02', '2026-11-30', "
        "5, 'ok', ?)",
        (NOW,),
    )
    conn.commit()
    conn.close()


def columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def test_m3_backtest_runs_is_rebuilt_with_streams_and_model_hash(tmp_path: Path) -> None:
    path = tmp_path / "nh.db"
    make_m3_db(path)

    conn = db.connect(path)

    names = columns(conn, "backtest_runs")
    assert "streams" in names and "model_hash" in names
    assert "symbol" not in names and "interval" not in names
    # 옛 계약의 기록은 지운다. 새 계약에서 preview_strategy의 근거가 될 수 없다.
    assert conn.execute("SELECT COUNT(*) FROM backtest_runs").fetchone() == (0,)
    # 다른 표는 그대로다.
    assert conn.execute("SELECT email FROM users").fetchone() == ("a@x.com",)
    index = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'backtest_runs_lookup'"
    ).fetchone()
    assert "streams" in str(index[0])


def test_rebuilt_backtest_runs_survives_the_next_open(tmp_path: Path) -> None:
    path = tmp_path / "nh.db"
    make_m3_db(path)
    first = db.connect(path)
    first.execute(
        "INSERT INTO backtest_runs (user_id, market, streams, code_hash, model_hash, code, "
        "params, settings, requested_start, requested_end, bar_count, status, created_at) VALUES "
        "(1, 'domestic', '{\"A016C000\":[\"day\"]}', 'h', NULL, 'c', '{}', '{}', '2026-11-02', "
        "'2026-11-30', 5, 'ok', ?)",
        (NOW,),
    )
    first.commit()
    first.close()

    second = db.connect(path)

    assert second.execute("SELECT streams FROM backtest_runs").fetchall() == [
        ('{"A016C000":["day"]}',)
    ]


# orders의 맨 뒤 두 칸. run_id(M4b) 전에는 둘 다 없고, combo_id(M6) 전에는 combo_id만 없다.
ORDERS_TAIL = (
    ",\n    run_id TEXT,\n    combo_id TEXT\n);\nCREATE INDEX IF NOT EXISTS orders_by_user_status"
)
M4A_TAIL = "\n);\nCREATE INDEX IF NOT EXISTS orders_by_user_status"
M5_TAIL = ",\n    run_id TEXT\n);\nCREATE INDEX IF NOT EXISTS orders_by_user_status"


def make_m4a_db(path: Path) -> None:
    """orders에 run_id가 없는 DB. 오늘의 UNKNOWN 주문 하나를 넣는다."""
    assert ORDERS_TAIL in db.SCHEMA
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA.replace(ORDERS_TAIL, M4A_TAIL))
    conn.execute(
        "INSERT INTO users (id, google_sub, email, created_at) VALUES (1, 's', 'a@x.com', ?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO orders (user_id, market, symbol, side, order_type, quantity, status, "
        "submitted_at, updated_at) VALUES (1, 'domestic', 'A016C000', 'buy', 'market', 1, "
        "'UNKNOWN', ?, ?)",
        (NOW, NOW),
    )
    conn.commit()
    assert "run_id" not in columns(conn, "orders")
    conn.close()


def test_m4a_orders_gain_run_id_and_keep_their_rows(tmp_path: Path) -> None:
    path = tmp_path / "nh.db"
    make_m4a_db(path)

    conn = db.connect(path)

    assert columns(conn, "orders")[-2:] == ["run_id", "combo_id"]
    # 행을 지우지 않는다. UNKNOWN 행이 그 종목의 늘리는 주문을 계속 막아야 한다.
    assert conn.execute("SELECT symbol, status, run_id FROM orders").fetchall() == [
        ("A016C000", "UNKNOWN", None)
    ]
    conn.execute(
        "INSERT INTO orders (user_id, market, symbol, side, order_type, quantity, status, "
        "submitted_at, updated_at, run_id) VALUES (1, 'domestic', 'A016C000', 'sell', 'market', "
        "1, 'SUBMITTED', ?, ?, 'run-1')",
        (NOW, NOW),
    )
    assert conn.execute("SELECT run_id FROM orders ORDER BY id").fetchall() == [(None,), ("run-1",)]


def test_orders_with_run_id_survive_the_next_open(tmp_path: Path) -> None:
    path = tmp_path / "nh.db"
    make_m4a_db(path)
    first = db.connect(path)
    first.execute("UPDATE orders SET run_id = 'run-1'")
    first.commit()
    first.close()

    second = db.connect(path)

    assert columns(second, "orders").count("run_id") == 1
    assert second.execute("SELECT run_id FROM orders").fetchall() == [("run-1",)]


def test_a_fresh_orders_table_has_the_same_columns_as_a_migrated_one(tmp_path: Path) -> None:
    path = tmp_path / "nh.db"
    make_m4a_db(path)

    migrated = columns(db.connect(path), "orders")

    assert migrated == columns(db.connect(":memory:"), "orders")


def make_m5_orders_db(path: Path) -> None:
    """orders에 combo_id가 없는 DB. 오늘의 UNKNOWN 수동 주문과 자동 주문 하나씩을 넣는다."""
    assert ORDERS_TAIL in db.SCHEMA
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA.replace(ORDERS_TAIL, M5_TAIL))
    conn.execute(
        "INSERT INTO users (id, google_sub, email, created_at) VALUES (1, 's', 'a@x.com', ?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO orders (user_id, market, symbol, side, order_type, quantity, status, "
        "submitted_at, updated_at, run_id) VALUES "
        "(1, 'domestic', 'B016C725', 'buy', 'market', 1, 'UNKNOWN', ?, ?, NULL), "
        "(1, 'domestic', 'A016C000', 'sell', 'market', 1, 'FILLED', ?, ?, 'run-1')",
        (NOW, NOW, NOW, NOW),
    )
    conn.commit()
    assert "combo_id" not in columns(conn, "orders")
    conn.close()


def test_m5_orders_gain_an_empty_combo_id_and_keep_their_rows(tmp_path: Path) -> None:
    path = tmp_path / "nh.db"
    make_m5_orders_db(path)

    conn = db.connect(path)

    user = UserId(1)
    kept = [(o.symbol, o.status, o.run_id, o.combo_id) for o in orders.list_for(conn, user)]
    assert kept == [
        ("B016C725", "UNKNOWN", None, None),
        ("A016C000", "FILLED", "run-1", None),
    ]
    assert [o.symbol for o in orders.unknown(conn, user)] == ["B016C725"]
    leg = OrderRequest("domestic", "B016C725", "buy", "limit", 1, Decimal("1.25"), "fak")
    added = orders.insert_submitted(
        conn, user, leg, datetime(2026, 12, 1, 1, 5, tzinfo=UTC), Origin(combo_id="c1")
    )
    assert [o.id for o in orders.list_for_combo(conn, user, "c1")] == [added]
    assert columns(conn, "orders") == columns(db.connect(":memory:"), "orders")


def test_m5_database_without_combos_gains_the_table(tmp_path: Path) -> None:
    """묶음(M6) 전의 DB. combos는 SCHEMA가 새로 만들고 다른 표는 그대로다."""
    assert db.COMBOS in db.SCHEMA
    path = tmp_path / "nh.db"
    old = sqlite3.connect(path)
    old.executescript(db.SCHEMA.replace(db.COMBOS, ""))
    old.execute(
        "INSERT INTO users (id, google_sub, email, created_at) VALUES (1, 's', 'a@x.com', ?)",
        (NOW,),
    )
    old.commit()
    assert columns(old, "combos") == []
    old.close()

    conn = db.connect(path)

    user = UserId(1)
    at = datetime(2026, 12, 1, 1, 0, tzinfo=UTC)
    combos.insert(conn, combos.Combo("c1", user, "domestic", "{}", "{}", at))
    assert combos.claim(conn, user, "c1", at, at + timedelta(seconds=90)) is True
    assert conn.execute("SELECT email FROM users").fetchone() == ("a@x.com",)
    assert columns(conn, "combos") == columns(db.connect(":memory:"), "combos")


# 묶음 한도(M6) 전의 risk_limits는 지금 SCHEMA에서 이 칸만 뺀 모양이다.
COMBO_COLUMN = "    option_max_combo_loss TEXT,\n"
# 옵션 한도(M5) 전의 risk_limits. 지금 SCHEMA에서 옵션 칸 넷(묶음 칸 포함)을 뺀 모양이다.
OPTION_COLUMNS = (
    "    option_premium_daily TEXT,\n"
    "    option_max_position INTEGER,\n"
    "    option_max_short INTEGER,\n" + COMBO_COLUMN
)


def make_m4b_db(path: Path) -> None:
    """risk_limits에 옵션 칸이 없는 DB. 사용자 둘의 선물 한도를 넣는다."""
    assert OPTION_COLUMNS in db.SCHEMA
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA.replace(OPTION_COLUMNS, "", 1))
    for user in (1, 2):
        conn.execute(
            "INSERT INTO users (id, google_sub, email, created_at) VALUES (?, ?, ?, ?)",
            (user, f"s{user}", f"u{user}@x.com", NOW),
        )
    conn.execute(
        "INSERT INTO risk_limits (user_id, market, daily_loss, max_position, max_order_qty, "
        "updated_at) VALUES (1, 'domestic', '1000000', 3, 1, ?), "
        "(2, 'domestic', '250000.50', 5, 2, ?)",
        (NOW, NOW),
    )
    conn.commit()
    assert "option_premium_daily" not in columns(conn, "risk_limits")
    conn.close()


def test_m4b_risk_limits_gain_option_columns_and_keep_futures_limits(tmp_path: Path) -> None:
    path = tmp_path / "nh.db"
    make_m4b_db(path)

    conn = db.connect(path)

    first, second = UserId(1), UserId(2)
    assert limits.get(conn, first, "domestic") == RiskLimits(Decimal("1000000"), 3, 1)
    assert limits.get(conn, second, "domestic") == RiskLimits(Decimal("250000.50"), 5, 2)
    with_options = RiskLimits(Decimal("1000000"), 3, 1, OptionLimits(Decimal("500000"), 10, 0))
    limits.put(conn, first, "domestic", with_options, datetime(2026, 12, 1, 1, 5, tzinfo=UTC))
    assert limits.get(conn, first, "domestic") == with_options
    assert limits.get(conn, second, "domestic") == RiskLimits(Decimal("250000.50"), 5, 2)


def test_a_fresh_risk_limits_table_has_the_same_columns_as_a_migrated_one(
    tmp_path: Path,
) -> None:
    path = tmp_path / "nh.db"
    make_m4b_db(path)

    migrated = columns(db.connect(path), "risk_limits")

    assert migrated == columns(db.connect(":memory:"), "risk_limits")
    assert migrated[-4:] == [
        "option_premium_daily",
        "option_max_position",
        "option_max_short",
        "option_max_combo_loss",
    ]


def make_m5_db(path: Path) -> None:
    """risk_limits에 묶음 칸이 없는 DB. 사용자 하나의 선물·옵션 한도를 넣는다."""
    assert COMBO_COLUMN in db.SCHEMA
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA.replace(COMBO_COLUMN, "", 1))
    conn.execute(
        "INSERT INTO users (id, google_sub, email, created_at) VALUES (1, 's', 'a@x.com', ?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO risk_limits (user_id, market, daily_loss, max_position, max_order_qty, "
        "updated_at, option_premium_daily, option_max_position, option_max_short) "
        "VALUES (1, 'domestic', '1000000', 3, 1, ?, '500000', 10, 2)",
        (NOW,),
    )
    conn.commit()
    assert "option_max_combo_loss" not in columns(conn, "risk_limits")
    conn.close()


def test_m5_risk_limits_gain_an_empty_combo_column_and_keep_option_limits(
    tmp_path: Path,
) -> None:
    path = tmp_path / "nh.db"
    make_m5_db(path)

    conn = db.connect(path)

    user = UserId(1)
    options = OptionLimits(Decimal("500000"), 10, 2)
    assert limits.get(conn, user, "domestic") == RiskLimits(Decimal("1000000"), 3, 1, options)
    with_combo = RiskLimits(
        Decimal("1000000"), 3, 1, OptionLimits(Decimal("500000"), 10, 2, Decimal("300000"))
    )
    limits.put(conn, user, "domestic", with_combo, datetime(2026, 12, 1, 1, 5, tzinfo=UTC))
    assert limits.get(conn, user, "domestic") == with_combo
    assert columns(conn, "risk_limits") == columns(db.connect(":memory:"), "risk_limits")


def test_m5_database_without_option_holdings_gains_the_table(tmp_path: Path) -> None:
    """옵션 보유 기록(M5) 전의 DB. 새 표는 SCHEMA가 만들고 다른 표는 그대로다."""
    assert db.OPTION_HOLDINGS in db.SCHEMA
    path = tmp_path / "nh.db"
    old = sqlite3.connect(path)
    old.executescript(db.SCHEMA.replace(db.OPTION_HOLDINGS, ""))
    old.execute(
        "INSERT INTO users (id, google_sub, email, created_at) VALUES (1, 's', 'a@x.com', ?)",
        (NOW,),
    )
    old.commit()
    assert columns(old, "option_holdings") == []
    old.close()

    conn = db.connect(path)

    user = UserId(1)
    held = Position("B016C725", "콜", "buy", 1, 1, None, None, None)
    at = datetime(2026, 12, 1, 1, 0, tzinfo=UTC)
    account = AccountState("domestic", "KRW", None, None, None, None, [held], [], at, True)
    option_holdings.reconcile(conn, user, account, {"B016C725": date(2026, 12, 10)})
    assert [h.symbol for h in option_holdings.load(conn, user, "domestic")] == ["B016C725"]
    assert conn.execute("SELECT email FROM users").fetchone() == ("a@x.com",)
    assert columns(conn, "option_holdings") == columns(db.connect(":memory:"), "option_holdings")
