"""실행기 테스트가 같이 쓰는 대본: 봉마다 목표를 내는 전략, 봉 올리기, 켜고 돌리기."""

from collections.abc import Mapping
from datetime import datetime, timedelta

from nh_mcp.domain import BarEvent, Output, StrategyEvent, StrategyScope, Streams, Target

from .rig import CODE, SYMBOL, T0, Rig, Strategy, approval, flat_bar, scope

MINUTE: Streams = {SYMBOL: frozenset({"minute"})}
TICK: Streams = {SYMBOL: frozenset({"tick"})}


def by_minute(targets: Mapping[int, Target], start: datetime = T0) -> Strategy:
    """start부터 몇 번째 1분봉인지에 따라 정해 둔 목표를 낸다. 체결·다른 이벤트에는 None."""

    def strategy(event: StrategyEvent, _positions: dict[str, int]) -> Output:
        if not isinstance(event, BarEvent) or event.interval != "minute":
            return None
        index = int((event.bar.at - start).total_seconds() // 60)
        target = targets.get(index)
        return None if target is None else {SYMBOL: target}

    return strategy


def publish_minutes(
    rig: Rig, closes: list[str], start: datetime = T0, delay: timedelta = timedelta(seconds=2)
) -> None:
    for i, close in enumerate(closes):
        rig.exchange.publish(SYMBOL, flat_bar(start + timedelta(minutes=i), close), delay)


async def start(rig: Rig, streams: Streams = MINUTE, run_scope: StrategyScope | None = None) -> str:
    return await rig.runner.start(rig.user, approval(streams, run_scope or scope()), CODE, None)


def after_minute(index: int, start: datetime = T0) -> datetime:
    """index번째 1분봉을 받아 처리하고도 남는 시각(그 봉 마감 30초 뒤)."""
    return start + timedelta(minutes=index + 1, seconds=30)
