"""암호화된 NH 키. 모든 조회에 사용자 조건을 건다. 평문 키는 이 모듈에 들어오지 않는다."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from nh_mcp.domain import Market, UserId


@dataclass(frozen=True)
class SealedKeyPair:
    sealed_app_key: bytes
    sealed_app_secret: bytes
    app_key_last4: str


def save(
    conn: sqlite3.Connection, user_id: UserId, market: Market, pair: SealedKeyPair, now: datetime
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO nh_keys (user_id, market, sealed_app_key, sealed_app_secret, "
            "app_key_last4, registered_at, last_used_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (user_id, market) DO UPDATE SET "
            "sealed_app_key = excluded.sealed_app_key, "
            "sealed_app_secret = excluded.sealed_app_secret, "
            "app_key_last4 = excluded.app_key_last4, "
            "registered_at = excluded.registered_at, last_used_at = excluded.last_used_at",
            (
                user_id,
                market,
                pair.sealed_app_key,
                pair.sealed_app_secret,
                pair.app_key_last4,
                now.isoformat(),
                now.isoformat(),
            ),
        )


def load(conn: sqlite3.Connection, user_id: UserId, market: Market) -> SealedKeyPair | None:
    row = conn.execute(
        "SELECT sealed_app_key, sealed_app_secret, app_key_last4 FROM nh_keys "
        "WHERE user_id = ? AND market = ?",
        (user_id, market),
    ).fetchone()
    if row is None:
        return None
    return SealedKeyPair(bytes(row[0]), bytes(row[1]), str(row[2]))


def registered_markets(conn: sqlite3.Connection, user_id: UserId) -> list[Market]:
    rows = conn.execute(
        "SELECT market FROM nh_keys WHERE user_id = ? ORDER BY market", (user_id,)
    ).fetchall()
    markets: list[Market] = []
    for (market,) in rows:
        if market == "domestic":
            markets.append("domestic")
        elif market == "overseas":
            markets.append("overseas")
    return markets


def delete_all(conn: sqlite3.Connection, user_id: UserId) -> None:
    with conn:
        conn.execute("DELETE FROM nh_keys WHERE user_id = ?", (user_id,))
