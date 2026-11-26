"""스스로 멈추는 조건마다 실제로 멈추고, 멈출 때 세션을 닫고 구독을 풀고 걸린 주문을 취소한다."""

import asyncio
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from nh_mcp.domain import Output, SessionFailed, StrategyEvent, Target, TickEvent
from nh_mcp.realtime.feed import FeedGap
from nh_mcp.store import ticks
from nh_mcp.store.ticks import Sub

from .rig import SYMBOL, T0, Rig, held, scope
from .scripts import TICK, after_minute, by_minute, publish_minutes, start

PRICE = Decimal("780.00")
TICKS = Sub(SYMBOL, "tick")


def rig_at(tmp_path: Path, targets: dict[int, Target], price: Decimal = PRICE) -> Rig:
    """봉마다 정해 둔 목표를 내는 전략. 시장가는 그 자리에서 price에 체결된다."""
    rig = Rig(tmp_path, by_minute(targets))
    rig.exchange.market_price = lambda _symbol, _side: price
    return rig


def state(rig: Rig, run_id: str) -> tuple[str, str | None]:
    row = rig.row(run_id)
    return row.state, row.reason


def limit_on_tick(event: StrategyEvent, _positions: dict[str, int]) -> Output:
    return {SYMBOL: Target(1, Decimal("779.00"))} if isinstance(event, TickEvent) else None


def trade(rig: Rig, seq: int = 1) -> ticks.Trade:
    return ticks.Trade(seq, rig.timer.now(), PRICE, 1, None, None, 1, seq)


def test_user_stop_cancels_resting_order_closes_session_and_unsubscribes(tmp_path: Path) -> None:
    rig = Rig(tmp_path, limit_on_tick)

    async def scenario() -> str:
        run_id = await start(rig, TICK)
        [handle] = rig.recorder.handles
        await rig.timer.advance(1)
        handle.deliver(TICKS, trade(rig))
        await rig.timer.advance(1)
        assert await rig.runner.stop(rig.user, run_id) is True
        await rig.runner.shutdown()
        return run_id

    run_id = asyncio.run(scenario())

    assert state(rig, run_id) == ("stopped", "user")
    [placed] = rig.exchange.placed
    assert rig.exchange.calls("cancel") == [
        {"sym": SYMBOL, "trd_div": "1", "ord_qty": "1", "org_ord_no": placed.order_no}
    ]
    assert len(rig.sessions.closed) == 1
    assert rig.recorder.removed == rig.recorder.handles
    assert rig.row(run_id).session_id is None
    assert rig.runner.active(rig.user) == []


