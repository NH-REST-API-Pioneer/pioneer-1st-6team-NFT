"""자동 실행 기록(M4b): 실행 하나가 `strategy_runs` 한 줄, 그 실행에 일어난 일은 `strategy_events`.

- 켠 뒤에 바뀌지 않는 것(`StrategyRun`)과 바뀌는 칸(상태, 사유, 멈춘 시각, 세션, 마지막 이벤트)을
  나눠 읽는다. 상태는 `runner/machine.py`의 전이 표를 지나서만 `set_state`로 바꾼다.
- 모든 조회와 쓰기에 사용자 조건을 건다(안전 불변식 8). 서버가 다시 뜰 때 치울 줄을 찾는
  `to_clean_up`과, 서버 전체 동시 사용자 수만 세는 `running_users`만 사용자를 가리지 않는다.
- 기록은 실행마다 최근 MAX_EVENTS개만 남긴다. 제안(suggestion)은 종목마다 가장 최근 것 하나만 두고
  그 수에 세지 않는다(종목은 많아야 5개).
"""

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, cast

from nh_mcp.domain import (
    JsonValue,
    Market,
    ModelHash,
    StrategyScope,
    Stream,
    Streams,
    UserId,
    model_hash,
)
from nh_mcp.store.db import parse_stamp, stamp

type RunState = Literal["starting", "running", "paused", "stopped"]
type StopReason = Literal[
    "user",
    "expired",
    "daily_orders",
    "daily_loss",
    "stale_feed",
    "rejections",
    "sandbox_failed",
    "invalid_output",
    "key_removed",
    "unknown_order",
    "server_restart",
]
type EventKind = Literal["output", "order", "fill", "stop", "warning", "suggestion"]

STATES: tuple[RunState, ...] = ("starting", "running", "paused", "stopped")
REASONS: tuple[StopReason, ...] = (
    "user",
    "expired",
    "daily_orders",
    "daily_loss",
    "stale_feed",
    "rejections",
    "sandbox_failed",
    "invalid_output",
    "key_removed",
    "unknown_order",
    "server_restart",
)
_STREAMS: tuple[Stream, ...] = ("tick", "book", "minute", "day")
# 실행 하나에 남기는 기록 수(제안 빼고).
MAX_EVENTS = 500


@dataclass(frozen=True)
class StrategyRun:
    """켠 뒤에는 바뀌지 않는 것. 승인 범위(`scope`)와 해시가 승인받은 그대로다."""

    run_id: str
    user_id: UserId
    market: Market
    streams: Streams
    code_hash: str
    params: dict[str, JsonValue]
    model_hash: ModelHash | None
    scope: StrategyScope
    started_at: datetime


@dataclass(frozen=True)
class RunRow:
    """한 줄 전체. session_id는 실행기가 만든 추측할 수 없는 값이라 repr에 넣지 않는다."""

    run: StrategyRun
    state: RunState
    reason: StopReason | None
    stopped_at: datetime | None
    session_id: str | None = field(repr=False)
    last_event_at: datetime | None


@dataclass(frozen=True)
class RunEvent:
    at: datetime
    kind: EventKind
    symbol: str | None
    detail: dict[str, JsonValue]


@dataclass(frozen=True)
class Change:
    """상태 바꾸기 하나. 허용되는 것인지는 runner/machine.py의 전이 표가 본다."""

    before: RunState
    after: RunState
    reason: StopReason | None


def _canonical(value: JsonValue) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def streams_text(streams: Streams) -> str:
    """종목코드 순, 종목마다 흐름 이름 순의 JSON. 백테스트 기록(`streams_key`)과 같은 모양이다."""
    return _canonical({symbol: [*sorted(names)] for symbol, names in sorted(streams.items())})


def scope_data(scope: StrategyScope) -> dict[str, JsonValue]:
    return {
        "quantity": scope.quantity,
        "max_position": scope.max_position,
        "daily_orders": scope.daily_orders,
        "daily_loss": format(scope.daily_loss, "f"),
        "expires_at": scope.expires_at.astimezone(UTC).isoformat(),
        "on_loss": scope.on_loss,
        "mode": scope.mode,
    }


