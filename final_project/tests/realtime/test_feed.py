"""전략 구독: 수신기가 받은 체결·호가를 전략에 나눠 준다. 가짜 NH 실시간 서버에 붙인다."""

import asyncio
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from nh_mcp.domain import BookEvent, TickEvent
from nh_mcp.realtime import feed
from nh_mcp.realtime.feed import (
    FeedClosed,
    FeedGap,
    FeedItem,
    FeedResumed,
    Recorded,
    Subscription,
)
from nh_mcp.realtime.recorder import MAX_SUBSCRIPTIONS, TooManySubscriptions
from nh_mcp.services.backtest import read_recording
from nh_mcp.store import recordings
from nh_mcp.store.ticks import Sub

from .fake_stream import FakeStream, book, trade, until
from .test_recorder import BOOK, HOUR, SYM, TICK, make_conn, recording, rig

OTHER = "A0173000"
OTHER_BOOK = Sub(OTHER, "book")
DAY = date(2026, 12, 1)


async def take(handle: Subscription, n: int) -> list[FeedItem]:
    async with asyncio.timeout(5):
        return [await anext(handle) for _ in range(n)]


def events(items: list[FeedItem]) -> list[Recorded]:
    return [i for i in items if isinstance(i, TickEvent | BookEvent)]


