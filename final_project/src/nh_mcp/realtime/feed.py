"""받은 체결·호가를 전략 이벤트로 바꾸고, 전략 구독마다 나눠 준다.

- 행 → 이벤트는 `Timeline` 하나로 한다. 백테스트(저장한 행)와 실시간(받은 행)이 같은 것을 써서, 같은
  행이면 같은 이벤트가 나온다.
- 구독(`Subscription`)은 사용자 한 명의 세션 안에 있다. 그 사용자가 받은 것만, 그 구독이 원한
  (종목, 흐름)만 들어간다(안전 불변식 8).
- 구독마다 쌓아 둘 수 있는 것은 MAX_PENDING개다. 넘으면 그 구독만 끊고
  (`FeedClosed("slow_consumer")`) 수신기와 다른 구독은 기다리지 않는다.
- 전략 구독은 저장하지 않는다. 서버를 다시 띄우면 없다(전략은 일시정지로 돌아온다).
"""

import asyncio
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from nh_mcp.domain import BookEvent, TickEvent, UserId
from nh_mcp.store import ticks
from nh_mcp.store.ticks import Row, Sub

log = logging.getLogger(__name__)

# 구독 하나에 쌓아 둘 수 있는 항목 수. 바쁜 종목 하나(체결·호가 합쳐 초당 수십 개)로 10초 남짓이다.
# 그만큼 밀린 전략은 지난 가격으로 판단하고 있으니 끊는다(실행기는 stale_feed로 멈춘다).
MAX_PENDING = 1000

type Recorded = TickEvent | BookEvent
type FeedGapReason = Literal["disconnect", "subscribe_failed"]
type CloseReason = Literal["slow_consumer", "unsubscribed", "server_stop"]


@dataclass(frozen=True)
class FeedGap:
    """이 (종목, 흐름)이 start부터 들어오지 않는다. 끊긴 동안의 것은 다시 오지 않는다."""

    sub: Sub
    start: datetime
    reason: FeedGapReason


@dataclass(frozen=True)
class FeedResumed:
    """이 (종목, 흐름)이 at부터 들어온다. 구독을 시작할 때도 받는 중이면 먼저 온다."""

    sub: Sub
    at: datetime


@dataclass(frozen=True)
class FeedClosed:
    """구독이 끝났다. 언제나 마지막 항목이다."""

    reason: CloseReason


type FeedItem = Recorded | FeedGap | FeedResumed | FeedClosed


def _levels(levels: tuple[ticks.Level, ...]) -> tuple[tuple[Decimal, int], ...]:
    """최우선부터 잔량이 있는 단계까지. NH는 빈 단계를 가격·잔량 0으로 준다(모의는 5단계만 찬다)."""
    out: list[tuple[Decimal, int]] = []
    for level in levels:
        if level.price is None or not level.qty:
            break
        out.append((level.price, level.qty))
    return tuple(out)


class Timeline:
    """받은 순서대로 행을 이벤트로 바꾼다.

    at은 거래소 시각(초)에 받은 날짜를 붙인 것이다. 앞 이벤트보다 이르면 앞 이벤트의 시각으로 둔다
    (종목·흐름마다 거래소 시각이 어긋나 받은 순서와 다를 수 있다). 그래서 at은 받은 순서대로 뒤로
    가지 않고, 판단한 뒤 처음 받은 그 종목의 체결에서 체결된다. 앞 이벤트는 이 Timeline이 바꾼 것만
    본다: 백테스트는 그 실행의 흐름, 실시간은 그 구독의 흐름이다.
    """

    def __init__(self) -> None:
        self._latest: datetime | None = None

    def event(self, sub: Sub, row: ticks.Trade | ticks.Book) -> Recorded:
        at = row.at if self._latest is None or row.at > self._latest else self._latest
        self._latest = at
        match row:
            case ticks.Trade(price=price, qty=qty, bid=bid, ask=ask):
                return TickEvent(sub.symbol, at, price, qty, bid, ask)
            case ticks.Book(bids=bids, asks=asks):
                return BookEvent(sub.symbol, at, _levels(bids), _levels(asks))