def test_expiry_stops_the_run_at_the_expiry_time(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    until = T0 + timedelta(seconds=90)

    async def scenario() -> str:
        run_id = await start(rig, run_scope=scope(expires_at=until))
        await rig.timer.run_until(T0 + timedelta(minutes=3))
        await rig.runner.shutdown()
        return run_id

    run_id = asyncio.run(scenario())

    assert state(rig, run_id) == ("stopped", "expired")
    assert rig.row(run_id).stopped_at == until


def test_daily_orders_stop_after_the_last_allowed_order(tmp_path: Path) -> None:
    rig = rig_at(tmp_path, {0: Target(1), 1: Target(0), 2: Target(1)})
    publish_minutes(rig, ["780.00"] * 3)

    async def scenario() -> str:
        run_id = await start(rig, run_scope=scope(daily_orders=1))
        await rig.timer.run_until(after_minute(2))
        await rig.runner.shutdown()
        return run_id

    run_id = asyncio.run(scenario())

    assert state(rig, run_id) == ("stopped", "daily_orders")
    # 승인한 하루 1번 넘게는 NH로 나가지 않는다.
    assert len(rig.exchange.order_requests()) == 1


def test_daily_loss_with_hold_stops_and_keeps_the_position(tmp_path: Path) -> None:
    # 780에 1계약 산 뒤 775로 떨어지면 (775 - 780) x 250000 = -1,250,000원. 한도 100만 원.
    rig = rig_at(tmp_path, {0: Target(1)})
    publish_minutes(rig, ["780.00", "775.00", "775.00"])

    async def scenario() -> str:
        run_id = await start(rig, run_scope=scope(on_loss="hold"))
        await rig.timer.run_until(after_minute(2))
        await rig.runner.shutdown()
        return run_id

    run_id = asyncio.run(scenario())

    assert state(rig, run_id) == ("stopped", "daily_loss")
    assert len(rig.exchange.order_requests()) == 1
    assert held(rig) == 1


def test_daily_loss_with_liquidate_sends_a_closing_market_order_then_stops(tmp_path: Path) -> None:
    rig = rig_at(tmp_path, {0: Target(1)})
    publish_minutes(rig, ["780.00", "775.00", "775.00"])

    async def scenario() -> str:
        run_id = await start(rig, run_scope=scope(on_loss="liquidate"))
        await rig.timer.run_until(after_minute(2))
        await rig.runner.shutdown()
        return run_id

    run_id = asyncio.run(scenario())

    assert state(rig, run_id) == ("stopped", "daily_loss")
    closing = {
        "sym": SYMBOL,
        "trd_div": "2",
        "ord_tp_cd": "2",
        "ord_qty": "1",
        "ord_pric": "0",
        "exec_cnd_cd": "1",
    }
    assert rig.exchange.order_requests()[1:] == [closing]
    assert held(rig) == 0


def test_unknown_order_stops_at_once_and_is_never_resent(tmp_path: Path) -> None:
    rig = rig_at(tmp_path, {0: Target(1), 1: Target(1), 2: Target(1)})
    rig.exchange.silent_orders = True
    publish_minutes(rig, ["780.00"] * 3)

    async def scenario() -> str:
        run_id = await start(rig)
        await rig.timer.run_until(after_minute(2))
        await rig.runner.shutdown()
        return run_id

    run_id = asyncio.run(scenario())

    assert state(rig, run_id) == ("stopped", "unknown_order")
    # 응답을 못 받은 주문은 다시 보내지 않는다(불변식 5).
    assert len(rig.exchange.order_requests()) == 1


def test_three_rejections_in_a_row_stop_the_run(tmp_path: Path) -> None:
    rig = rig_at(tmp_path, {i: Target(1) for i in range(5)})
    rig.exchange.reject_orders = True
    publish_minutes(rig, ["780.00"] * 5)

    async def scenario() -> str:
        run_id = await start(rig)
        await rig.timer.run_until(after_minute(4))
        await rig.runner.shutdown()
        return run_id

    run_id = asyncio.run(scenario())

    assert state(rig, run_id) == ("stopped", "rejections")
    assert len(rig.exchange.order_requests()) == 3


def test_key_removal_stops_the_run_and_cancels_with_the_key_still_there(tmp_path: Path) -> None:
    rig = Rig(tmp_path, limit_on_tick)

    async def scenario() -> str:
        run_id = await start(rig, TICK)
        [handle] = rig.recorder.handles
        await rig.timer.advance(1)
        handle.deliver(TICKS, trade(rig))
        await rig.timer.advance(1)
        await rig.runner.key_removed(rig.user)
        await rig.runner.shutdown()
        return run_id

    run_id = asyncio.run(scenario())

    assert state(rig, run_id) == ("stopped", "key_removed")
    assert len(rig.exchange.calls("cancel")) == 1


def test_a_key_deleted_mid_run_stops_with_key_removed(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    publish_minutes(rig, ["780.00"] * 3)

    async def scenario() -> str:
        run_id = await start(rig)
        await rig.timer.run_until(after_minute(0))
        rig.keys.delete_all(rig.user)
        await rig.timer.run_until(after_minute(2))
        await rig.runner.shutdown()
        return run_id

    run_id = asyncio.run(scenario())

    assert state(rig, run_id) == ("stopped", "key_removed")


def test_sandbox_death_stops_with_sandbox_failed(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    publish_minutes(rig, ["780.00"] * 3)

    async def scenario() -> str:
        run_id = await start(rig)
        await rig.timer.run_until(after_minute(0))
        rig.sessions.gone = True
        await rig.timer.run_until(after_minute(2))
        await rig.runner.shutdown()
        return run_id

    run_id = asyncio.run(scenario())

    assert state(rig, run_id) == ("stopped", "sandbox_failed")


def test_a_resource_limit_in_the_container_is_sandbox_failed(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.sessions.fail_on = lambda _e: SessionFailed("memory", "")
    publish_minutes(rig, ["780.00"])

    async def scenario() -> str:
        run_id = await start(rig)
        await rig.timer.run_until(after_minute(0))
        await rig.runner.shutdown()
        return run_id

    run_id = asyncio.run(scenario())

    assert state(rig, run_id) == ("stopped", "sandbox_failed")


def test_invalid_output_stops_and_no_order_reaches_nh(tmp_path: Path) -> None:
    # 승인한 최대 포지션은 1계약인데 2계약을 원한다. 실행기 검사가 막고 NH로는 아무것도 가지 않는다.
    rig = rig_at(tmp_path, {0: Target(2)})
    publish_minutes(rig, ["780.00"] * 2)

    async def scenario() -> str:
        run_id = await start(rig, run_scope=scope(max_position=1))
        await rig.timer.run_until(after_minute(1))
        await rig.runner.shutdown()
        return run_id

    run_id = asyncio.run(scenario())

    assert state(rig, run_id) == ("stopped", "invalid_output")
    assert rig.exchange.order_requests() == []


def test_slow_consumer_stops_with_stale_feed(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    async def scenario() -> str:
        run_id = await start(rig, TICK)
        [handle] = rig.recorder.handles
        handle.end("slow_consumer")
        await rig.timer.advance(1)
        await rig.runner.shutdown()
        return run_id

    run_id = asyncio.run(scenario())

    assert state(rig, run_id) == ("stopped", "stale_feed")


def test_a_tick_gap_over_three_session_minutes_stops_with_stale_feed(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    seen: dict[str, str] = {}

    async def scenario() -> str:
        run_id = await start(rig, TICK)
        [handle] = rig.recorder.handles
        handle.push(FeedGap(TICKS, rig.timer.now(), "disconnect"))
        await rig.timer.advance(170)
        seen["before"] = rig.row(run_id).state
        await rig.timer.advance(20)
        await rig.runner.shutdown()
        return run_id

    run_id = asyncio.run(scenario())

    assert seen["before"] == "running"
    assert state(rig, run_id) == ("stopped", "stale_feed")
    assert rig.row(run_id).stopped_at == T0 + timedelta(minutes=3)


def test_a_tick_gap_after_the_close_does_not_stop(tmp_path: Path) -> None:
    late = T0.replace(hour=15, minute=44)
    rig = Rig(tmp_path, start=late)

    async def scenario() -> str:
        run_id = await start(rig, TICK)
        [handle] = rig.recorder.handles
        handle.push(FeedGap(TICKS, late, "disconnect"))
        await rig.timer.run_until(late.replace(hour=20))
        await rig.runner.shutdown()
        return run_id

    run_id = asyncio.run(scenario())

    assert state(rig, run_id) == ("running", None)


def test_server_stop_on_the_feed_leaves_the_state_for_the_next_boot(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    async def scenario() -> str:
        run_id = await start(rig, TICK)
        [handle] = rig.recorder.handles
        handle.end("server_stop")
        await rig.timer.advance(1)
        await rig.runner.shutdown()
        return run_id

    run_id = asyncio.run(scenario())

    assert state(rig, run_id) == ("running", None)
    assert rig.sessions.closed == []
