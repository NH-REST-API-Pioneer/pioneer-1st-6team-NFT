"""실행 상태의 전이 표: 표에 있는 것만 되고, 멈춘 실행은 다시 바뀌지 않는다."""

import itertools
import sqlite3
from datetime import timedelta

import pytest

from nh_mcp.domain import Streams, UserId
from nh_mcp.runner.machine import TRANSITIONS, IllegalTransition, advance, check
from nh_mcp.store import db, strategy_runs, users
from nh_mcp.store.strategy_runs import REASONS, STATES, Change, RunState, StopReason, StrategyRun

from .rig import SYMBOL, T0, scope

LATER = T0 + timedelta(minutes=5)


def a_run(user: UserId, run_id: str = "run-1") -> StrategyRun:
    streams: Streams = {SYMBOL: frozenset({"minute"})}
    return StrategyRun(run_id, user, "domestic", streams, "c" * 64, {}, None, scope(), T0)


def stored(state: RunState = "starting") -> tuple[sqlite3.Connection, StrategyRun]:
    conn = db.connect(":memory:")
    run = a_run(users.get_or_create(conn, "g-a", "a@example.com", T0))
    strategy_runs.insert(conn, run)
    if state != "starting":
        assert strategy_runs.set_state(
            conn, run.user_id, run.run_id, Change("starting", state, None), T0
        )
    return conn, run


def test_the_table_is_exactly_the_documented_lifecycle() -> None:
    stops = set(REASONS) - {"server_restart"}
    assert set(TRANSITIONS) == {
        ("starting", "running"),
        ("starting", "stopped"),
        ("starting", "paused"),
        ("running", "stopped"),
        ("running", "paused"),
        ("paused", "stopped"),
    }
    assert TRANSITIONS["starting", "running"] == {None}
    assert TRANSITIONS["running", "stopped"] == stops
    assert TRANSITIONS["running", "paused"] == {"server_restart"}
    assert TRANSITIONS["paused", "stopped"] == {"user", "key_removed", "expired"}


@pytest.mark.parametrize(
    ("before", "after", "reason"),
    [
        (b, a, r)
        for b, a in itertools.product(STATES, STATES)
        for r in (None, *REASONS)
        if r not in TRANSITIONS.get((b, a), frozenset())
    ],
)
def test_every_transition_outside_the_table_is_refused(
    before: RunState, after: RunState, reason: StopReason | None
) -> None:
    with pytest.raises(IllegalTransition):
        check(before, after, reason)


def test_advance_writes_the_state_reason_and_stop_time() -> None:
    conn, run = stored("running")

    assert advance(conn, run, "stopped", "daily_loss", LATER) == "running"

    row = strategy_runs.get(conn, run.user_id, run.run_id)
    assert row is not None
    assert (row.state, row.reason, row.stopped_at) == ("stopped", "daily_loss", LATER)


def test_a_stopped_run_never_changes_again() -> None:
    conn, run = stored("running")
    advance(conn, run, "stopped", "user", LATER)

    later: list[tuple[RunState, StopReason | None]] = [
        ("running", None),
        ("paused", "server_restart"),
        ("stopped", "user"),
    ]
    for after, reason in later:
        with pytest.raises(IllegalTransition):
            advance(conn, run, after, reason, LATER)
    row = strategy_runs.get(conn, run.user_id, run.run_id)
    assert row is not None and (row.state, row.reason) == ("stopped", "user")


def test_pausing_is_only_for_a_server_restart() -> None:
    conn, run = stored("running")

    with pytest.raises(IllegalTransition):
        advance(conn, run, "paused", "user", LATER)
    advance(conn, run, "paused", "server_restart", LATER)
    with pytest.raises(IllegalTransition):
        advance(conn, run, "running", None, LATER)


def test_another_users_run_cannot_be_moved() -> None:
    conn, run = stored("running")
    other = users.get_or_create(conn, "g-b", "b@example.com", T0)

    with pytest.raises(IllegalTransition):
        advance(conn, a_run(other, run.run_id), "stopped", "user", LATER)
    row = strategy_runs.get(conn, run.user_id, run.run_id)
    assert row is not None and row.state == "running"
