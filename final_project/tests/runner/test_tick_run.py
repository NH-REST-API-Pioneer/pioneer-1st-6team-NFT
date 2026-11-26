"""틱 실행: 저장해 둔 체결을 가짜 수신기로 흘리면 기대한 목표와 NH 주문이 나온다."""

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from nh_mcp.domain import Output, Side, StrategyEvent, Target, TickEvent
from nh_mcp.realtime.feed import recorded_events
from nh_mcp.store import ticks
from nh_mcp.store.ticks import Sub

from .rig import CODE, SYMBOL, T0, Rig, approval, held, scope

TICKS = Sub(SYMBOL, "tick")
# 저장해 둔 체결(받은 순서). 785.00 이상이면 1계약, 784.00 이하면 정리하는 전략에 흘린다.
PRICES = ["784.50", "784.80", "785.00", "785.10", "784.60", "784.00", "783.90"]


def trades(start: datetime) -> list[ticks.Trade]:
    return [
        ticks.Trade(i + 1, start + timedelta(seconds=3 * i), Decimal(p), 1, None, None, 1, i + 1)
        for i, p in enumerate(PRICES)
    ]


def threshold(event: StrategyEvent, _positions: dict[str, int]) -> Output:
    if not isinstance(event, TickEvent):
        return None
    if event.price >= Decimal("785.00"):
        return {SYMBOL: Target(1)}
    if event.price <= Decimal("784.00"):
        return {SYMBOL: Target(0)}
    return None


def market(side: Side) -> dict[str, str]:
    return {
        "sym": SYMBOL,
        "trd_div": "1" if side == "buy" else "2",
        "ord_tp_cd": "2",
        "ord_qty": "1",
        "ord_pric": "0",
        "exec_cnd_cd": "1",
    }


def test_recorded_trades_become_the_expected_targets_and_orders(tmp_path: Path) -> None:
    rig = Rig(tmp_path, threshold)
    last: list[Decimal] = [Decimal("784.50")]
    rig.exchange.market_price = lambda _symbol, _side: last[0]
    rows = trades(T0 + timedelta(seconds=10))

    async def scenario() -> str:
        run_id = await rig.runner.start(
            rig.user, approval({SYMBOL: frozenset({"tick"})}, scope()), CODE, None
        )
        [handle] = rig.recorder.handles
        for row in rows:
            await rig.timer.run_until(row.at)
            last[0] = row.price
            handle.deliver(TICKS, row)
        await rig.timer.advance(5)
        await rig.runner.shutdown()
        return run_id

    run_id = asyncio.run(scenario())

    assert rig.row(run_id).state == "running"
    # 실행기가 넣은 시장 이벤트는 백테스트가 같은 행으로 만드는 이벤트와 같다.
    assert rig.sessions.market_events() == recorded_events([(TICKS, r) for r in rows])
    # 785.00에서 매수(시장가), 784.00에서 정리. NH가 받은 주문 그대로.
    assert rig.exchange.order_requests() == [market("buy"), market("sell")]
    assert [(f.side, f.qty, f.price) for f in rig.sessions.fills()] == [
        ("buy", 1, Decimal("785.00")),
        ("sell", 1, Decimal("784.00")),
    ]
    # 체결은 그것을 낳은 주문 뒤, 다음 시장 이벤트보다 먼저 들어간다.
    kinds = [type(e).__name__ for e in rig.sessions.fed]
    assert kinds == ["TickEvent"] * 3 + ["FillEvent"] + ["TickEvent"] * 3 + [
        "FillEvent",
        "TickEvent",
    ]
    assert held(rig) == 0
