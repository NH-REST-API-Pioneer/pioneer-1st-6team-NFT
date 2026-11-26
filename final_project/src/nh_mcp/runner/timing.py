"""실행기의 시간: 기다리는 길(`Timer`), 정해 둔 간격, 거래 시간 계산.

시간은 모두 `Timer`로만 읽고 기다린다. 테스트는 가짜 시계로 몇 분·몇 시간을 바로 건너뛴다.
"""

import asyncio
from datetime import date, datetime, time, timedelta
from typing import Protocol

from nh_mcp.domain import KST, Clock, Session

# 1분봉: 그 분이 끝나고 3초 뒤부터 REST로 받고, 20초가 될 때까지 2초마다 다시 본다. 모의 분봉은
# 마감 뒤 0.2~1.2초에 마지막으로 바뀌었다(2026-11-26 10분 실측). 장중에 새 봉이 3분 연속 없으면
# 시세 끊김이다.
MINUTE_AFTER = timedelta(seconds=3)
MINUTE_UNTIL = timedelta(seconds=20)
MINUTE_EVERY = timedelta(seconds=2)
STALE_MINUTES = 3
# 일봉: 장이 끝나고(종가 단일가 끝) 2분 뒤부터 15분 뒤까지 1분마다 본다. 15:45 1분봉(마감 2~20초
# 뒤에 받음)보다 늦게 받아야 백테스트처럼 그날 1분봉 다음에 들어간다.
DAY_AFTER = timedelta(minutes=2)
DAY_UNTIL = timedelta(minutes=15)
# 체결·호가가 장중에 3분 넘게 끊기면 시세 끊김이다.
STALE_GAP = timedelta(minutes=3)
# 걸린 주문이 있는 동안 주문 내역을 2초마다 본다(사람 결정 2026-11-25).
FILL_POLL = timedelta(seconds=2)
# 새 목표 전에 취소한 주문이 끝났는지(취소·체결) 이만큼까지 기다린다. 못 보면 모르는 주문이다.
SETTLE_LIMIT = timedelta(seconds=20)
# 접수된 주문이 이만큼 지나도 주문 내역에 없으면 모르는 주문이다.
MISSING_LIMIT = timedelta(seconds=30)
# 주문 가능 시간 밖에서 미룬 목표는 다음 개장 1초 뒤에 다시 낸다.
OPEN_DELAY = timedelta(seconds=1)
# 마지막 이벤트 시각은 이만큼에 한 번만 적는다(이벤트마다 DB에 쓰지 않게).
TOUCH_EVERY = timedelta(seconds=10)
# 한 번에 기다리는 길이. 벽시계가 바뀌어도 이만큼마다 다시 잰다.
_SLEEP_CHUNK = 60.0

SATURDAY = 5


class Timer(Protocol):
    def now(self) -> datetime: ...

    async def sleep_until(self, when: datetime) -> None: ...


class WallTimer:
    """실제 시계. 기다릴 때는 남은 시간을 다시 재며 조금씩 잔다."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    def now(self) -> datetime:
        return self._clock()

    async def sleep_until(self, when: datetime) -> None:
        while (left := (when - self._clock()).total_seconds()) > 0:
            await asyncio.sleep(min(left, _SLEEP_CHUNK))


def kst(moment: datetime) -> datetime:
    return moment.astimezone(KST)


def today(moment: datetime) -> date:
    return kst(moment).date()


def at(day: date, clock_time: time) -> datetime:
    return datetime.combine(day, clock_time, KST)


def weekday(day: date) -> bool:
    return day.weekday() < SATURDAY


def in_session(moment: datetime, session: Session) -> bool:
    """주문 가능 시간(평일 시가 단일가 끝 ~ 종가 단일가 끝) 안인가."""
    local = kst(moment)
    return weekday(local.date()) and session.opens <= local.time() < session.closes


def expected_minute(start: datetime, session: Session) -> bool:
    """이 분에 시작하는 1분봉이 있어야 하는가: 평일 개장부터 종가 단일가 전까지.

    종가 단일가 구간(15:35~15:44)에는 봉이 없고, 15:45 봉(종가 체결)은 받되 없어도 끊김이 아니다.
    """
    local = kst(start)
    return weekday(local.date()) and session.opens <= local.time() < session.closing_auction


def wanted_minute(start: datetime, session: Session) -> bool:
    """이 분의 1분봉을 받으러 가는가: 평일 개장부터 장 마감 분(15:45)까지."""
    local = kst(start)
    return weekday(local.date()) and session.opens <= local.time() <= session.closes


def next_open(moment: datetime, session: Session) -> datetime:
    """moment 뒤의 첫 개장 시각(평일 시가 단일가 끝)."""
    day = today(moment)
    while True:
        opens = at(day, session.opens)
        if weekday(day) and opens > moment:
            return opens
        day += timedelta(days=1)


def session_start(moment: datetime, session: Session) -> datetime:
    """그날의 개장 시각."""
    return at(today(moment), session.opens)


def minute_floor(moment: datetime) -> datetime:
    return moment.replace(second=0, microsecond=0)


def next_minute(moment: datetime) -> datetime:
    """moment 뒤의 첫 분 경계(moment가 경계면 그다음 분)."""
    return minute_floor(moment) + timedelta(minutes=1)
