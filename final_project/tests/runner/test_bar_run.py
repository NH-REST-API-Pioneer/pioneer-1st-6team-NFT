"""1분봉·일봉 실행: REST로 늦게 오는 봉, 오지 않는 봉(시세 끊김), 데우기, 넣는 순서."""

import asyncio
from datetime import timedelta
from pathlib import Path

from nh_mcp.domain import BarEvent, StrategyEvent, Streams, Target

from .rig import CODE, SYMBOL, T0, Rig, approval, flat_bar, scope

MINUTE: Streams = {SYMBOL: frozenset({"minute"})}


def test_bars_published_two_to_fifteen_seconds_late_all_arrive_in_order(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    delays = [2, 15, 7, 11, 3, 14]
    bars = [flat_bar(T0 + timedelta(minutes=i), f"{780 + i}.00") for i in range(len(delays))]
    for bar, delay in zip(bars, delays, strict=True):
        rig.exchange.publish(SYMBOL, bar, timedelta(seconds=delay))

    async def scenario() -> str:
        run_id = await rig.runner.start(rig.user, approval(MINUTE, scope()), CODE, None)
        await rig.timer.run_until(T0 + timedelta(minutes=6, seconds=30))
        await rig.runner.shutdown()
        return run_id

    run_id = asyncio.run(scenario())

    assert rig.row(run_id).state == "running"
    assert rig.sessions.market_events() == [BarEvent(SYMBOL, "minute", b) for b in bars]


def test_a_bar_still_forming_is_never_fed_only_its_final_version(tmp_path: Path) -> None:
    """NH는 지금 만들어지는 분의 봉도 주고, 그 봉은 마감 약 1초 뒤까지 바뀐다(2026-11-26 장중 측정:
    마지막 바뀜이 마감 0.2~1.2초 뒤). 마감 전의 봉은 넣지 않고, 마감 2초 뒤 확정된 것만 넣는다."""
    rig = Rig(tmp_path)
    finals = [flat_bar(T0 + timedelta(minutes=i), f"{780 + i}.00") for i in range(3)]
    for bar in finals:
        partial = flat_bar(bar.at, "1.00")
        rig.exchange.publish(SYMBOL, partial, timedelta(seconds=-59))
        rig.exchange.publish(SYMBOL, bar, timedelta(seconds=1))

    async def scenario() -> None:
        await rig.runner.start(rig.user, approval(MINUTE, scope()), CODE, None)
        await rig.timer.run_until(T0 + timedelta(minutes=3, seconds=30))
        await rig.runner.shutdown()

    asyncio.run(scenario())

    assert rig.sessions.market_events() == [BarEvent(SYMBOL, "minute", b) for b in finals]


def test_no_new_bar_for_three_session_minutes_stops_with_stale_feed(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    for i in range(2):
        rig.exchange.publish(SYMBOL, flat_bar(T0 + timedelta(minutes=i), "780.00"), timedelta(0))
    seen: dict[str, str] = {}

    async def scenario() -> str:
        run_id = await rig.runner.start(rig.user, approval(MINUTE, scope()), CODE, None)
        # 10:02·10:03 봉이 오지 않은 뒤(두 번 놓침)에는 아직 돈다.
        await rig.timer.run_until(T0 + timedelta(minutes=5, seconds=19))
        seen["before"] = rig.row(run_id).state
        # 10:04 봉의 마감(10:05:20)에 셋째로 놓쳐 멈춘다.
        await rig.timer.run_until(T0 + timedelta(minutes=5, seconds=21))
        await rig.runner.shutdown()
        return run_id

    run_id = asyncio.run(scenario())

    row = rig.row(run_id)
    assert seen["before"] == "running"
    assert (row.state, row.reason) == ("stopped", "stale_feed")
    assert row.stopped_at == T0 + timedelta(minutes=5, seconds=20)
    assert len(rig.sessions.closed) == 1
    assert len(rig.sessions.market_events()) == 2


def test_closing_auction_minutes_without_bars_are_not_a_stale_feed(tmp_path: Path) -> None:
    """15:35~15:44는 종가 단일가라 봉이 없다. 15:45 봉 다음 그날 일봉까지 받는다."""
    start = T0.replace(hour=15, minute=30)
    rig = Rig(tmp_path, start=start)
    for i in range(5):  # 15:30~15:34
        rig.exchange.publish(SYMBOL, flat_bar(start + timedelta(minutes=i), "780.00"), timedelta(0))
    closing = flat_bar(start.replace(minute=45), "781.00")
    rig.exchange.publish(SYMBOL, closing, timedelta(seconds=3))
    day = flat_bar(start.replace(hour=0, minute=0), "781.00", 1000)
    rig.exchange.publish(SYMBOL, day, timedelta(hours=-8), day=True)  # 16:00(+1일 0시 - 8시간)
    streams: Streams = {SYMBOL: frozenset({"minute", "day"})}

    async def scenario() -> str:
        run_id = await rig.runner.start(rig.user, approval(streams, scope()), CODE, None)
        await rig.timer.run_until(start.replace(hour=16, minute=20))
        await rig.runner.shutdown()
        return run_id

    run_id = asyncio.run(scenario())

    assert rig.row(run_id).state == "running"
    fed = rig.sessions.market_events()
    assert [_label(e) for e in fed] == [
        *[f"minute 15:{m}" for m in (30, 31, 32, 33, 34, 45)],
        "day 00:00",
    ]


def _label(event: StrategyEvent) -> str:
    assert isinstance(event, BarEvent)
    return f"{event.interval} {event.bar.at:%H:%M}"


def test_warm_up_feeds_the_last_two_trading_days_and_orders_nothing(tmp_path: Path) -> None:
    """켤 때 지난 두 거래일 봉(오늘 지금까지 끝난 것 포함)을 백테스트 순서로 넣고 출력은 버린다."""
    rig = Rig(tmp_path, strategy=lambda _e, _p: {SYMBOL: Target(1)})
    friday, monday = T0 - timedelta(days=4), T0 - timedelta(days=1)  # 11-27(금), 11-30(월)
    old = T0 - timedelta(days=5)  # 11-26(목): 데우기에 들지 않는다.
    stamps = [old.replace(hour=15, minute=34), friday.replace(hour=9), monday.replace(hour=9)]
    stamps += [T0 - timedelta(minutes=2), T0 - timedelta(minutes=1)]
    for at in stamps:
        rig.exchange.publish(SYMBOL, flat_bar(at, "780.00"), timedelta(0))
    # 지금 만들어지는 봉(10:00 시작)은 아직 끝나지 않아 넣지 않는다.
    rig.exchange.publish(SYMBOL, flat_bar(T0, "780.00"), timedelta(seconds=-60))
    streams: Streams = {SYMBOL: frozenset({"minute"})}

    async def scenario() -> None:
        await rig.runner.start(rig.user, approval(streams, scope()), CODE, None)
        await rig.timer.advance(1)
        await rig.runner.shutdown()

    asyncio.run(scenario())

    assert [e.bar.at for e in rig.sessions.market_events() if isinstance(e, BarEvent)] == stamps[2:]
    assert rig.exchange.order_requests() == []
