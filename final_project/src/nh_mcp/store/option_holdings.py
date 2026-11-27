"""옵션 보유 기록(`option_holdings`, `docs/options.md` "만기 처리"). 모든 조회와 쓰기에 사용자
조건을 건다(안전 불변식 8).

옵션은 만기에 주문 없이 잔고에서 사라진다. 계좌를 받을 때마다 `reconcile`로 잔고에 맞춰, 사라진 것을
만기일이 지났으면 행사 또는 소멸(`exercised_or_expired`), 아니면 청산(`closed`)으로 닫는다.
"""

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Literal

from nh_mcp.domain import KST, AccountState, Market, Position, Side, UserId
from nh_mcp.store.db import parse_stamp, stamp

type CloseReason = Literal["closed", "exercised_or_expired"]


@dataclass(frozen=True)
class Holding:
    """종목·방향 하나의 기록. 보유 중이면 closed_at과 close_reason이 둘 다 None이다."""

    symbol: str
    side: Side
    quantity: int
    expiry: date
    first_seen: datetime
    last_seen: datetime
    closed_at: datetime | None = None
    close_reason: CloseReason | None = None


_COLUMNS = "symbol, side, quantity, expiry, first_seen, last_seen, closed_at, close_reason"
_PUT = (
    f"INSERT OR REPLACE INTO option_holdings (user_id, market, {_COLUMNS}) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def _row(row: tuple[object, ...]) -> Holding:
    reason: CloseReason | None = None
    if row[7] is not None:
        reason = "exercised_or_expired" if row[7] == "exercised_or_expired" else "closed"
    return Holding(
        symbol=str(row[0]),
        side="buy" if row[1] == "buy" else "sell",
        quantity=int(str(row[2])),
        expiry=date.fromisoformat(str(row[3])),
        first_seen=parse_stamp(str(row[4])),
        last_seen=parse_stamp(str(row[5])),
        closed_at=None if row[6] is None else parse_stamp(str(row[6])),
        close_reason=reason,
    )


def _values(user_id: UserId, market: Market, h: Holding) -> tuple[object, ...]:
    return (
        user_id,
        market,
        h.symbol,
        h.side,
        h.quantity,
        h.expiry.isoformat(),
        stamp(h.first_seen),
        stamp(h.last_seen),
        None if h.closed_at is None else stamp(h.closed_at),
        h.close_reason,
    )


def load(conn: sqlite3.Connection, user_id: UserId, market: Market) -> list[Holding]:
    """이 사용자의 기록 전부(닫힌 것 포함). 종목·방향 순."""
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM option_holdings WHERE user_id = ? AND market = ? "
        "ORDER BY symbol, side",
        (user_id, market),
    ).fetchall()
    return [_row(row) for row in rows]


def expired_since(
    conn: sqlite3.Connection, user_id: UserId, market: Market, since: datetime
) -> list[Holding]:
    """since 이후에 행사 또는 소멸로 닫힌 기록. 최근에 닫힌 것부터."""
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM option_holdings WHERE user_id = ? AND market = ? "
        "AND close_reason = 'exercised_or_expired' AND closed_at >= ? "
        "ORDER BY closed_at DESC, symbol, side",
        (user_id, market, stamp(since)),
    ).fetchall()
    return [_row(row) for row in rows]


def _reason(expiry: date, at: datetime) -> CloseReason:
    """만기일(KST)이 지난 뒤에 사라졌으면 행사 또는 소멸. 만기일 당일은 장이 끝난 뒤라도 청산으로
    본다. 그날의 장 마감 시각과 NH가 만기 종목을 잔고에서 언제 빼는지를 확인하지 못했다.
    """
    return "exercised_or_expired" if at.astimezone(KST).date() > expiry else "closed"


def _seen(row: Holding | None, p: Position, expiry: date, at: datetime) -> Holding | None:
    """잔고에 있는 옵션의 새 행. 더 나중의 잔고로 이미 맞춘 행이면 None(그대로 둔다)."""
    if row is None or (row.closed_at is not None and at > row.closed_at):
        # 처음 보거나, 닫은 뒤에 다시 잡혔다(같은 종목을 다시 샀다). 새 보유로 연다.
        return Holding(p.symbol, p.side, p.quantity, expiry, at, at)
    if row.closed_at is not None or at < row.last_seen:
        return None
    return replace(row, quantity=p.quantity, last_seen=at)


def reconcile(
    conn: sqlite3.Connection,
    user_id: UserId,
    account: AccountState,
    expiries: Mapping[str, date],
) -> None:
    """계좌의 잔고(`as_of`에 받은 것)에 기록을 맞춘다. 같은 잔고로 다시 불러도 그대로다.

    다 읽지 못한 계좌(`complete`가 거짓)로는 아무것도 바꾸지 않는다. 빠진 행을 닫으면 안 된다.
    expiries는 잔고의 옵션 종목 → 만기일(종목 마스터)이다. 여기 없는 잔고(선물, 마스터에 없는
    종목)는 기록하지 않고, 같은 종목·방향의 열린 행은 잔고에 있으므로 닫지 않는다.
    `as_of`보다 나중의 잔고로 맞춘 행은 건드리지 않는다. 조회 도구와 주문 점검이 겹쳐 맞추는 순서가
    받은 순서와 달라도 옛 잔고가 새 기록을 되돌리지 않는다. 읽기와 쓰기가 한 트랜잭션이다.
    """
    if not account.complete:
        return
    market, positions, seen_at = account.market, account.positions, account.as_of
    with conn:
        # 쓰기 잠금을 먼저 잡아, 읽은 뒤 다른 연결이 같은 행을 닫거나 열지 못하게 한다.
        conn.execute("BEGIN IMMEDIATE")
        rows = {(h.symbol, h.side): h for h in load(conn, user_id, market)}
        # 잔고에 같은 종목·방향이 여러 행이면 수량을 더한 한 행으로 기록한다.
        held: dict[tuple[str, Side], Position] = {}
        for p in positions:
            before = held.get((p.symbol, p.side))
            total = p if before is None else replace(before, quantity=before.quantity + p.quantity)
            held[(p.symbol, p.side)] = total
        changed: list[Holding] = []
        for key, p in held.items():
            expiry = expiries.get(p.symbol)
            if expiry is None:
                continue
            seen = _seen(rows.get(key), p, expiry, seen_at)
            if seen is not None:
                changed.append(seen)
        for key, row in rows.items():
            if row.closed_at is None and key not in held and seen_at > row.last_seen:
                reason = _reason(row.expiry, seen_at)
                changed.append(replace(row, closed_at=seen_at, close_reason=reason))
        conn.executemany(_PUT, [_values(user_id, market, h) for h in changed])
