"""자동 실행 기록: 사용자로 나뉘고, 켠 내용을 그대로 다시 읽고, 기록은 최근 것만 남는다."""

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from nh_mcp.domain import ModelHash, StrategyScope, UserId
from nh_mcp.store import db, strategy_runs, users
from nh_mcp.store.strategy_runs import MAX_EVENTS, Change, RunEvent, StrategyRun

NOW = datetime(2026, 12, 1, 1, 0, tzinfo=UTC)
SCOPE = StrategyScope(2, 3, 7, Decimal("123456.5"), NOW + timedelta(days=2), "liquidate", "suggest")


def setup() -> tuple[sqlite3.Connection, UserId, UserId]:
    conn = db.connect(":memory:")
    a = users.get_or_create(conn, "g-a", "a@example.com", NOW)
    b = users.get_or_create(conn, "g-b", "b@example.com", NOW)
    return conn, a, b


def a_run(user: UserId, run_id: str = "r1") -> StrategyRun:
    return StrategyRun(
        run_id=run_id,
        user_id=user,
        market="domestic",
        streams={"B": frozenset({"tick", "day"}), "A": frozenset({"minute"})},
        code_hash="c" * 64,
        params={"window": 5, "band": "0.02"},
        model_hash=ModelHash("d" * 64),
        scope=SCOPE,
        started_at=NOW,
    )


def test_a_run_reads_back_exactly_as_it_was_started() -> None:
    conn, a, _ = setup()
    run = a_run(a)

    strategy_runs.insert(conn, run)

    row = strategy_runs.get(conn, a, "r1")
    assert row is not None
    assert row.run == run
    assert (row.state, row.reason, row.stopped_at, row.session_id) == ("starting", None, None, None)
    text = conn.execute("SELECT streams, scope FROM strategy_runs").fetchone()
    assert text[0] == '{"A":["minute"],"B":["day","tick"]}'
    assert '"daily_loss":"123456.5"' in text[1]


def test_rows_are_split_by_user() -> None:
    conn, a, b = setup()
    strategy_runs.insert(conn, a_run(a))

    assert strategy_runs.get(conn, b, "r1") is None
    assert strategy_runs.list_for(conn, b) == []
    assert not strategy_runs.set_state(conn, b, "r1", Change("starting", "running", None), NOW)
    strategy_runs.set_session(conn, b, "r1", "stolen")
    row = strategy_runs.get(conn, a, "r1")
    assert row is not None and (row.state, row.session_id) == ("starting", None)


def test_a_state_change_needs_the_expected_current_state() -> None:
    conn, a, _ = setup()
    strategy_runs.insert(conn, a_run(a))

    assert not strategy_runs.set_state(conn, a, "r1", Change("running", "stopped", "user"), NOW)
    assert strategy_runs.set_state(conn, a, "r1", Change("starting", "running", None), NOW)


def test_clean_up_finds_running_runs_and_left_sessions_of_every_user() -> None:
    conn, a, b = setup()
    for user, run_id in ((a, "running"), (b, "starting"), (a, "stopped"), (b, "left")):
        strategy_runs.insert(conn, a_run(user, run_id))
    strategy_runs.set_state(conn, a, "running", Change("starting", "running", None), NOW)
    strategy_runs.set_state(conn, a, "stopped", Change("starting", "stopped", "user"), NOW)
    strategy_runs.set_state(conn, b, "left", Change("starting", "stopped", "user"), NOW)
    strategy_runs.set_session(conn, b, "left", "s-1")

    found = {r.run.run_id for r in strategy_runs.to_clean_up(conn)}

    assert found == {"running", "starting", "left"}


def test_events_keep_the_latest_ones_and_one_suggestion_per_symbol() -> None:
    conn, a, b = setup()
    strategy_runs.insert(conn, a_run(a))
    for i in range(MAX_EVENTS + 20):
        strategy_runs.log(conn, a, "r1", RunEvent(NOW, "output", None, {"i": i}))
    for position in (1, -1):
        event = RunEvent(NOW, "suggestion", "A", {"A": {"position": position, "price": None}})
        strategy_runs.log(conn, a, "r1", event)

    kept = strategy_runs.events(conn, a, "r1")

    outputs = [e.detail["i"] for e in kept if e.kind == "output"]
    assert outputs == list(range(20, MAX_EVENTS + 20))
    suggestions = strategy_runs.suggestions(conn, a, "r1")
    assert suggestions["A"].detail == {"A": {"position": -1, "price": None}}
    assert strategy_runs.events(conn, b, "r1") == []


def test_live_runs_are_starting_or_running_and_only_mine() -> None:
    conn, a, b = setup()
    for run_id in ("r1", "r2", "r3", "r4"):
        strategy_runs.insert(conn, a_run(a, run_id))
    strategy_runs.insert(conn, a_run(b, "b1"))
    strategy_runs.set_state(conn, a, "r2", Change("starting", "running", None), NOW)
    strategy_runs.set_state(conn, a, "r3", Change("starting", "paused", "server_restart"), NOW)
    strategy_runs.set_state(conn, a, "r4", Change("starting", "stopped", "user"), NOW)

    assert [r.run.run_id for r in strategy_runs.live(conn, a)] == ["r1", "r2"]
    assert [r.run.run_id for r in strategy_runs.live(conn, b)] == ["b1"]


def test_running_users_counts_other_people_with_live_runs_only() -> None:
    conn, a, b = setup()
    c = users.get_or_create(conn, "g-c", "c@example.com", NOW)
    strategy_runs.insert(conn, a_run(a, "a1"))
    strategy_runs.insert(conn, a_run(b, "b1"))
    strategy_runs.insert(conn, a_run(b, "b2"))
    strategy_runs.insert(conn, a_run(c, "c1"))
    strategy_runs.set_state(conn, c, "c1", Change("starting", "paused", "server_restart"), NOW)

    assert strategy_runs.running_users(conn, other_than=a) == 1
    assert strategy_runs.running_users(conn, other_than=c) == 2


def test_recent_is_latest_started_first_and_bounded() -> None:
    conn, a, b = setup()
    for i in range(5):
        later = replace(a_run(a, f"r{i}"), started_at=NOW + timedelta(minutes=i))
        strategy_runs.insert(conn, later)
    strategy_runs.insert(conn, a_run(b, "b1"))

    assert [r.run.run_id for r in strategy_runs.recent(conn, a, 3)] == ["r4", "r3", "r2"]
    assert [r.run.run_id for r in strategy_runs.recent(conn, b, 3)] == ["b1"]


def test_recent_events_are_the_last_ones_oldest_first_without_suggestions() -> None:
    conn, a, b = setup()
    strategy_runs.insert(conn, a_run(a))
    for i in range(6):
        event = RunEvent(NOW + timedelta(seconds=i), "output", None, {"i": i})
        strategy_runs.log(conn, a, "r1", event)
    suggestion = RunEvent(NOW, "suggestion", "A", {"A": {"position": 1, "price": None}})
    strategy_runs.log(conn, a, "r1", suggestion)

    events = strategy_runs.recent_events(conn, a, "r1", 3)

    assert [e.detail for e in events] == [{"i": 3}, {"i": 4}, {"i": 5}]
    assert strategy_runs.recent_events(conn, b, "r1", 3) == []
