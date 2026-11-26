"""주문 기록. 전송 전에 SUBMITTED 행을 먼저 쓰고, 응답을 받으면 갱신한다.

`run_id`는 자동 주문을 낸 전략 실행이다. 수동 주문은 None(NULL)이다.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from nh_mcp.domain import Market, OrderRequest, OrderType, Side, UserId
from nh_mcp.store.db import parse_stamp, stamp

type OrderStatus = Literal["SUBMITTED", "ACCEPTED", "REJECTED", "UNKNOWN", "FILLED", "CANCELLED"]


@dataclass(frozen=True)
class StoredOrder:
    id: int
    user_id: UserId
    market: Market
    symbol: str
    side: Side
    type: OrderType
    quantity: int
    price: Decimal | None
    status: OrderStatus
    nh_order_no: str | None
    msg_cd: str | None
    msg_cntn: str | None
    submitted_at: datetime
    updated_at: datetime
    run_id: str | None


_COLUMNS = (
    "id, user_id, market, symbol, side, order_type, quantity, price, status, "
    "nh_order_no, msg_cd, msg_cntn, submitted_at, updated_at, run_id"
)

_STATUSES: tuple[OrderStatus, ...] = (
    "SUBMITTED",
    "ACCEPTED",
    "REJECTED",
    "UNKNOWN",
    "FILLED",
    "CANCELLED",
)


def _status(text: str) -> OrderStatus:
    for status in _STATUSES:
        if status == text:
            return status
    raise ValueError(f"알 수 없는 주문 상태: {text}")


def _market(text: str) -> Market:
    return "domestic" if text == "domestic" else "overseas"


def _row(row: tuple[object, ...]) -> StoredOrder:
    return StoredOrder(
        id=int(str(row[0])),
        user_id=UserId(int(str(row[1]))),
        market=_market(str(row[2])),
        symbol=str(row[3]),
        side="buy" if row[4] == "buy" else "sell",
        type="limit" if row[5] == "limit" else "market",
        quantity=int(str(row[6])),
        price=None if row[7] is None else Decimal(str(row[7])),
        status=_status(str(row[8])),
        nh_order_no=None if row[9] is None else str(row[9]),
        msg_cd=None if row[10] is None else str(row[10]),
        msg_cntn=None if row[11] is None else str(row[11]),
        submitted_at=parse_stamp(str(row[12])),
        updated_at=parse_stamp(str(row[13])),
        run_id=None if row[14] is None else str(row[14]),
    )


def insert_submitted(
    conn: sqlite3.Connection,
    user_id: UserId,
    order: OrderRequest,
    now: datetime,
    run_id: str | None = None,
) -> int:
    with conn:
        cursor = conn.execute(
            "INSERT INTO orders (user_id, market, symbol, side, order_type, quantity, price, "
            "status, submitted_at, updated_at, run_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'SUBMITTED', ?, ?, ?)",
            (
                user_id,
                order.market,
                order.symbol,
                order.side,
                order.type,
                order.quantity,
                None if order.price is None else str(order.price),
                stamp(now),
                stamp(now),
                run_id,
            ),
        )
    if cursor.lastrowid is None:
        raise RuntimeError("주문 행을 쓰지 못했습니다.")
    return cursor.lastrowid


@dataclass(frozen=True)
class Outcome:
    """주문의 새 상태. 값을 주지 않은 칸(None)은 행에서 그대로 둔다."""

    status: OrderStatus
    nh_order_no: str | None = None
    msg_cd: str | None = None
    msg_cntn: str | None = None


def update(
    conn: sqlite3.Connection, user_id: UserId, order_id: int, outcome: Outcome, now: datetime
) -> bool:
    """상태를 바꾼다. 그 사용자의 그 행이 바뀌었으면 True(없는 id·남의 행이면 False)."""
    with conn:
        cursor = conn.execute(
            "UPDATE orders SET status = ?, nh_order_no = COALESCE(?, nh_order_no), "
            "msg_cd = COALESCE(?, msg_cd), msg_cntn = COALESCE(?, msg_cntn), updated_at = ? "
            "WHERE id = ? AND user_id = ?",
            (
                outcome.status,
                outcome.nh_order_no,
                outcome.msg_cd,
                outcome.msg_cntn,
                stamp(now),
                order_id,
                user_id,
            ),
        )
    return cursor.rowcount == 1


def get(conn: sqlite3.Connection, user_id: UserId, order_id: int) -> StoredOrder | None:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM orders WHERE id = ? AND user_id = ?", (order_id, user_id)
    ).fetchone()
    return None if row is None else _row(row)


def list_for(conn: sqlite3.Connection, user_id: UserId) -> list[StoredOrder]:
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM orders WHERE user_id = ? ORDER BY id", (user_id,)
    ).fetchall()
    return [_row(row) for row in rows]


def count_for_run(conn: sqlite3.Connection, user_id: UserId, run_id: str, since: datetime) -> int:
    """그 전략 실행이 since 뒤에 낸 주문 수(보낸 것 모두, 거부·모름 포함)."""
    row: tuple[int] = conn.execute(
        "SELECT COUNT(*) FROM orders WHERE user_id = ? AND run_id = ? AND submitted_at >= ?",
        (user_id, run_id, stamp(since)),
    ).fetchone()
    return row[0]


def unknown(
    conn: sqlite3.Connection, user_id: UserId, symbol: str | None = None
) -> list[StoredOrder]:
    """상태를 모르는 주문. `symbol`을 주면 그 종목만."""
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM orders WHERE user_id = ? AND status = 'UNKNOWN' "
        "AND (? IS NULL OR symbol = ?) ORDER BY id",
        (user_id, symbol, symbol),
    ).fetchall()
    return [_row(row) for row in rows]


def unsettled(
    conn: sqlite3.Connection, user_id: UserId, symbol: str | None = None
) -> list[StoredOrder]:
    """나갔는지 모르는 주문: UNKNOWN과 SUBMITTED. `symbol`을 주면 그 종목만.

    전송은 사용자별 잠금 안에서만 하므로, 잠금을 쥔 쪽이 보는 SUBMITTED 행은 전송 중에
    멈춘 것이다(프로세스가 죽었거나 응답 뒤 갱신에 실패). UNKNOWN과 똑같이 다룬다.
    """
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM orders WHERE user_id = ? "
        "AND status IN ('UNKNOWN', 'SUBMITTED') AND (? IS NULL OR symbol = ?) ORDER BY id",
        (user_id, symbol, symbol),
    ).fetchall()
    return [_row(row) for row in rows]


def bound_order_numbers(conn: sqlite3.Connection, user_id: UserId) -> set[str]:
    """이 서버의 주문 행에 이미 묶인 NH 주문번호."""
    rows = conn.execute(
        "SELECT nh_order_no FROM orders WHERE user_id = ? AND nh_order_no IS NOT NULL",
        (user_id,),
    ).fetchall()
    return {str(row[0]) for row in rows}