def test_live_events_are_the_backtest_events_of_the_same_rows(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with rig(tmp_path) as r:
            await recording(r, TICK, BOOK)
            handle = await r.recorder.subscribe(r.user, "run-1", frozenset({TICK, BOOK}))
            # 이미 받는 중인 흐름은 FeedResumed가 먼저 온다.
            assert await take(handle, 2) == [
                FeedResumed(BOOK, r.clock()),
                FeedResumed(TICK, r.clock()),
            ]
            await r.fake.push(trade(SYM, "100031", "726.15", "2"))
            # 거래소 시각이 앞 체결보다 이른 호가: at은 뒤로 가지 않는다.
            await r.fake.push(book(SYM, "100029"))
            await r.fake.push(trade(SYM, "100032", "726.10", "1"))
            await r.fake.push(book(SYM, "100033", best_ask=72620))
            live = events(await take(handle, 4))
            r.clock.advance(30)
            await until(lambda: r.stored_trades() == 2)
            stored = read_recording(r.store, r.user, {SYM: frozenset({"tick", "book"})}, (DAY, DAY))
            assert stored.events == live
            first, early_book = live[0], live[1]
            assert isinstance(first, TickEvent) and isinstance(early_book, BookEvent)
            assert early_book.at == first.at
            assert (first.price, first.bid, first.ask) == (
                Decimal("726.15"),
                Decimal("726.10"),
                Decimal("726.15"),
            )
            assert early_book.asks[0] == (Decimal("726.15"), 1)

    asyncio.run(scenario())


def test_recording_and_strategies_share_twenty_and_a_shared_stream_counts_once(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with rig(tmp_path) as r:
            await recording(r, TICK)
            await r.recorder.subscribe(r.user, "run-1", frozenset({TICK, BOOK}))
            await until(lambda: r.live() == {TICK, BOOK})
            conn = r.fake.last
            assert conn.sent("A") == [("KA", SYM), ("KB", SYM)]
            assert r.recorder.owned(r.user) == {TICK, BOOK}
            many = frozenset(Sub(f"B{n:03d}C000", "tick") for n in range(MAX_SUBSCRIPTIONS - 1))
            with pytest.raises(TooManySubscriptions) as caught:
                await r.recorder.subscribe(r.user, "run-2", many)
            assert caught.value.count == MAX_SUBSCRIPTIONS + 1
            with pytest.raises(TooManySubscriptions):
                await r.recorder.set_recording(r.user, {TICK, *many})
            await asyncio.sleep(0.05)
            # 거절은 아무것도 바꾸지 않는다.
            assert r.recorder.owned(r.user) == {TICK, BOOK}
            assert recordings.load(r.conn, r.user) == {TICK}
            assert len(conn.sent("A")) == 2
            fits = frozenset(list(many)[:-1])
            await r.recorder.subscribe(r.user, "run-2", fits)
            await until(lambda: len(r.live()) == MAX_SUBSCRIPTIONS)

    asyncio.run(scenario())


def test_unsubscribing_keeps_what_recording_still_wants(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with rig(tmp_path) as r:
            await recording(r, TICK)
            handle = await r.recorder.subscribe(r.user, "run-1", frozenset({TICK, BOOK}))
            await until(lambda: r.live() == {TICK, BOOK})
            conn = r.fake.last
            await r.recorder.unsubscribe(handle)
            await r.recorder.unsubscribe(handle)
            await until(lambda: conn.sent("D") == [("KB", SYM)])
            assert r.live() == {TICK}
            items = [item async for item in handle]
            assert items[-1] == FeedClosed("unsubscribed")
            assert handle.closed == FeedClosed("unsubscribed")
            # 전략만 원하던 것이 다 빠지면 연결을 닫는다.
            await r.recorder.set_recording(r.user, set())
            await until(lambda: not r.connected())
            only = await r.recorder.subscribe(r.user, "run-2", frozenset({BOOK}))
            await until(lambda: r.connected() and r.live() == {BOOK})
            await r.recorder.unsubscribe(only)
            await until(lambda: not r.connected())
            assert len(r.fake.conns) == 2

    asyncio.run(scenario())


def test_strategy_only_streams_reach_the_strategy_but_are_not_stored(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with rig(tmp_path) as r:
            await recording(r, TICK)
            handle = await r.recorder.subscribe(r.user, "run-1", frozenset({BOOK}))
            assert await take(handle, 1) == [FeedResumed(BOOK, r.clock())]
            await r.fake.push(book(SYM, "100030"))
            await r.fake.push(trade(SYM, "100031", "726.15", "2"))
            assert [type(e) for e in events(await take(handle, 1))] == [BookEvent]
            await r.fake.drop()
            await until(lambda: len(r.fake.conns) == 2 and r.live() == {TICK, BOOK})
            r.clock.advance(30)
            await until(lambda: r.stored_trades() == 1)
            assert r.store.books(r.user, SYM, *HOUR) == []
            # 빈 구간도 저장하는 흐름에만 남는다.
            assert {g.sub for g in r.store.gaps(r.user)} == {TICK}

    asyncio.run(scenario())


def test_each_owner_and_each_user_sees_only_their_own_streams(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with rig(tmp_path) as r:
            x = await r.recorder.subscribe(r.user, "run-x", frozenset({TICK}))
            y = await r.recorder.subscribe(r.user, "run-y", frozenset({OTHER_BOOK}))
            await until(lambda: r.live() == {TICK, OTHER_BOOK})
            mine = r.fake.last
            z = await r.recorder.subscribe(r.other, "run-z", frozenset({TICK}))
            await until(lambda: r.live(r.other) == {TICK})
            theirs = r.fake.last
            assert mine is not theirs
            await mine.socket.send(json.dumps(trade(SYM, "100030", "726.15", "2")))
            await mine.socket.send(json.dumps(book(OTHER, "100030")))
            await theirs.socket.send(json.dumps(trade(SYM, "100031", "700.00", "9")))
            got_x, got_y, got_z = [events(await take(h, 2)) for h in (x, y, z)]
            assert [(e.symbol, type(e)) for e in got_x] == [(SYM, TickEvent)]
            assert [(e.symbol, type(e)) for e in got_y] == [(OTHER, BookEvent)]
            assert [(e.symbol, e.price) for e in got_z if isinstance(e, TickEvent)] == [
                (SYM, Decimal("700.00"))
            ]

    asyncio.run(scenario())


def test_a_disconnect_is_a_gap_then_resumed_and_a_refusal_is_a_gap(tmp_path: Path) -> None:
    fake = FakeStream()
    fake.refuse.add(("KB", OTHER))

    async def scenario() -> None:
        async with rig(tmp_path, fake) as r:
            handle = await r.recorder.subscribe(r.user, "run-1", frozenset({TICK, OTHER_BOOK}))
            started = r.clock()
            first = await take(handle, 2)
            assert set(first) == {
                FeedResumed(TICK, started),
                FeedGap(OTHER_BOOK, started, "subscribe_failed"),
            }
            r.clock.advance(1)
            await r.fake.push(trade(SYM, "100031", "726.15", "2"))
            last_seen = r.clock()
            assert len(events(await take(handle, 1))) == 1
            r.clock.advance(5)
            await r.fake.drop()
            gap, resumed, refused = await take(handle, 3)
            assert gap == FeedGap(TICK, last_seen, "disconnect")
            assert resumed == FeedResumed(TICK, r.clock())
            assert refused == FeedGap(OTHER_BOOK, r.clock(), "subscribe_failed")
            # 저장하지 않는 흐름이라 ticks.db에는 빈 구간이 없다.
            assert r.store.gaps(r.user) == []

    asyncio.run(scenario())


def test_a_slow_strategy_is_dropped_while_recording_and_others_go_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(feed, "MAX_PENDING", 3)
    pushed = 5

    async def scenario() -> None:
        async with rig(tmp_path) as r:
            await recording(r, TICK)
            slow = await r.recorder.subscribe(r.user, "slow", frozenset({TICK, BOOK}))
            fast = await r.recorder.subscribe(r.user, "fast", frozenset({TICK}))
            await until(lambda: r.live() == {TICK, BOOK})
            conn = r.fake.last
            seen: list[FeedItem] = []

            async def read() -> None:
                async for item in fast:
                    seen.append(item)

            reader = asyncio.create_task(read())
            for n in range(pushed):
                await r.fake.push(trade(SYM, f"1000{31 + n}", "726.15", "1"))
                await until(lambda n=n: r.counts().tick == n + 1)
            assert slow.closed == FeedClosed("slow_consumer")
            # 밀렸던 것은 버리고 끝만 남는다.
            assert [item async for item in slow] == [FeedClosed("slow_consumer")]
            # 끊긴 전략만 원하던 호가는 해제하고, 저장하는 체결은 남긴다.
            await until(lambda: conn.sent("D") == [("KB", SYM)])
            assert r.live() == {TICK}
            await until(lambda: len(events(seen)) == pushed)
            r.clock.advance(30)
            await until(lambda: r.stored_trades() == pushed)
            reader.cancel()

    asyncio.run(scenario())


def test_strategy_subscriptions_end_with_the_server_and_are_not_kept(tmp_path: Path) -> None:
    conn = make_conn()

    async def scenario() -> Subscription:
        async with rig(tmp_path, conn=conn) as r:
            handle = await r.recorder.subscribe(r.user, "run-1", frozenset({TICK}))
            await until(lambda: r.live() == {TICK})
        async with rig(tmp_path, conn=conn) as again:
            await asyncio.sleep(0.05)
            assert again.recorder.owned(again.user) == frozenset()
            assert again.fake.conns == []
        assert recordings.load(conn, again.user) == frozenset()
        return handle

    handle = asyncio.run(scenario())
    assert handle.closed == FeedClosed("server_stop")
