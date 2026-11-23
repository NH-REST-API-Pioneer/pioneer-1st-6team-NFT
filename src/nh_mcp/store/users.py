"""사용자. 저장하는 것은 Google 계정 식별자와 이메일뿐이다."""

import sqlite3
from datetime import datetime

from nh_mcp.domain import UserId


def get_or_create(conn: sqlite3.Connection, google_sub: str, email: str, now: datetime) -> UserId:
    with conn:
        conn.execute(
            "INSERT INTO users (google_sub, email, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT (google_sub) DO UPDATE SET email = excluded.email",
            (google_sub, email, now.isoformat()),
        )
    row = conn.execute("SELECT id FROM users WHERE google_sub = ?", (google_sub,)).fetchone()
    return UserId(int(row[0]))


def is_suspended(conn: sqlite3.Connection, user_id: UserId) -> bool:
    row = conn.execute("SELECT suspended FROM users WHERE id = ?", (user_id,)).fetchone()
    # 모르는 사용자는 막는다.
    return row is None or bool(row[0])


def set_suspended(conn: sqlite3.Connection, user_id: UserId, suspended: bool) -> None:
    with conn:
        conn.execute("UPDATE users SET suspended = ? WHERE id = ?", (int(suspended), user_id))
