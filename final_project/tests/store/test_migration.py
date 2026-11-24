"""배포된 DB(옛 confirm_tokens CHECK)를 새 코드로 다시 열 때의 이주."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nh_mcp.store import db

NOW = db.stamp(datetime(2026, 12, 1, 1, 0, tzinfo=UTC))
LATER = db.stamp(datetime(2026, 12, 1, 1, 2, tzinfo=UTC))
OLD_CHECK = "CHECK (purpose IN ('order', 'limits'))"
NEW_CHECK = "CHECK (purpose IN ('order', 'limits', 'strategy'))"


def make_old_db(path: Path) -> None:
    """지금 SCHEMA에서 confirm_tokens의 CHECK만 옛것으로 되돌려 만든다."""
    assert NEW_CHECK in db.SCHEMA
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA.replace(NEW_CHECK, OLD_CHECK))
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
            "INSERT INTO confirm_tokens VALUES ('h-new', 1, 'strategy', '{}', ?, ?, NULL)",
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
        "INSERT INTO confirm_tokens VALUES ('h', 1, 'strategy', '{}', ?, ?, NULL)", (NOW, LATER)
    )

    assert counts(conn) == (1, 0, 1)
