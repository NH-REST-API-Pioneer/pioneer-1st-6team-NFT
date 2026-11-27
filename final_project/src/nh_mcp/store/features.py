"""사용자가 등록 화면에서 켜는 기능 묶음(`docs/tools.md` "기능 묶음"). 행이 있으면 켜진 것이다.

기본은 꺼짐이다. 모든 조회와 쓰기에 사용자 조건을 건다(안전 불변식 8).
"""

import sqlite3
from datetime import datetime
from typing import Literal

from nh_mcp.domain import UserId
from nh_mcp.store.db import stamp

# auto_run: "자동 실행"(preview_strategy, start_strategy, stop_strategy, get_strategy_status).
# options: "옵션"(get_option_chain, 옵션 종목의 시세·차트·주문).
# option_sell: "옵션 매도"(보유 없이 파는 옵션 주문). "옵션"이 켜져 있을 때만 켠다.
type Feature = Literal["auto_run", "options", "option_sell"]


def enabled(conn: sqlite3.Connection, user_id: UserId, feature: Feature) -> bool:
    row = conn.execute(
        "SELECT 1 FROM user_features WHERE user_id = ? AND feature = ?", (user_id, feature)
    ).fetchone()
    return row is not None


def turn_on(conn: sqlite3.Connection, user_id: UserId, feature: Feature, now: datetime) -> None:
    """켠다. 이미 켜져 있으면 처음 켠 시각을 그대로 둔다."""
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO user_features (user_id, feature, enabled_at) VALUES (?, ?, ?)",
            (user_id, feature, stamp(now)),
        )


def turn_off(conn: sqlite3.Connection, user_id: UserId, feature: Feature) -> None:
    with conn:
        conn.execute(
            "DELETE FROM user_features WHERE user_id = ? AND feature = ?", (user_id, feature)
        )
