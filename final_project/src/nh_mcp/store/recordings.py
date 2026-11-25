"""틱·호가 저장 설정(주 DB의 `tick_recordings`). 사용자마다 저장할 (종목, 흐름) 전체 집합."""

import sqlite3
from collections import defaultdict
from datetime import datetime
from typing import cast

from nh_mcp.domain import UserId
from nh_mcp.store.db import stamp
from nh_mcp.store.ticks import RecordStream, Sub


def load(conn: sqlite3.Connection, user_id: UserId) -> frozenset[Sub]:
    rows = conn.execute(
        "SELECT symbol, stream FROM tick_recordings WHERE user_id = ?", (user_id,)
    ).fetchall()
    return frozenset(Sub(str(symbol), cast(RecordStream, stream)) for symbol, stream in rows)


def load_all(conn: sqlite3.Connection) -> dict[UserId, frozenset[Sub]]:
    found: defaultdict[UserId, set[Sub]] = defaultdict(set)
    for user_id, symbol, stream in conn.execute(
        "SELECT user_id, symbol, stream FROM tick_recordings"
    ).fetchall():
        found[UserId(int(user_id))].add(Sub(str(symbol), cast(RecordStream, stream)))
    return {user_id: frozenset(subs) for user_id, subs in found.items()}


def replace(conn: sqlite3.Connection, user_id: UserId, subs: frozenset[Sub], now: datetime) -> None:
    """이 사용자의 집합을 subs로 맞춘다. 남는 행은 처음 켠 시각을 그대로 둔다."""
    kept = load(conn, user_id)
    with conn:
        conn.executemany(
            "DELETE FROM tick_recordings WHERE user_id = ? AND symbol = ? AND stream = ?",
            [(user_id, sub.symbol, sub.stream) for sub in kept - subs],
        )
        conn.executemany(
            "INSERT INTO tick_recordings VALUES (?, ?, ?, ?)",
            [(user_id, sub.symbol, sub.stream, stamp(now)) for sub in subs - kept],
        )