def recorded_events(rows: Iterable[tuple[Sub, ticks.Trade | ticks.Book]]) -> list[Recorded]:
    """저장한 행을 받은 순서 그대로 이벤트로 바꾼다(백테스트)."""
    timeline = Timeline()
    return [timeline.event(sub, row) for sub, row in rows]


class Subscription:
    """전략 하나가 받는 실시간 흐름. `async for`로 읽고, FeedClosed가 마지막이다.

    push·end는 수신기만 부른다.
    """

    def __init__(self, user_id: UserId, owner: str, subs: frozenset[Sub]) -> None:
        self.user_id = user_id
        self.owner = owner
        self.subs = subs
        self._timeline = Timeline()
        self._queue: asyncio.Queue[FeedItem] = asyncio.Queue()
        self._closed: FeedClosed | None = None
        self._done = False

    def __repr__(self) -> str:
        return f"Subscription(user={self.user_id}, owner={self.owner!r})"

    @property
    def closed(self) -> FeedClosed | None:
        return self._closed

    def push(self, item: Recorded | FeedGap | FeedResumed) -> bool:
        """넣는다. 이미 MAX_PENDING개가 쌓였으면 넣지 않고 False."""
        if self._closed is not None:
            return True
        if self._queue.qsize() >= MAX_PENDING:
            return False
        self._queue.put_nowait(item)
        return True

    def deliver(self, sub: Sub, row: ticks.Trade | ticks.Book) -> bool:
        return self.push(self._timeline.event(sub, row))

    def end(self, reason: CloseReason) -> None:
        """끝낸다. 밀렸던 것은 버린다(지난 시세로 판단하지 않게)."""
        if self._closed is not None:
            return
        self._closed = FeedClosed(reason)
        while not self._queue.empty():
            self._queue.get_nowait()
        self._queue.put_nowait(self._closed)

    def __aiter__(self) -> "Subscription":
        return self

    async def __anext__(self) -> FeedItem:
        if self._done:
            raise StopAsyncIteration
        item = await self._queue.get()
        if isinstance(item, FeedClosed):
            self._done = True
        return item


class Feed:
    """사용자 한 명의 전략 구독들. 세션이 받은 것을 그 흐름을 원한 구독에만 넘긴다.

    on_drop은 밀린 구독을 끊은 뒤 부른다(세션이 NH 구독을 다시 맞춘다).
    """

    def __init__(self, user_id: UserId, on_drop: Callable[[], None]) -> None:
        self._user = user_id
        self._on_drop = on_drop
        self._subscriptions: list[Subscription] = []

    def wanted(self) -> frozenset[Sub]:
        return frozenset(sub for s in self._subscriptions for sub in s.subs)

    def add(self, owner: str, subs: frozenset[Sub]) -> Subscription:
        handle = Subscription(self._user, owner, subs)
        self._subscriptions.append(handle)
        return handle

    def remove(self, handle: Subscription, reason: CloseReason) -> bool:
        """끝내고 뺀다. 이미 빠졌으면 False."""
        if handle not in self._subscriptions:
            return False
        self._subscriptions.remove(handle)
        handle.end(reason)
        return True

    def data(self, sub: Sub, row: Row, day: date) -> None:
        """받은 행 하나. 저장 형식 그대로의 행이라 백테스트와 같은 길로 읽는다."""
        targets = [s for s in self._subscriptions if sub in s.subs]
        if not targets:
            return
        record = ticks.read_row(sub.stream, row, day)
        self._settle([s for s in targets if not s.deliver(sub, record)])

    def mark(self, sub: Sub, item: FeedGap | FeedResumed) -> None:
        self._settle([s for s in self._subscriptions if sub in s.subs and not s.push(item)])

    def close_all(self, reason: CloseReason) -> None:
        for handle in self._subscriptions:
            handle.end(reason)
        self._subscriptions.clear()

    def _settle(self, behind: list[Subscription]) -> None:
        for handle in behind:
            self.remove(handle, "slow_consumer")
            log.warning(
                "사용자 %s 전략 구독 %s이 %d개 넘게 밀려 끊었습니다.",
                self._user,
                handle.owner,
                MAX_PENDING,
            )
        if behind:
            self._on_drop()
