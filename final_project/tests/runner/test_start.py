"""켜기: 승인받은 것만, 실행할 종목이 비어 있을 때만, 계좌를 알 때만 켠다.

켜다 실패하면 멈춤으로 남는다."""

import asyncio
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from nh_mcp.domain import BarEvent, SessionFailed, Target
from nh_mcp.services.runs import StartRefused
from nh_mcp.store import strategy_runs

from .rig import API, CODE, KEY_A, SYMBOL, T0, Placed, Rig, approval, flat_bar, held, scope
from .scripts import MINUTE


def refused(rig: Rig, payload: str | None = None, code: str = CODE) -> StartRefused:
    async def scenario() -> None:
        await rig.runner.start(rig.user, payload or approval(MINUTE, scope()), code, None)

    with pytest.raises(StartRefused) as caught:
        asyncio.run(scenario())
    return caught.value


def nothing_started(rig: Rig) -> bool:
    return strategy_runs.list_for(rig.conn, rig.user) == [] and rig.sessions.opened == []


def test_refuses_when_the_account_holds_a_position_in_the_symbol(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.exchange.positions[(KEY_A, SYMBOL)] = 1

    error = refused(rig)

    assert error.rule == "account_not_flat"
    assert nothing_started(rig)
    assert rig.exchange.order_requests() == []


def test_refuses_when_the_account_has_an_open_order_in_the_symbol(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    waiting = Placed(KEY_A, "0000000001", SYMBOL, "buy", "limit", 1, Decimal("770.00"), T0)
    rig.exchange.placed.append(waiting)

    error = refused(rig)

    assert error.rule == "account_not_flat"
    assert nothing_started(rig)


def test_refuses_when_the_account_cannot_be_read(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.exchange.down_paths.add(API + "open-interest")

    error = refused(rig)

    assert error.rule == "account_unknown"
    assert nothing_started(rig)


def test_refuses_code_or_model_other_than_the_approved_one(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    assert refused(rig, code=CODE + "\n# 바꿈\n").rule == "not_approved_code"

    async def with_model() -> None:
        await rig.runner.start(rig.user, approval(MINUTE, scope()), CODE, b"model")

    with pytest.raises(StartRefused) as caught:
        asyncio.run(with_model())
    assert caught.value.rule == "not_approved_model"
    assert refused(rig, payload='{"market": "domestic"}').rule == "invalid_approval"
    assert nothing_started(rig)


def test_a_strategy_that_fails_to_open_is_left_stopped(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.sessions.open_failure = SessionFailed("invalid_code", "on_event 함수가 없습니다")

    error = refused(rig)

    assert error.rule == "sandbox_failed"
    [row] = strategy_runs.list_for(rig.conn, rig.user)
    assert (row.state, row.reason) == ("stopped", "sandbox_failed")
    assert rig.sessions.closed == []


def test_invalid_output_during_warm_up_stops_the_run_and_closes_the_session(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, strategy=lambda _e, _p: {SYMBOL: Target(5)})
    rig.exchange.publish(SYMBOL, flat_bar(T0 - timedelta(minutes=1), "780.00"), timedelta(0))

    error = refused(rig)

    assert error.rule == "invalid_output"
    [row] = strategy_runs.list_for(rig.conn, rig.user)
    assert (row.state, row.reason, row.session_id) == ("stopped", "invalid_output", None)
    assert len(rig.sessions.closed) == 1
    assert [type(e) for e in rig.sessions.fed] == [BarEvent]
    assert rig.exchange.order_requests() == []
    assert held(rig) == 0
