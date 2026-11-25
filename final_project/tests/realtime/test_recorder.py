"""시세 수신기를 가짜 NH 실시간 서버(127.0.0.1)에 붙여 본다. 연결 함수는 운영 것을 그대로 쓴다."""

import asyncio
import json
import logging
import sqlite3
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from nh_mcp.domain import KST, UserId
from nh_mcp.nh.client import StreamLogin
from nh_mcp.realtime.recorder import MAX_SUBSCRIPTIONS, BadSymbol, Recorder, TooManySubscriptions
from nh_mcp.realtime.session import Counts, Link, Tuning
from nh_mcp.store import db, recordings, ticks, users
from nh_mcp.store.ticks import Sub, TickStore

from .fake_stream import CANARY_KEY_PREFIX, CANARY_TOKEN, FakeStream, book, trade, until

SYM = "A016C000"
TICK, BOOK = Sub(SYM, "tick"), Sub(SYM, "book")
# 장중 한가운데(KST 10:00:30). 분 경계는 시계를 직접 밀어 넘긴다.
T0 = datetime(2026, 12, 1, 10, 0, 30, tzinfo=KST).astimezone(UTC)
HOUR = (datetime(2026, 12, 1, 10, 0, tzinfo=KST), datetime(2026, 12, 1, 11, 0, tzinfo=KST))
FAST = Tuning(
    backoff_base=0.01, backoff_cap=0.04, idle_timeout=5.0, init_timeout=2.0, flush_every=0.01
)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@dataclass
class Rig:
    fake: FakeStream
    recorder: Recorder
    clock: Clock
    conn: sqlite3.Connection
    slept: list[float]
    user: UserId
    other: UserId

    @property
    def store(self) -> TickStore:
        return self.recorder.store

    def live(self, user: UserId | None = None) -> set[Sub]:
        status = self.recorder.status(user or self.user)
        return set() if status is None else set(status.live)

    def connected(self) -> bool:
        status = self.recorder.status(self.user)
        return status is not None and status.connected

    def failed(self) -> dict[Sub, str]:
        status = self.recorder.status(self.user)
        return {} if status is None else dict(status.failed)

    def counts(self, user: UserId | None = None) -> Counts:
        status = self.recorder.status(user or self.user)
        assert status is not None
        return status.counts

    def stored_trades(self, user: UserId | None = None) -> int:
        return len(self.store.trades(user or self.user, SYM, *HOUR))


def make_conn() -> sqlite3.Connection:
    conn = db.connect(":memory:")
    users.get_or_create(conn, "sub-a", "a@example.com", T0)
    users.get_or_create(conn, "sub-b", "b@example.com", T0)
    return conn


@asynccontextmanager
async def rig(
    tmp_path: Path,
    fake: FakeStream | None = None,
    conn: sqlite3.Connection | None = None,
    clock: Clock | None = None,
) -> AsyncGenerator[Rig]:
    fake = fake or FakeStream()
    conn = conn or make_conn()
    clock = clock or Clock()
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        await asyncio.sleep(0)

    link = Link(fake.login, clock, fake.connect, FAST, sleep=sleep, jitter=lambda: 1.0)
    recorder = Recorder(conn, tmp_path / "ticks.db", link)
    async with fake.running(), recorder.running():
        yield Rig(fake, recorder, clock, conn, slept, UserId(1), UserId(2))


async def recording(r: Rig, *subs: Sub) -> None:
    await r.recorder.set_recording(r.user, subs)
    await until(lambda: r.live() == set(subs))


