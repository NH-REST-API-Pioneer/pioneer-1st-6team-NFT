"""사용자별 위험 한도. 모든 조회에 사용자 조건을 건다."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from nh_mcp.domain import Market, OptionLimits, RiskLimits, UserId
from nh_mcp.store.db import stamp


def _read(conn: sqlite3.Connection, user_id: UserId, market: Market) -> RiskLimits | None:
    row = conn.execute(
        "SELECT daily_loss, max_position, max_order_qty, option_premium_daily, "
        "option_max_position, option_max_short FROM risk_limits WHERE user_id = ? AND market = ?",
        (user_id, market),
    ).fetchone()
    if row is None:
        return None
    # 옵션 칸 셋은 `put`이 함께 쓰고 함께 비운다.
    options = None if row[3] is None else OptionLimits(Decimal(row[3]), int(row[4]), int(row[5]))
    return RiskLimits(
        daily_loss=Decimal(row[0]),
        max_position=int(row[1]),
        max_order_qty=int(row[2]),
        options=options,
    )


def get(conn: sqlite3.Connection, user_id: UserId, market: Market) -> RiskLimits | None:
    return _read(conn, user_id, market)


_UPSERT = (
    "INSERT INTO risk_limits (user_id, market, daily_loss, max_position, max_order_qty, "
    "updated_at, option_premium_daily, option_max_position, option_max_short) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT (user_id, market) DO UPDATE SET daily_loss = excluded.daily_loss, "
    "max_position = excluded.max_position, max_order_qty = excluded.max_order_qty, "
    "updated_at = excluded.updated_at, option_premium_daily = excluded.option_premium_daily, "
    "option_max_position = excluded.option_max_position, "
    "option_max_short = excluded.option_max_short"
)


def _values(
    user_id: UserId, market: Market, new: RiskLimits, now: datetime
) -> tuple[int, str, str, int, int, str, str | None, int | None, int | None]:
    options = new.options
    return (
        user_id,
        market,
        str(new.daily_loss),
        new.max_position,
        new.max_order_qty,
        stamp(now),
        None if options is None else str(options.premium_daily),
        None if options is None else options.max_position,
        None if options is None else options.max_short,
    )


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
