"""사용자별 위험 한도. 모든 조회에 사용자 조건을 건다."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from nh_mcp.domain import Market, RiskLimits, UserId
from nh_mcp.store.db import stamp


def _read(conn: sqlite3.Connection, user_id: UserId, market: Market) -> RiskLimits | None:
    row = conn.execute(
        "SELECT daily_loss, max_position, max_order_qty FROM risk_limits "
        "WHERE user_id = ? AND market = ?",
        (user_id, market),
    ).fetchone()
    if row is None:
        return None
    return RiskLimits(
        daily_loss=Decimal(row[0]), max_position=int(row[1]), max_order_qty=int(row[2])
    )


def get(conn: sqlite3.Connection, user_id: UserId, market: Market) -> RiskLimits | None:
    return _read(conn, user_id, market)


_UPSERT = (
    "INSERT INTO risk_limits (user_id, market, daily_loss, max_position, max_order_qty, "
    "updated_at) VALUES (?, ?, ?, ?, ?, ?) "
    "ON CONFLICT (user_id, market) DO UPDATE SET daily_loss = excluded.daily_loss, "
    "max_position = excluded.max_position, max_order_qty = excluded.max_order_qty, "
    "updated_at = excluded.updated_at"
)


def _values(
    user_id: UserId, market: Market, new: RiskLimits, now: datetime
) -> tuple[int, str, str, int, int, str]:
    return (user_id, market, str(new.daily_loss), new.max_position, new.max_order_qty, stamp(now))


def put(
    conn: sqlite3.Connection, user_id: UserId, market: Market, new: RiskLimits, now: datetime
) -> None:
    with conn:
        conn.execute(_UPSERT, _values(user_id, market, new, now))


@dataclass(frozen=True)
class Change:
    """한도 변경 한 건. `expected`는 바꾸려고 본 지금 값(처음 정하기면 None)."""

    expected: RiskLimits | None
    new: RiskLimits


def put_if_unchanged(
    conn: sqlite3.Connection, user_id: UserId, market: Market, change: Change, now: datetime
) -> bool:
    """지금 값이 `change.expected`일 때만 `change.new`로 바꾼다. 읽기와 쓰기가 한 트랜잭션이다."""
    with conn:
        # 쓰기 잠금을 먼저 잡아, 읽은 뒤 다른 연결이 값을 바꾸지 못하게 한다.
        conn.execute("BEGIN IMMEDIATE")
        if _read(conn, user_id, market) != change.expected:
            return False
        conn.execute(_UPSERT, _values(user_id, market, change.new, now))
        return True
