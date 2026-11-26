"""실시간 봉: NH REST로 1분봉과 일봉을 받아 실행에 넘긴다.

- 1분봉: 분이 끝나고 MINUTE_AFTER 뒤부터 MINUTE_UNTIL까지 MINUTE_EVERY마다 받는다. 그 분 봉이
  모든 종목에서 오거나 시간이 다 되면, 받은 새 봉을 백테스트와 같은 순서(`feed_order`: 끝난 시각 →
  일봉 먼저 → 종목코드)로 한 번에 넘긴다. 늦게 온 지난 분의 봉도 그 순서대로 들어간다.
- 끊김: 봉이 있어야 하는 분(`expected_minute`)에 그 종목의 새 봉이 STALE_MINUTES번 연달아 없으면
  `Stale`을 넘기고 멈춘다. 평일 공휴일에도 봉이 없어 멈춘다(달력을 모르므로 막는 쪽).
- 일봉: 장이 끝나고 DAY_AFTER 뒤부터 DAY_UNTIL까지 1분마다 받아 그날 봉을 넘긴다.
- 키가 없어지면 `KeyGone`을 넘기고 멈춘다. 그 밖의 조회 실패는 그 시도를 못 받은 것으로 본다.
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from nh_mcp.domain import BarEvent, Candle, Session, UserId
from nh_mcp.runner.timing import (
    DAY_AFTER,
    DAY_UNTIL,
    MINUTE_AFTER,
    MINUTE_EVERY,
    MINUTE_UNTIL,
    STALE_MINUTES,
    Timer,
    at,
    expected_minute,
    next_minute,
    next_open,
    today,
    wanted_minute,
    weekday,
)
from nh_mcp.services.backtest import feed_order
from nh_mcp.services.queries import BrokerRejected, KeyMissing, Unavailable, UnknownSymbol
from nh_mcp.services.runs import RunMarket, SymbolInfo

_ONE_MINUTE = timedelta(minutes=1)
# 1분봉은 그날 것만 받는다. 하루 약 411봉이라 한 번에 온다.
MINUTE_LIMIT = 2000
# 일봉은 지난 며칠 것을 받아 그날 것을 찾는다.
DAY_LOOKBACK = timedelta(days=7)
DAY_LIMIT = 10


@dataclass(frozen=True)
class Bars:
    """넣을 봉들(넣는 순서)."""

    events: list[BarEvent]


@dataclass(frozen=True)
class Stale:
    detail: str


@dataclass(frozen=True)
class KeyGone:
    pass


type Emit = Callable[[Bars | Stale | KeyGone], Awaitable[None]]


class _KeyGone(Exception):
    pass


@dataclass(frozen=True)
class BarPlan:
    """받을 것: 1분봉 종목, 일봉 종목(종목코드 순), 종목마다 이미 넣은 마지막 1분봉의 시작 시각."""

    user_id: UserId
    infos: Mapping[str, SymbolInfo]
    minutes: list[str]
    days: list[str]
    last: dict[str, datetime]


class BarPoller:
    """실행 하나의 봉 받기."""

    def __init__(self, market: RunMarket, timer: Timer, plan: BarPlan, emit: Emit) -> None:
        self._user = plan.user_id
        self._market = market
        self._timer = timer
        self._infos = plan.infos
        self._minutes = plan.minutes
        self._days = plan.days
        self._last = plan.last
        self._emit = emit
        self._missed = dict.fromkeys(plan.minutes, 0)
        self._day_done: datetime | None = None

    async def run(self) -> None:
        try:
            while True:
                end = self._next_end(self._timer.now())
                await self._timer.sleep_until(end + MINUTE_AFTER)
                if self._minutes and await self._minute(end):
                    return
                if self._days:
                    await self._day(end)
        except _KeyGone:
            await self._emit(KeyGone())

    def _next_end(self, now: datetime) -> datetime:
        """다음에 할 일이 있는 분 경계: 1분봉을 받을 분이나 그날 일봉을 받을 때 중 이른 것."""
        end = next_minute(now)
        due = [self._minute_due(end, self._infos[s].session) for s in self._minutes]
        if self._days:
            due.append(self._day_at(end))
        return min(due)

    @staticmethod
    def _minute_due(end: datetime, session: Session) -> datetime:
        start = end - _ONE_MINUTE
        return end if wanted_minute(start, session) else next_open(start, session) + _ONE_MINUTE

    def _day_at(self, end: datetime) -> datetime:
        """end 이후 처음으로 일봉을 받으러 갈 분(아직 받지 않은 평일의 마감 + DAY_AFTER부터)."""
        day = today(end)
        while True:
            done = self._day_done is not None and today(self._day_done) == day
            if weekday(day) and not done:
                closes = max(at(day, self._infos[s].session.closes) for s in self._days)
                if end <= closes + DAY_UNTIL:
                    return max(end, closes + DAY_AFTER)
            day += timedelta(days=1)

    def _day_due(self, end: datetime) -> bool:
        return self._day_at(end) == end

    async def _fetch(
        self, symbol: str, interval: Literal["minute", "day"], day_from: datetime
    ) -> list[Candle] | None:
        """봉을 받는다. 받지 못하면 None(그 시도만 놓친다). 키가 없으면 _KeyGone."""
        period = (today(day_from), today(self._timer.now()))
        limit = MINUTE_LIMIT if interval == "minute" else DAY_LIMIT
        try:
            return await self._market.bars(self._user, symbol, interval, period, limit)
        except KeyMissing:
            raise _KeyGone() from None
        except (Unavailable, BrokerRejected, UnknownSymbol):
            return None

    async def _minute(self, end: datetime) -> bool:
        """분 하나. 끊김으로 멈췄으면 True."""
        start = end - _ONE_MINUTE
        wanted = [s for s in self._minutes if wanted_minute(start, self._infos[s].session)]
        if not wanted:
            return False
        fresh: dict[str, list[Candle]] = {}
        deadline = end + MINUTE_UNTIL
        while True:
            for symbol in wanted:
                if any(c.at >= start for c in fresh.get(symbol, [])):
                    continue
                bars = await self._fetch(symbol, "minute", start)
                if bars is None:
                    continue
                now = self._timer.now()
                fresh[symbol] = [
                    c for c in bars if c.at > self._last[symbol] and c.at + _ONE_MINUTE <= now
                ]
            arrived = all(any(c.at >= start for c in fresh.get(s, [])) for s in wanted)
            now = self._timer.now()
            if arrived or now >= deadline:
                break
            await self._timer.sleep_until(min(now + MINUTE_EVERY, deadline))
        events = sorted(
            (BarEvent(symbol, "minute", c) for symbol, bars in fresh.items() for c in bars),
            key=feed_order,
        )
        for event in events:
            self._last[event.symbol] = max(self._last[event.symbol], event.bar.at)
        if events:
            await self._emit(Bars(events))
        return await self._count_misses(start, wanted, fresh)

    async def _count_misses(
        self, start: datetime, wanted: list[str], fresh: dict[str, list[Candle]]
    ) -> bool:
        for symbol in wanted:
            if fresh.get(symbol):
                self._missed[symbol] = 0
            elif expected_minute(start, self._infos[symbol].session):
                self._missed[symbol] += 1
                if self._missed[symbol] >= STALE_MINUTES:
                    await self._emit(
                        Stale(f"{symbol} 1분봉이 장중 {STALE_MINUTES}분 연달아 오지 않았습니다.")
                    )
                    return True
        return False

    async def _day(self, end: datetime) -> None:
        """장이 끝난 뒤 그날 일봉. 모든 종목 봉이 오거나 DAY_UNTIL이 지나면 한 번 넘긴다."""
        if not self._day_due(end):
            return
        day = today(end)
        closes = max(at(day, self._infos[s].session.closes) for s in self._days)
        found: dict[str, Candle] = {}
        for symbol in self._days:
            bars = await self._fetch(symbol, "day", end - DAY_LOOKBACK)
            match = [c for c in bars or [] if today(c.at) == day]
            if match:
                found[symbol] = match[-1]
        if len(found) < len(self._days) and end < closes + DAY_UNTIL:
            return
        self._day_done = end
        events = sorted((BarEvent(s, "day", c) for s, c in found.items()), key=feed_order)
        if events:
            await self._emit(Bars(events))