def _object(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise ValueError("JSON 객체가 아닙니다.")
    return cast(dict[str, object], raw)


def _int(raw: object) -> int:
    if type(raw) is not int:
        raise ValueError("정수가 아닙니다.")
    return raw


def _streams(text: str) -> Streams:
    out: dict[str, frozenset[Stream]] = {}
    for symbol, names in _object(json.loads(text)).items():
        if not isinstance(names, list):
            raise ValueError("흐름 목록이 아닙니다.")
        out[symbol] = frozenset(s for s in _STREAMS if s in cast(list[object], names))
    return out


def _scope(text: str) -> StrategyScope:
    values = _object(json.loads(text))
    on_loss, mode = values["on_loss"], values["mode"]
    return StrategyScope(
        quantity=_int(values["quantity"]),
        max_position=_int(values["max_position"]),
        daily_orders=_int(values["daily_orders"]),
        daily_loss=Decimal(str(values["daily_loss"])),
        expires_at=datetime.fromisoformat(str(values["expires_at"])),
        on_loss="liquidate" if on_loss == "liquidate" else "hold",
        mode="auto" if mode == "auto" else "suggest",
    )


def _state(text: object) -> RunState:
    for state in STATES:
        if state == text:
            return state
    raise ValueError(f"알 수 없는 상태: {text}")


def _reason(text: object) -> StopReason | None:
    if text is None:
        return None
    for reason in REASONS:
        if reason == text:
            return reason
    raise ValueError(f"알 수 없는 사유: {text}")


_KINDS: tuple[EventKind, ...] = ("output", "order", "fill", "stop", "warning", "suggestion")


def _kind(text: object) -> EventKind:
    for kind in _KINDS:
        if kind == text:
            return kind
    raise ValueError(f"알 수 없는 기록: {text}")


def _stamp_or_none(raw: object) -> datetime | None:
    return None if raw is None else parse_stamp(str(raw))


_COLUMNS = (
    "run_id, user_id, market, streams, code_hash, params, model_hash, scope, state, reason, "
    "started_at, stopped_at, session_id, last_event_at"
)


def _row(row: tuple[object, ...]) -> RunRow:
    params = _object(json.loads(str(row[5])))
    hashed = None if row[6] is None else model_hash(str(row[6]))
    run = StrategyRun(
        run_id=str(row[0]),
        user_id=UserId(int(str(row[1]))),
        market="domestic" if row[2] == "domestic" else "overseas",
        streams=_streams(str(row[3])),
        code_hash=str(row[4]),
        params=cast(dict[str, JsonValue], params),
        model_hash=hashed,
        scope=_scope(str(row[7])),
        started_at=parse_stamp(str(row[10])),
    )
    return RunRow(
        run=run,
        state=_state(row[8]),
        reason=_reason(row[9]),
        stopped_at=_stamp_or_none(row[11]),
        session_id=None if row[12] is None else str(row[12]),
        last_event_at=_stamp_or_none(row[13]),
    )


def insert(conn: sqlite3.Connection, run: StrategyRun) -> None:
    """새 실행을 `starting`으로 남긴다."""
    with conn:
        conn.execute(
            f"INSERT INTO strategy_runs ({_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'starting', NULL, ?, NULL, NULL, NULL)",
            (
                run.run_id,
                run.user_id,
                run.market,
                streams_text(run.streams),
                run.code_hash,
                _canonical(run.params),
                run.model_hash,
                _canonical(scope_data(run.scope)),
                stamp(run.started_at),
            ),
        )


def get(conn: sqlite3.Connection, user_id: UserId, run_id: str) -> RunRow | None:
    found = conn.execute(
        f"SELECT {_COLUMNS} FROM strategy_runs WHERE user_id = ? AND run_id = ?",
        (user_id, run_id),
    ).fetchone()
    return None if found is None else _row(found)


def list_for(conn: sqlite3.Connection, user_id: UserId) -> list[RunRow]:
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM strategy_runs WHERE user_id = ? ORDER BY started_at, run_id",
        (user_id,),
    ).fetchall()
    return [_row(r) for r in rows]


def live(conn: sqlite3.Connection, user_id: UserId) -> list[RunRow]:
    """이 사용자의 켜는 중이거나 도는 실행(`starting`·`running`)."""
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM strategy_runs WHERE user_id = ? "
        "AND state IN ('starting', 'running') ORDER BY started_at, run_id",
        (user_id,),
    ).fetchall()
    return [_row(r) for r in rows]


def recent(conn: sqlite3.Connection, user_id: UserId, limit: int) -> list[RunRow]:
    """이 사용자의 실행, 최근에 켠 것부터 limit개."""
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM strategy_runs WHERE user_id = ? "
        "ORDER BY started_at DESC, run_id DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    return [_row(r) for r in rows]


def running_users(conn: sqlite3.Connection, other_than: UserId) -> int:
    """`starting`·`running` 실행이 있는 다른 사용자 수. 누구인지는 돌려주지 않는다."""
    row: tuple[int] = conn.execute(
        "SELECT COUNT(DISTINCT user_id) FROM strategy_runs "
        "WHERE state IN ('starting', 'running') AND user_id != ?",
        (other_than,),
    ).fetchone()
    return row[0]


