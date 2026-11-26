"""틱·호가 수신기 전체. MCP 서버 프로세스 안에서 사용자마다 asyncio 작업으로 돈다.

- 저장 설정은 주 DB(`tick_recordings`)에 있다. 서버를 다시 띄우면 그대로 다시 연다(주문이 아니라
  저절로 다시 켜도 된다). 지난번에 살아 있던 구독은 그때부터 다시 구독될 때까지 빈 구간이다.
- 구독은 APP Key(= 사용자)당 20건까지다. 종목 하나의 흐름 하나가 1건이다. 틱 저장과 전략 구독의
  합집합을 센다(둘 다 원해도 1건). 넘으면 거부하고 아무것도 바꾸지 않는다.
- 전략 구독(`subscribe`)은 저장하지 않는다. 서버를 다시 띄우면 없다(전략은 일시정지로 돌아온다).
- 보관 기간이 지난 묶음은 켤 때와 그 뒤 한 시간마다 지운다(`purge`).
- 사용자마다 연결·버퍼·순번을 따로 둔다. 같이 쓰는 것은 ticks.db 파일 하나이고, 모든 행에 사용자
  식별자가 붙는다(안전 불변식 8).
"""

import asyncio
import logging
import re
import sqlite3
from collections.abc import AsyncGenerator, Iterable
from contextlib import asynccontextmanager
from pathlib import Path

from nh_mcp.domain import Clock, UserId
from nh_mcp.nh.client import StreamLogin
from nh_mcp.nh.clients import ClientPool
from nh_mcp.realtime.feed import Subscription
from nh_mcp.realtime.session import Link, Login, Session, Status
from nh_mcp.store import recordings, ticks
from nh_mcp.store.ticks import Sub, TickStore

log = logging.getLogger(__name__)

MAX_SUBSCRIPTIONS = 20
PURGE_EVERY_SECONDS = 3600.0
SYMBOL = re.compile(r"[A-Z0-9]{1,12}")


class TooManySubscriptions(Exception):
    def __init__(self, count: int) -> None:
        super().__init__(
            f"실시간 구독은 APP Key당 {MAX_SUBSCRIPTIONS}건까지입니다"
            f"(틱 저장과 전략을 합쳐 {count}건)."
        )
        self.count = count


class BadSymbol(Exception):
    """종목코드 모양이 아니다(영문 대문자·숫자 12자 이하)."""


def pool_login(pool: ClientPool) -> Login:
    """사용자의 국내 NH 클라이언트로 웹소켓 키를 받는다. 키를 푸는 곳은 여전히 ClientPool뿐이다."""

    async def login(user_id: UserId, renew_token: bool) -> StreamLogin:
        client = await pool.client_for(user_id, "domestic")
        return await client.stream_login(renew_token)

    return login


class Recorder:
    def __init__(self, conn: sqlite3.Connection, ticks_path: Path | str, link: Link) -> None:
        self._conn = conn
        self._ticks_path = ticks_path
        self._link = link
        self._clock: Clock = link.clock
        self._store: TickStore | None = None
        self._sessions: dict[UserId, Session] = {}
        self._housekeeping: asyncio.Task[None] | None = None

    @property
    def store(self) -> TickStore:
        if self._store is None:
            raise RuntimeError("수신기가 켜지지 않았습니다.")
        return self._store

    async def start(self) -> None:
        """ticks.db를 열고, 저장 설정대로 사용자마다 연결을 연다."""
        store = TickStore(ticks.connect(self._ticks_path))
        self._store = store
        now = self._clock()
        store.purge(now)
        for user_id, subs in recordings.load_all(self._conn).items():
            for sub in subs:
                # 살아 있던 구독에 내려가며 연 빈 구간이 없으면 죽은 것이다(열린 분을 잃음).
                until = store.persisted_until(user_id, sub)
                if until is not None:
                    store.open_gap(user_id, sub, until, "crash")
            self._session(user_id).start(subs)
        self._housekeeping = asyncio.create_task(self._purge_loop())

    async def stop(self) -> None:
        if self._housekeeping is not None:
            self._housekeeping.cancel()
            await asyncio.gather(self._housekeeping, return_exceptions=True)
        await asyncio.gather(*(session.stop() for session in self._sessions.values()))
        self._sessions.clear()
        if self._store is not None:
            self._store.close()
            self._store = None

    @asynccontextmanager
    async def running(self) -> AsyncGenerator["Recorder"]:
        await self.start()
        try:
            yield self
        finally:
            await self.stop()

    async def set_recording(self, user_id: UserId, subs: Iterable[Sub]) -> frozenset[Sub]:
        """저장할 (종목, 흐름) 전체를 정한다. 같은 집합을 다시 주면 아무것도 바뀌지 않는다."""
        desired = frozenset(subs)
        _check(desired)
        session = self._sessions.get(user_id)
        _fits(desired | self.owned(user_id))
        recordings.replace(self._conn, user_id, desired, self._clock())
        if self._store is not None:
            if session is None:
                self._session(user_id).start(desired)
            else:
                session.record(desired)
                await session.refresh()
        return desired

    async def subscribe(self, user_id: UserId, owner: str, subs: frozenset[Sub]) -> Subscription:
        """전략 하나(owner)가 이 사용자의 실시간 체결·호가를 받는다. 저장 설정에는 남기지 않는다.

        넘치면 TooManySubscriptions이고 아무것도 바뀌지 않는다. 받는 것은 이 사용자의 것, 그중
        subs뿐이다. 수신기가 켜지지 않았으면 RuntimeError.
        """
        _check(subs)
        session = self._sessions.get(user_id)
        recording = frozenset[Sub]() if session is None else session.recording
        _fits(recording | self.owned(user_id) | subs)
        if session is None:
            session = self._session(user_id)
            handle = session.subscribe(owner, subs)
            session.start(frozenset())
        else:
            handle = session.subscribe(owner, subs)
            await session.refresh()
        return handle

    async def unsubscribe(self, handle: Subscription) -> None:
        """그 구독만 뺀다. 틱 저장이나 다른 전략이 원하는 NH 구독은 남는다. 두 번 불러도 된다."""
        session = self._sessions.get(handle.user_id)
        if session is not None and session.feed.remove(handle, "unsubscribed"):
            await session.refresh()

    def owned(self, user_id: UserId) -> frozenset[Sub]:
        """이 사용자의 전략 구독들이 원하는 (종목, 흐름)."""
        session = self._sessions.get(user_id)
        return frozenset() if session is None else session.feed.wanted()

    def status(self, user_id: UserId) -> Status | None:
        session = self._sessions.get(user_id)
        return None if session is None else session.status()

    def _session(self, user_id: UserId) -> Session:
        session = Session(user_id, self.store, self._link)
        self._sessions[user_id] = session
        return session

    async def _purge_loop(self) -> None:
        while True:
            await asyncio.sleep(PURGE_EVERY_SECONDS)
            removed = await asyncio.to_thread(self.store.purge, self._clock())
            if removed:
                log.info("보관 기간이 지난 틱 묶음 %d개를 지웠습니다.", removed)


def _check(subs: Iterable[Sub]) -> None:
    for sub in subs:
        if not SYMBOL.fullmatch(sub.symbol):
            raise BadSymbol(sub.symbol)


def _fits(union: frozenset[Sub]) -> None:
    if len(union) > MAX_SUBSCRIPTIONS:
        raise TooManySubscriptions(len(union))
