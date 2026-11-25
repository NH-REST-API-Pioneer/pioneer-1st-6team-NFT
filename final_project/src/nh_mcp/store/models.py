"""사용자가 올린 모델 파일의 목록. 파일 내용은 `models/store.py`가 디스크에 둔다.

모든 조회에 사용자 조건을 건다. 다른 사용자의 해시는 없는 것과 같다(안전 불변식 8).
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from nh_mcp.domain import ModelHash, UserId
from nh_mcp.store.db import parse_stamp, stamp


@dataclass(frozen=True)
class ModelRow:
    """모델 파일 하나. name은 화면에 보여 주기만 한다(경로에 쓰지 않는다)."""

    sha256: ModelHash
    name: str
    size: int
    uploaded_at: datetime


def add(conn: sqlite3.Connection, user_id: UserId, row: ModelRow) -> None:
    """한 사용자에게 같은 해시는 한 줄이다. 이미 있으면 그대로 둔다(처음 이름이 남는다)."""
    with conn:
        conn.execute(
            "INSERT INTO models (user_id, sha256, name, size, uploaded_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (user_id, sha256) DO NOTHING",
            (user_id, row.sha256, row.name, row.size, stamp(row.uploaded_at)),
        )


def get(conn: sqlite3.Connection, user_id: UserId, sha256: ModelHash) -> ModelRow | None:
    found = conn.execute(
        "SELECT sha256, name, size, uploaded_at FROM models WHERE user_id = ? AND sha256 = ?",
        (user_id, sha256),
    ).fetchone()
    return None if found is None else _row(found)


def list_for(conn: sqlite3.Connection, user_id: UserId) -> list[ModelRow]:
    """이 사용자의 모델 파일, 올린 순서대로."""
    rows = conn.execute(
        "SELECT sha256, name, size, uploaded_at FROM models WHERE user_id = ? "
        "ORDER BY uploaded_at, sha256",
        (user_id,),
    ).fetchall()
    return [_row(r) for r in rows]


def count(conn: sqlite3.Connection, user_id: UserId) -> int:
    row = conn.execute("SELECT COUNT(*) FROM models WHERE user_id = ?", (user_id,)).fetchone()
    return int(row[0])


def remove(conn: sqlite3.Connection, user_id: UserId, sha256: ModelHash) -> bool:
    """지웠으면 True, 이 사용자에게 그 해시가 없었으면 False."""
    with conn:
        cursor = conn.execute(
            "DELETE FROM models WHERE user_id = ? AND sha256 = ?", (user_id, sha256)
        )
    return cursor.rowcount > 0


def _row(found: tuple[str, str, int, str]) -> ModelRow:
    sha256, name, size, uploaded_at = found
    return ModelRow(ModelHash(sha256), name, int(size), parse_stamp(uploaded_at))