def to_clean_up(conn: sqlite3.Connection) -> list[RunRow]:
    """서버가 다시 뜰 때 치울 줄: 돌던 것(`starting`·`running`)과 세션이 남은 것. 모든 사용자."""
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM strategy_runs "
        "WHERE state IN ('starting', 'running') OR session_id IS NOT NULL ORDER BY started_at"
    ).fetchall()
    return [_row(r) for r in rows]


def set_state(
    conn: sqlite3.Connection, user_id: UserId, run_id: str, change: Change, at: datetime
) -> bool:
    """상태가 아직 change.before일 때만 바꾼다. 바뀌었으면 True.

    `paused`·`stopped`로 갈 때 멈춘 시각을 적는다.
    """
    stopped_at = stamp(at) if change.after in ("paused", "stopped") else None
    with conn:
        cursor = conn.execute(
            "UPDATE strategy_runs SET state = ?, reason = ?, "
            "stopped_at = COALESCE(?, stopped_at) "
            "WHERE user_id = ? AND run_id = ? AND state = ?",
            (change.after, change.reason, stopped_at, user_id, run_id, change.before),
        )
    return cursor.rowcount == 1


def set_session(
    conn: sqlite3.Connection, user_id: UserId, run_id: str, session_id: str | None
) -> None:
    """열린 전략 컨테이너의 세션 식별자. 닫았으면 None."""
    with conn:
        conn.execute(
            "UPDATE strategy_runs SET session_id = ? WHERE user_id = ? AND run_id = ?",
            (session_id, user_id, run_id),
        )


def touch(conn: sqlite3.Connection, user_id: UserId, run_id: str, at: datetime) -> None:
    """전략에 마지막으로 시장 이벤트를 넣은 시각."""
    with conn:
        conn.execute(
            "UPDATE strategy_runs SET last_event_at = ? WHERE user_id = ? AND run_id = ?",
            (stamp(at), user_id, run_id),
        )


def log(conn: sqlite3.Connection, user_id: UserId, run_id: str, event: RunEvent) -> None:
    """기록 하나를 남긴다. 제안은 그 종목의 옛 제안을 지우고, 나머지는 최근 MAX_EVENTS개만 둔다."""
    with conn:
        if event.kind == "suggestion":
            conn.execute(
                "DELETE FROM strategy_events WHERE user_id = ? AND run_id = ? "
                "AND kind = 'suggestion' AND symbol IS ?",
                (user_id, run_id, event.symbol),
            )
        conn.execute(
            "INSERT INTO strategy_events (run_id, user_id, at, kind, symbol, detail) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                run_id,
                user_id,
                stamp(event.at),
                event.kind,
                event.symbol,
                _canonical(event.detail),
            ),
        )
        conn.execute(
            "DELETE FROM strategy_events WHERE user_id = ? AND run_id = ? "
            "AND kind != 'suggestion' AND id NOT IN ("
            "SELECT id FROM strategy_events WHERE user_id = ? AND run_id = ? "
            "AND kind != 'suggestion' ORDER BY id DESC LIMIT ?)",
            (user_id, run_id, user_id, run_id, MAX_EVENTS),
        )


def _event(row: tuple[object, ...]) -> RunEvent:
    return RunEvent(
        at=parse_stamp(str(row[0])),
        kind=_kind(row[1]),
        symbol=None if row[2] is None else str(row[2]),
        detail=cast(dict[str, JsonValue], _object(json.loads(str(row[3])))),
    )


def events(conn: sqlite3.Connection, user_id: UserId, run_id: str) -> list[RunEvent]:
    """그 실행의 기록, 오래된 것부터(제안 포함)."""
    rows = conn.execute(
        "SELECT at, kind, symbol, detail FROM strategy_events "
        "WHERE user_id = ? AND run_id = ? ORDER BY id",
        (user_id, run_id),
    ).fetchall()
    return [_event(r) for r in rows]


def recent_events(
    conn: sqlite3.Connection, user_id: UserId, run_id: str, limit: int
) -> list[RunEvent]:
    """그 실행의 최근 기록 limit개(제안 빼고), 오래된 것부터."""
    rows = conn.execute(
        "SELECT at, kind, symbol, detail FROM strategy_events "
        "WHERE user_id = ? AND run_id = ? AND kind != 'suggestion' ORDER BY id DESC LIMIT ?",
        (user_id, run_id, limit),
    ).fetchall()
    return [_event(r) for r in reversed(rows)]


def suggestions(conn: sqlite3.Connection, user_id: UserId, run_id: str) -> dict[str, RunEvent]:
    """종목마다 가장 최근 제안."""
    return {
        e.symbol: e for e in events(conn, user_id, run_id) if e.kind == "suggestion" and e.symbol
    }
