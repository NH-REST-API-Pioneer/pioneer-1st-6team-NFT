"""실행 상태의 전이 표. 상태를 바꾸는 곳은 여기 `advance` 하나다.

```
starting ──▶ running ──▶ stopped(사유)
    │           │
    │           └──▶ paused(server_restart) ──▶ stopped(user·key_removed·expired)
    ├──▶ paused(server_restart)
    └──▶ stopped(켜다 실패한 사유)
```

일시정지는 서버가 다시 뜰 때만 된다. 일시정지에서 다시 돌리는 길은 없다(다시 켜려면 미리보기부터).
멈춘 실행은 다시 바뀌지 않는다.
"""

import sqlite3
from datetime import datetime

from nh_mcp.store import strategy_runs
from nh_mcp.store.strategy_runs import REASONS, Change, RunState, StopReason, StrategyRun

# 멈출 때 쓸 수 있는 사유. 일시정지 사유(server_restart)는 멈춤에 쓰지 않는다.
_STOPS: frozenset[StopReason | None] = frozenset(r for r in REASONS if r != "server_restart")
_PAUSE: frozenset[StopReason | None] = frozenset({"server_restart"})

# (지금, 다음) → 쓸 수 있는 사유. 표에 없는 전이는 할 수 없다.
TRANSITIONS: dict[tuple[RunState, RunState], frozenset[StopReason | None]] = {
    ("starting", "running"): frozenset({None}),
    ("starting", "stopped"): _STOPS,
    ("starting", "paused"): _PAUSE,
    ("running", "stopped"): _STOPS,
    ("running", "paused"): _PAUSE,
    ("paused", "stopped"): frozenset({"user", "key_removed", "expired"}),
}


class IllegalTransition(Exception):
    def __init__(self, before: RunState, after: RunState, reason: StopReason | None) -> None:
        super().__init__(f"{before} → {after}({reason})는 할 수 없는 전이입니다.")
        self.before = before
        self.after = after
        self.reason = reason


def check(before: RunState, after: RunState, reason: StopReason | None) -> None:
    """표에 있는 전이면 지나가고, 아니면 IllegalTransition."""
    if reason not in TRANSITIONS.get((before, after), frozenset()):
        raise IllegalTransition(before, after, reason)


def advance(
    conn: sqlite3.Connection,
    run: StrategyRun,
    after: RunState,
    reason: StopReason | None,
    at: datetime,
) -> RunState:
    """지금 상태를 읽어 표대로 바꾸고 바꾸기 전 상태를 돌려준다.

    없는 실행, 표에 없는 전이, 그사이에 다른 쪽이 먼저 바꾼 경우 모두 IllegalTransition이다.
    """
    row = strategy_runs.get(conn, run.user_id, run.run_id)
    if row is None:
        raise IllegalTransition("stopped", after, reason)
    check(row.state, after, reason)
    change = Change(row.state, after, reason)
    if not strategy_runs.set_state(conn, run.user_id, run.run_id, change, at):
        raise IllegalTransition(row.state, after, reason)
    return row.state
