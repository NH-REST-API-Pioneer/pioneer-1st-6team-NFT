"""감사 로그. 키와 NH 토큰은 처음부터 넣지 않는다."""

import json
import sqlite3
from datetime import datetime

from nh_mcp.domain import JsonValue, UserId


def record(
    conn: sqlite3.Connection,
    user_id: UserId,
    action: str,
    detail: dict[str, JsonValue],
    now: datetime,
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO audit_log (user_id, at, action, detail) VALUES (?, ?, ?, ?)",
            (user_id, now.isoformat(), action, json.dumps(detail, ensure_ascii=False)),
        )
