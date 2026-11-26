"""목표가 주문이 되는 방식: 걸린 주문 먼저 취소, 장 밖이면 다음 개장, 1회 수량씩, 제안 모드,
사용자 분리."""

import asyncio
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from nh_mcp.domain import BarEvent, Output, StrategyEvent, Target
from nh_mcp.store import strategy_runs

from .rig import API, CODE, KEY_A, KEY_B, SYMBOL, T0, Rig, approval, flat_bar, held, scope
from .scripts import after_minute, by_minute, publish_minutes, start

PRICE = Decimal("780.00")


def test_a_new_target_cancels_the_resting_order_and_waits_before_placing(tmp_path: Path) -> None:
    rig = Rig(
        tmp_path, by_minute({0: Target(1, Decimal("779.00")), 1: Target(1, Decimal("779.50"))})
    )
    publish_minutes(rig, ["780.00"] * 2)

    async def scenario() -> None:
        await start(rig)
        await rig.timer.run_until(after_minute(1))
        await rig.runner.shutdown()

    asyncio.run(scenario())

    first, second = rig.exchange.placed
    assert (first.price, second.price) == (Decimal("779.00"), Decimal("779.50"))
    trail = [
        path.removeprefix(API)
        for _, _, path, _ in rig.exchange.requests
        if path.removeprefix(API) in ("order", "cancel", "order-history")
    ]
    # 첫 주문 → (둘째 봉) 취소 → 주문 내역으로 끝난 것을 본 뒤 → 새 주문.
    cancel = trail.index("cancel")
    assert trail[: cancel + 1].count("order") == 1
    assert "order-history" in trail[cancel + 1 : trail.index("order", cancel)]
    assert rig.exchange.calls("cancel")[0]["org_ord_no"] == first.order_no
    assert first.cancelled and not second.cancelled


def test_a_target_out_of_hours_waits_and_is_placed_at_the_next_open(tmp_path: Path) -> None:
    """일봉 신호는 장이 끝난 뒤에 나온다. 다음 거래일 개장(08:45) 1초 뒤에 낸다."""
    after_close = T0.replace(hour=15, minute=50)

    def on_day(event: StrategyEvent, _positions: dict[str, int]) -> Output:
        return {SYMBOL: Target(1)} if isinstance(event, BarEvent) else None

    rig = Rig(tmp_path, on_day, start=after_close)
    rig.exchange.market_price = lambda _symbol, _side: PRICE
    day = flat_bar(T0.replace(hour=0), "781.00", 1000)
    rig.exchange.publish(SYMBOL, day, timedelta(hours=-8, minutes=-10), day=True)  # 15:50
    tomorrow_open = (T0 + timedelta(days=1)).replace(hour=8, minute=45)

    async def scenario() -> str:
        run_id = await start(rig, {SYMBOL: frozenset({"day"})})
        await rig.timer.run_until(tomorrow_open - timedelta(seconds=1))
        assert rig.exchange.order_requests() == []
        await rig.timer.run_until(tomorrow_open + timedelta(seconds=5))
        await rig.runner.shutdown()
        return run_id

    run_id = asyncio.run(scenario())

    [(sent_at, _, _, _)] = [r for r in rig.exchange.requests if r[2] == API + "order"]
    assert sent_at == tomorrow_open + timedelta(seconds=1)
    assert held(rig) == 1
    assert rig.row(run_id).state == "running"


def test_a_target_beyond_one_order_quantity_continues_on_the_next_event(tmp_path: Path) -> None:
    rig = Rig(tmp_path, by_minute({0: Target(2)}))
    rig.exchange.market_price = lambda _symbol, _side: PRICE
    publish_minutes(rig, ["780.00"] * 3)

    async def scenario() -> None:
        await start(rig, run_scope=scope(quantity=1, max_position=2))
        await rig.timer.run_until(after_minute(2))
        await rig.runner.shutdown()

    asyncio.run(scenario())

    # 1회 수량 1계약씩: 첫 주문의 체결 이벤트를 넣은 뒤에 남은 1계약을 낸다.
    assert [o["ord_qty"] for o in rig.exchange.order_requests()] == ["1", "1"]
    assert [(f.side, f.qty) for f in rig.sessions.fills()] == [("buy", 1), ("buy", 1)]
    kinds = [type(e).__name__ for e in rig.sessions.fed]
    assert kinds[:3] == ["BarEvent", "FillEvent", "FillEvent"]
    assert held(rig) == 2


def test_suggest_mode_records_the_latest_target_per_symbol_and_places_nothing(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, by_minute({0: Target(1), 1: Target(-1, Decimal("781.00"))}))
    publish_minutes(rig, ["780.00"] * 2)

    async def scenario() -> str:
        run_id = await start(rig, run_scope=scope(mode="suggest"))
        await rig.timer.run_until(after_minute(1))
        await rig.runner.shutdown()
        return run_id

    run_id = asyncio.run(scenario())

    assert rig.exchange.order_requests() == []
    assert rig.exchange.calls("cancel") == []
    [(symbol, latest)] = strategy_runs.suggestions(rig.conn, rig.user, run_id).items()
    assert symbol == SYMBOL
    assert latest.detail == {SYMBOL: {"position": -1, "price": "781.00"}}


def test_a_run_reads_and_orders_only_its_own_users_account(tmp_path: Path) -> None:
    rig = Rig(tmp_path, by_minute({0: Target(1), 1: Target(0)}))
    rig.exchange.market_price = lambda _symbol, _side: PRICE
    # 다른 사용자 B는 같은 종목에 포지션이 있다. A의 켜기를 막지 않고, A가 B 계좌를 건드리지 않는다.
    rig.exchange.positions[(KEY_B, SYMBOL)] = 2
    publish_minutes(rig, ["780.00"] * 2)

    async def scenario() -> str:
        run_id = await start(rig)
        await rig.timer.run_until(after_minute(1))
        await rig.runner.shutdown()
        return run_id

    run_id = asyncio.run(scenario())

    assert rig.row(run_id).state == "running"
    assert len(rig.exchange.order_requests(KEY_A)) == 2
    assert [r for r in rig.exchange.requests if r[1] == KEY_B] == []
    assert rig.exchange.positions[(KEY_B, SYMBOL)] == 2
    assert strategy_runs.get(rig.conn, rig.other, run_id) is None
    assert strategy_runs.list_for(rig.conn, rig.other) == []


def test_another_user_cannot_stop_my_run(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    async def scenario() -> tuple[bool, str]:
        run_id = await rig.runner.start(
            rig.user, approval({SYMBOL: frozenset({"minute"})}, scope()), CODE, None
        )
        refused = await rig.runner.stop(rig.other, run_id)
        await rig.runner.shutdown()
        return refused, run_id

    refused, run_id = asyncio.run(scenario())

    assert refused is False
    assert rig.row(run_id).state == "running"