def test_connects_in_order_and_subscribes_each_stream(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with rig(tmp_path) as r:
            await recording(r, TICK, BOOK)
            conn = r.fake.last
            # 키 발급 → 그 키로 연결 → session_init(접근토큰) → 구독
            assert r.fake.issued == 1
            assert conn.access_key == CANARY_KEY_PREFIX + "0001"
            assert json.loads(conn.raw[0]) == {
                "header": {"api_ver": "1", "action": "0"},
                "body": {"action_code": "session_init", "session_token": CANARY_TOKEN},
            }
            assert conn.sent("A") == [("KB", SYM), ("KA", SYM)]

    asyncio.run(scenario())


def test_rows_are_stored_when_the_minute_changes_in_receive_order(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with rig(tmp_path) as r:
            await recording(r, TICK, BOOK)
            await r.fake.push(trade(SYM, "100030", "726.15", "2"))
            await r.fake.push(book(SYM, "100030"))
            await r.fake.push(trade(SYM, "100031", "726.10", "1"))
            await until(lambda: r.counts().tick == 2 and r.counts().book == 1)
            await asyncio.sleep(0.05)
            assert r.stored_trades() == 0  # 열린 분은 아직 메모리에만 있다
            r.clock.advance(30)
            await until(lambda: r.stored_trades() == 2)
            trades = r.store.trades(r.user, SYM, *HOUR)
            assert [(t.at.time().isoformat(), t.price, t.qty, t.bid, t.ask) for t in trades] == [
                ("10:00:30", Decimal("726.15"), 2, Decimal("726.10"), Decimal("726.15")),
                ("10:00:31", Decimal("726.10"), 1, Decimal("726.10"), Decimal("726.15")),
            ]
            books = r.store.books(r.user, SYM, *HOUR)
            assert [str(level.price) for level in books[0].asks[:2]] == ["726.15", "726.20"]
            assert trades[0].seq < books[0].seq < trades[1].seq

    asyncio.run(scenario())


def test_pingpong_is_echoed_verbatim(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with rig(tmp_path) as r:
            await recording(r, TICK)
            ping = '{"code" :"pingpong", "msg": "pingpong" }'
            await r.fake.last.socket.send(ping)
            await until(lambda: ping in r.fake.last.raw)
            assert r.counts().pings == 1

    asyncio.run(scenario())


def test_night_unknown_and_non_trade_rows_are_dropped_and_counted(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with rig(tmp_path) as r:
            await recording(r, TICK, BOOK)
            night = trade(SYM, "190000", "726.15", "1")
            night["header"] = {"action_code": "KC", "action_sym": SYM}
            await r.fake.push(night)
            await r.fake.push(book(SYM, "100030", code="KD"))
            await r.fake.push(trade(SYM, "100030", "726.15", "0", tick_div="0"))
            await r.fake.push(trade(SYM, "100031", "726.15", "3"))
            await until(lambda: r.counts().tick == 1)
            counts = r.counts()
            assert (counts.night, counts.unknown, counts.not_trade) == (1, 1, 1)
            r.clock.advance(30)
            await until(lambda: r.stored_trades() == 1)
            assert r.store.books(r.user, SYM, *HOUR) == []

    asyncio.run(scenario())


def test_refused_subscription_is_an_error_and_a_gap(tmp_path: Path) -> None:
    fake = FakeStream()
    fake.refuse.add(("KB", SYM))

    async def scenario() -> None:
        async with rig(tmp_path, fake) as r:
            await r.recorder.set_recording(r.user, {TICK, BOOK})
            await until(lambda: r.live() == {TICK} and bool(r.failed()))
            assert r.failed() == {BOOK: fake.refusal}
            gaps = r.store.gaps(r.user)
            assert [(g.sub, g.reason, g.ended_at) for g in gaps] == [
                (BOOK, "subscribe_failed", None)
            ]

    asyncio.run(scenario())


def test_disconnect_reconnects_with_a_new_key_resubscribes_and_records_the_gap(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with rig(tmp_path) as r:
            await recording(r, TICK, BOOK)
            r.clock.advance(1)
            await r.fake.push(trade(SYM, "100031", "726.15", "2"))
            await until(lambda: r.counts().tick == 1)
            last_seen = r.clock()
            r.clock.advance(5)
            await r.fake.drop()
            await until(lambda: len(r.fake.conns) == 2 and r.live() == {TICK, BOOK})
            assert r.fake.conns[1].access_key != r.fake.conns[0].access_key
            assert r.fake.conns[1].sent("A") == [("KB", SYM), ("KA", SYM)]
            assert r.slept == [0.01]
            gaps = r.store.gaps(r.user)
            assert {(g.sub, g.reason, g.started_at, g.ended_at) for g in gaps} == {
                (sub, "disconnect", last_seen, r.clock()) for sub in (TICK, BOOK)
            }
            # 끊기기 전에 받은 행도 남는다.
            r.clock.advance(30)
            await until(lambda: r.stored_trades() == 1)

    asyncio.run(scenario())


def test_backoff_grows_and_is_capped_while_the_server_refuses(tmp_path: Path) -> None:
    fake = FakeStream()
    fake.init_ok = False

    async def scenario() -> None:
        async with rig(tmp_path, fake) as r:
            await r.recorder.set_recording(r.user, {TICK})
            await until(lambda: len(r.slept) >= 4)
            assert r.slept[:4] == [0.01, 0.02, 0.04, 0.04]
            # session_init이 거절되면 다음 연결은 토큰을 새로 받는다.
            assert fake.renews[:2] == [False, True]
            fake.init_ok = True
            await until(lambda: r.live() == {TICK})

    asyncio.run(scenario())


def test_desired_set_converges_and_repeating_it_sends_nothing(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with rig(tmp_path) as r:
            await recording(r, TICK, BOOK)
            conn = r.fake.last
            before = len(conn.received)
            await r.recorder.set_recording(r.user, {BOOK, TICK})
            await asyncio.sleep(0.05)
            assert len(conn.received) == before
            await r.recorder.set_recording(r.user, {TICK})
            await until(lambda: conn.sent("D") == [("KB", SYM)])
            assert r.live() == {TICK}
            assert recordings.load(r.conn, r.user) == {TICK}
            await r.recorder.set_recording(r.user, set())
            await until(lambda: conn.sent("D") == [("KB", SYM), ("KA", SYM)])
            await until(lambda: not r.connected())
            await asyncio.sleep(0.05)
            # 저장할 것이 없으면 다시 연결하지 않는다. 일부러 멈춘 것은 빈 구간이 아니다.
            assert len(r.fake.conns) == 1
            assert r.store.gaps(r.user) == []

    asyncio.run(scenario())


def test_more_than_twenty_subscriptions_are_refused(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with rig(tmp_path) as r:
            subs = [Sub(f"B{n:03d}C000", "tick") for n in range(MAX_SUBSCRIPTIONS - 1)]
            subs += [Sub("A016C000", "tick"), Sub("A016C000", "book")]
            with pytest.raises(TooManySubscriptions) as caught:
                await r.recorder.set_recording(r.user, subs)
            assert caught.value.count == MAX_SUBSCRIPTIONS + 1
            with pytest.raises(BadSymbol):
                await r.recorder.set_recording(r.user, {Sub("A016C000?x=1", "tick")})
            await asyncio.sleep(0.05)
            assert recordings.load(r.conn, r.user) == frozenset()
            assert r.fake.conns == []
            await r.recorder.set_recording(r.user, subs[:MAX_SUBSCRIPTIONS])
            await until(lambda: len(r.live()) == MAX_SUBSCRIPTIONS)

    asyncio.run(scenario())


def test_restart_resumes_saved_recordings_and_closes_the_stop_gap(tmp_path: Path) -> None:
    conn, clock = make_conn(), Clock()

    async def scenario() -> None:
        async with rig(tmp_path, conn=conn, clock=clock) as r:
            await recording(r, TICK)
            await r.fake.push(trade(SYM, "100030", "726.15", "2"))
            await until(lambda: r.counts().tick == 1)
        stopped_at = clock()
        # 내려가며 열린 분까지 저장했다.
        store = TickStore(ticks.connect(tmp_path / "ticks.db"))
        assert len(store.trades(UserId(1), SYM, *HOUR)) == 1
        assert [(g.reason, g.started_at, g.ended_at) for g in store.gaps(UserId(1))] == [
            ("server_stop", stopped_at, None)
        ]
        store.close()
        clock.advance(120)
        async with rig(tmp_path, conn=conn, clock=clock) as r:
            await until(lambda: r.live() == {TICK})
            assert [(g.reason, g.started_at, g.ended_at) for g in r.store.gaps(r.user)] == [
                ("server_stop", stopped_at, clock())
            ]

    asyncio.run(scenario())


def test_after_a_crash_the_lost_open_minute_is_a_gap(tmp_path: Path) -> None:
    conn, clock = make_conn(), Clock()
    # 죽기 전의 흔적: 저장 설정과 마지막으로 다 저장된 시각(분 경계). 내려가며 연 빈 구간은 없다.
    recordings.replace(conn, UserId(1), frozenset({TICK}), T0)
    persisted = datetime(2026, 12, 1, 10, 0, tzinfo=KST)
    store = TickStore(ticks.connect(tmp_path / "ticks.db"))
    store.write(UserId(1), [], [TICK], persisted)
    store.close()
    clock.advance(600)

    async def scenario() -> None:
        async with rig(tmp_path, conn=conn, clock=clock) as r:
            await until(lambda: r.live() == {TICK})
            assert [(g.reason, g.started_at, g.ended_at) for g in r.store.gaps(r.user)] == [
                ("crash", persisted, clock())
            ]

    asyncio.run(scenario())


def test_each_user_has_their_own_connection_and_rows(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with rig(tmp_path) as r:
            await recording(r, TICK)
            a_conn = r.fake.last
            await r.recorder.set_recording(r.other, {TICK})
            await until(lambda: r.live(r.other) == {TICK})
            b_conn = r.fake.last
            assert a_conn is not b_conn
            await a_conn.socket.send(json.dumps(trade(SYM, "100030", "726.15", "2")))
            await until(lambda: r.counts().tick == 1)
            r.clock.advance(30)
            await until(lambda: r.stored_trades() == 1)
            assert r.counts(r.other).tick == 0
            assert r.stored_trades(r.other) == 0
            assert r.store.gaps(r.other) == []

    asyncio.run(scenario())


def test_websocket_key_and_token_never_reach_logs_errors_or_reprs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fake = FakeStream()
    fake.init_ok = False
    fake.refuse.add(("KB", SYM))
    # NH가 거절 사유에 토큰을 되돌려 보내는 최악의 경우까지 본다.
    fake.refusal = f"거절 {CANARY_TOKEN}"
    shown: list[str] = []

    async def scenario() -> None:
        async with rig(tmp_path, fake) as r:
            await r.recorder.set_recording(r.user, {TICK, BOOK})
            await until(lambda: len(r.slept) >= 1)
            fake.init_ok = True
            await until(lambda: r.live() == {TICK} and bool(r.failed()))
            await r.fake.drop()
            await until(lambda: len(r.fake.conns) >= 3 and r.live() == {TICK})
            shown.extend([repr(r.recorder.status(r.user)), repr(r.recorder), repr(r.store)])
        shown.append(repr(StreamLogin(CANARY_KEY_PREFIX + "9999", CANARY_TOKEN)))

    with caplog.at_level(logging.DEBUG):
        asyncio.run(scenario())

    text = " ".join(shown + [record.getMessage() for record in caplog.records])
    assert "사용자 1 구독 거절" in text  # 로그가 실제로 잡혔다
    assert CANARY_TOKEN not in text
    assert CANARY_KEY_PREFIX not in text
