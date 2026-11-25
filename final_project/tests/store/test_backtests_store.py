import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime

from nh_mcp.domain import UserId
from nh_mcp.store import backtests, db, users
from nh_mcp.store.backtests import NewRun, RunKey

NOW = datetime(2026, 12, 1, 1, 0, tzinfo=UTC)
STREAMS = '{"A016C000":["minute"]}'
KEY = RunKey("domestic", STREAMS, "ab" * 32, '{"window":"20"}', None)


def two_users(conn: sqlite3.Connection) -> tuple[UserId, UserId]:
    return (
        users.get_or_create(conn, "sub-a", "a@example.com", NOW),
        users.get_or_create(conn, "sub-b", "b@example.com", NOW),
    )


def run_for(user_id: UserId, key: RunKey = KEY, status: str = "ok") -> NewRun:
    return NewRun(
        user_id=user_id,
        key=key,
        code="def on_event(event, ctx): return None",
        settings='{"fee":"0"}',
        requested=(date(2026, 11, 1), date(2026, 11, 20)),
        first_bar_at=NOW,
        last_bar_at=NOW,
        bar_count=120,
        status=status,
        result='{"trades":0}' if status == "ok" else None,
    )


def test_success_is_found_only_for_the_same_key() -> None:
    conn = db.connect(":memory:")
    user_a, _ = two_users(conn)
    backtests.record(conn, run_for(user_a), NOW)

    assert backtests.has_success(conn, user_a, KEY) is True
    for other in (
        replace(KEY, streams='{"A016C000":["day"]}'),
        replace(KEY, streams='{"A016C000":["day","minute"]}'),
        replace(KEY, streams='{"A016C000":["minute"],"A0173000":["minute"]}'),
        replace(KEY, streams='{"A0173000":["minute"]}'),
        replace(KEY, code_hash="cd" * 32),
        replace(KEY, params='{"window":"21"}'),
        replace(KEY, model_hash="ef" * 32),
        replace(KEY, market="overseas"),
    ):
        assert backtests.has_success(conn, user_a, other) is False


def test_model_hash_matches_only_the_same_hash_or_both_none() -> None:
    conn = db.connect(":memory:")
    user_a, _ = two_users(conn)
    with_model = replace(KEY, model_hash="ef" * 32)
    backtests.record(conn, run_for(user_a, with_model), NOW)

    assert backtests.has_success(conn, user_a, with_model) is True
    # 모델 없는 실행(NULL)은 모델을 쓴 실행과 다르다.
    assert backtests.has_success(conn, user_a, KEY) is False
    assert backtests.has_success(conn, user_a, replace(KEY, model_hash="00" * 32)) is False
    backtests.record(conn, run_for(user_a), NOW)
    assert backtests.has_success(conn, user_a, KEY) is True


def test_streams_and_null_model_are_stored_as_given() -> None:
    conn = db.connect(":memory:")
    user_a, _ = two_users(conn)
    run_id = backtests.record(conn, run_for(user_a), NOW)

    row = conn.execute(
        "SELECT user_id, streams, model_hash FROM backtest_runs WHERE id = ?", (run_id,)
    ).fetchone()
    assert row == (user_a, STREAMS, None)


def test_failed_runs_do_not_count() -> None:
    conn = db.connect(":memory:")
    user_a, _ = two_users(conn)
    backtests.record(conn, run_for(user_a, status="failed"), NOW)

    assert backtests.has_success(conn, user_a, KEY) is False


def test_another_users_success_does_not_count() -> None:
    conn = db.connect(":memory:")
    user_a, user_b = two_users(conn)
    backtests.record(conn, run_for(user_a), NOW)

    assert backtests.has_success(conn, user_b, KEY) is False


def test_record_returns_distinct_ids_and_keeps_the_bar_range() -> None:
    conn = db.connect(":memory:")
    user_a, _ = two_users(conn)
    first = backtests.record(conn, run_for(user_a), NOW)
    second = backtests.record(
        conn, replace(run_for(user_a), first_bar_at=None, last_bar_at=None, bar_count=0), NOW
    )

    assert first != second
    row = conn.execute(
        "SELECT requested_start, requested_end, first_bar_at, bar_count FROM backtest_runs "
        "WHERE id = ?",
        (second,),
    ).fetchone()
    assert row == ("2026-11-01", "2026-11-20", None, 0)
