"""백테스트 기록. 성공한 기록이 있어야 `preview_strategy`가 통과하고, `start_strategy`는 그 기록의
코드 원문을 돌린다. 사용자 조건을 항상 건다."""

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime

from nh_mcp.domain import Market, UserId
from nh_mcp.store.db import stamp

# 성공으로 치는 기록의 상태. 그 밖의 상태(실패·시간 초과 등)는 미리보기를 통과시키지 못한다.
SUCCESS = "ok"


@dataclass(frozen=True)
class RunKey:
    """같은 백테스트인지 가르는 값.

    `streams`(종목 → 흐름)와 `params`는 정렬한 JSON 문자열이다. 모델 파일을 쓰지 않은 실행이면
    `model_hash`는 None이고, None끼리만 같다.
    """

    market: Market
    streams: str
    code_hash: str
    params: str
    model_hash: str | None


@dataclass(frozen=True)
class NewRun:
    """남길 백테스트 한 건. 봉이 없었으면 `first_bar_at`·`last_bar_at`은 None."""

    user_id: UserId
    key: RunKey
    code: str
    settings: str
    requested: tuple[date, date]
    first_bar_at: datetime | None
    last_bar_at: datetime | None
    bar_count: int
    status: str
    result: str | None


def record(conn: sqlite3.Connection, run: NewRun, now: datetime) -> int:
    """기록을 남기고 id를 돌려준다."""
    key = run.key
    with conn:
        cursor = conn.execute(
            "INSERT INTO backtest_runs (user_id, market, streams, code_hash, model_hash, code, "
            "params, settings, requested_start, requested_end, first_bar_at, last_bar_at, "
            "bar_count, status, result, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run.user_id,
                key.market,
                key.streams,
                key.code_hash,
                key.model_hash,
                run.code,
                key.params,
                run.settings,
                run.requested[0].isoformat(),
                run.requested[1].isoformat(),
                None if run.first_bar_at is None else stamp(run.first_bar_at),
                None if run.last_bar_at is None else stamp(run.last_bar_at),
                run.bar_count,
                run.status,
                run.result,
                stamp(now),
            ),
        )
    if cursor.lastrowid is None:
        raise RuntimeError("백테스트 기록의 id를 받지 못했습니다.")
    return cursor.lastrowid


def has_success(conn: sqlite3.Connection, user_id: UserId, key: RunKey) -> bool:
    """이 사용자가 같은 흐름·코드·파라미터·모델로 성공한 백테스트가 있는가."""
    # `IS`는 NULL끼리도 같다고 본다(모델 없는 실행끼리).
    row = conn.execute(
        "SELECT 1 FROM backtest_runs WHERE user_id = ? AND market = ? AND streams = ? "
        "AND code_hash = ? AND params = ? AND model_hash IS ? AND status = ? LIMIT 1",
        (user_id, key.market, key.streams, key.code_hash, key.params, key.model_hash, SUCCESS),
    ).fetchone()
    return row is not None


def code_for(conn: sqlite3.Connection, user_id: UserId, key: RunKey) -> str | None:
    """이 사용자가 같은 흐름·코드·파라미터·모델로 성공한 백테스트의 코드 원문(가장 최근 것)."""
    row = conn.execute(
        "SELECT code FROM backtest_runs WHERE user_id = ? AND market = ? AND streams = ? "
        "AND code_hash = ? AND params = ? AND model_hash IS ? AND status = ? "
        "ORDER BY id DESC LIMIT 1",
        (user_id, key.market, key.streams, key.code_hash, key.params, key.model_hash, SUCCESS),
    ).fetchone()
    return None if row is None else str(row[0])
