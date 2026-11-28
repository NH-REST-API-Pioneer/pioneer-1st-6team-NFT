"""옵션 묶음(M6). 묶음 하나가 `combos` 한 줄이고, 다리 주문은 `orders.combo_id`로 묶인다.

- request·plan은 서비스가 쓴 JSON 문자열을 그대로 둔다. 읽어서 타입으로 바꾸는 것은 서비스다.
- 상태는 저장하지 않는다. 다리 주문의 체결에서 매번 계산한다. 넣은 뒤에 바뀌는 칸은
  lease_until뿐이다. 쥔 실행만 늘리고 놓는다(쥔 기한이 같을 때만 바꾼다).
- 모든 조회와 쓰기에 사용자 조건을 건다(안전 불변식 8). 남의 묶음 id는 없는 묶음이다.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from nh_mcp.domain import Market, UserId
from nh_mcp.store.db import parse_stamp, stamp


@dataclass(frozen=True)
class Combo:
    """넣은 뒤에는 바뀌지 않는 것."""

    id: str
    user_id: UserId
    market: Market
    request: str  # 묶음 요청 JSON
    plan: str  # guard가 만든 계획 JSON
    created_at: datetime


@dataclass(frozen=True)
class ComboRow:
    combo: Combo
    lease_until: datetime | None  # 실행이 쥐고 있는 기한. 쥔 실행이 없으면 None


def insert(conn: sqlite3.Connection, combo: Combo) -> None:
    """새 묶음. 아무도 쥐고 있지 않다."""
    with conn:
        conn.execute(
            "INSERT INTO combos (id, user_id, market, request, plan, created_at, lease_until) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL)",
            (
                combo.id,
                combo.user_id,
                combo.market,
                combo.request,
                combo.plan,
                stamp(combo.created_at),
            ),
        )


def get(conn: sqlite3.Connection, user_id: UserId, combo_id: str) -> ComboRow | None:
    row = conn.execute(
        "SELECT id, user_id, market, request, plan, created_at, lease_until FROM combos "
        "WHERE id = ? AND user_id = ?",
        (combo_id, user_id),
    ).fetchone()
    if row is None:
        return None
    combo = Combo(
        id=str(row[0]),
        user_id=UserId(int(row[1])),
        market="domestic" if row[2] == "domestic" else "overseas",
        request=str(row[3]),
        plan=str(row[4]),
        created_at=parse_stamp(str(row[5])),
    )
    return ComboRow(combo, None if row[6] is None else parse_stamp(str(row[6])))


def claim(
    conn: sqlite3.Connection, user_id: UserId, combo_id: str, now: datetime, until: datetime
) -> bool:
    """아무도 쥐고 있지 않거나 기한이 지났으면 until까지 쥔다. 쥐었으면 True.

    한 문장이라 같은 묶음을 동시에 쥐려는 실행 중 하나만 이긴다.
    """
    with conn:
        cursor = conn.execute(
            "UPDATE combos SET lease_until = ? WHERE id = ? AND user_id = ? "
            "AND (lease_until IS NULL OR lease_until < ?)",
            (stamp(until), combo_id, user_id, stamp(now)),
        )
    return cursor.rowcount == 1


def renew(
    conn: sqlite3.Connection, user_id: UserId, combo_id: str, held: datetime, until: datetime
) -> bool:
    """아직 held까지 쥐고 있으면 until까지 늘린다. 늘렸으면 True.

    실행은 다리를 보낼 때마다 늘린다. 기한이 지나 다른 실행이 쥐었으면 기한이 held가 아니라
    늘지 않는다(그 실행은 더 보내지 않는다).
    """
    with conn:
        cursor = conn.execute(
            "UPDATE combos SET lease_until = ? WHERE id = ? AND user_id = ? AND lease_until = ?",
            (stamp(until), combo_id, user_id, stamp(held)),
        )
    return cursor.rowcount == 1


def holds(
    conn: sqlite3.Connection, user_id: UserId, combo_id: str, held: datetime, legs: int
) -> bool:
    """아직 held까지 쥐고 있고 그 묶음의 다리 주문 행(`orders.combo_id`)이 legs개인가.

    다리를 보내기 바로 앞에서 본다. 다른 실행이 쥐었거나 다리를 더 보냈으면 False다. 한 문장이라
    두 조건을 같은 때에 본다.
    """
    row = conn.execute(
        "SELECT 1 FROM combos WHERE id = ? AND user_id = ? AND lease_until = ? "
        "AND (SELECT COUNT(*) FROM orders WHERE user_id = ? AND combo_id = ?) = ?",
        (combo_id, user_id, stamp(held), user_id, combo_id, legs),
    ).fetchone()
    return row is not None


def release(conn: sqlite3.Connection, user_id: UserId, combo_id: str, held: datetime) -> None:
    """held까지 쥐고 있던 것을 놓는다. 그사이 다른 실행이 쥐었으면 그대로 둔다."""
    with conn:
        conn.execute(
            "UPDATE combos SET lease_until = NULL WHERE id = ? AND user_id = ? AND lease_until = ?",
            (combo_id, user_id, stamp(held)),
        )
