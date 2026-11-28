"""확인 토큰. 원문은 저장하지 않고 SHA-256만 둔다. 소진은 UPDATE 한 문장이다."""

import hashlib
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from nh_mcp.domain import TokenPurpose, TokenRow, UserId
from nh_mcp.store.db import parse_stamp, stamp

# 저장된 용도 문자열 → 용도. 여기에 없는 값은 없는 토큰으로 읽는다.
_PURPOSES: dict[str, TokenPurpose] = {
    "order": "order",
    "limits": "limits",
    "strategy": "strategy",
    "combo": "combo",
}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class NewToken:
    """발급할 토큰의 내용. 주문이면 주문 JSON, 한도면 새 한도 JSON."""

    user_id: UserId
    purpose: TokenPurpose
    payload: str


def issue(
    conn: sqlite3.Connection, new: NewToken, now: datetime, ttl: timedelta
) -> tuple[str, datetime]:
    """새 토큰 원문과 만료 시각을 돌려준다. 원문은 이 반환값에만 있다."""
    token = secrets.token_urlsafe(32)
    expires_at = now + ttl
    with conn:
        conn.execute(
            "INSERT INTO confirm_tokens (token_hash, user_id, purpose, payload, created_at, "
            "expires_at) VALUES (?, ?, ?, ?, ?, ?)",
            (_hash(token), new.user_id, new.purpose, new.payload, stamp(now), stamp(expires_at)),
        )
    return token, expires_at


def find(conn: sqlite3.Connection, token: str) -> TokenRow | None:
    """토큰 행. 누구의 것인지는 호출하는 쪽이 `guard.confirm.judge`로 따진다."""
    row = conn.execute(
        "SELECT user_id, purpose, payload, expires_at, used_at FROM confirm_tokens "
        "WHERE token_hash = ?",
        (_hash(token),),
    ).fetchone()
    if row is None:
        return None
    purpose = _PURPOSES.get(str(row[1]))
    if purpose is None:
        return None
    return TokenRow(
        user_id=UserId(int(row[0])),
        purpose=purpose,
        payload=str(row[2]),
        expires_at=parse_stamp(str(row[3])),
        used_at=None if row[4] is None else parse_stamp(str(row[4])),
    )


def consume(
    conn: sqlite3.Connection, token: str, user_id: UserId, purpose: TokenPurpose, now: datetime
) -> bool:
    """안 쓴·내 것·이 용도·안 만료인 토큰을 소진한다. 바뀐 행이 1이면 True."""
    with conn:
        cursor = conn.execute(
            "UPDATE confirm_tokens SET used_at = ? WHERE token_hash = ? AND user_id = ? "
            "AND purpose = ? AND used_at IS NULL AND expires_at > ?",
            (stamp(now), _hash(token), user_id, purpose, stamp(now)),
        )
    return cursor.rowcount == 1
