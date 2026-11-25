"""사용자 한 명의 NH 실시간 연결. 저장할 구독이 하나라도 있을 때만 연다.

순서: 웹소켓 키 발급 → 연결 → session_init → INIT 확인 → 구독(A). pingpong은 받은 그대로 돌려준다.
원하는 집합(desired)으로 수렴한다: 빠진 구독은 해제(D), 새 구독은 구독(A). 같은 집합을 다시 주면
아무것도 보내지 않는다.

끊기면 늦추며(지수, 흔들기, 최대 60초) 새 키로 다시 연결하고 다시 구독한다. 끊긴 구간은 빈 구간이다:
그 연결에서 마지막으로 메시지를 받은 시각부터 다시 구독이 확인될 때까지.

받은 행은 받은 분(KST)마다 메모리에 모았다가 분이 바뀌면 묶음으로 저장한다. 서버가 죽으면 열린 분을
잃는다. 그 구간은 다음에 켤 때 빈 구간으로 남는다(`store/ticks.py`의 `persisted_until`).

비밀값: 웹소켓 키는 연결 주소에, 접근토큰은 session_init에 들어간다. 주소와 그 메시지는 어디에도
적지 않는다. 연결 라이브러리의 로그는 끄고, 예외는 종류 이름만 적는다(안전 불변식 7).
"""

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Protocol

from websockets.asyncio.client import connect as websockets_connect
from websockets.exceptions import ConnectionClosed

from nh_mcp.config import NH_STREAM_URL
from nh_mcp.domain import Clock, UserId
from nh_mcp.nh.client import StreamLogin
from nh_mcp.realtime.protocol import (
    Data,
    InitReply,
    Pingpong,
    Skipped,
    SubReply,
    parse,
    session_init,
    stream_url,
    subscription,
)
from nh_mcp.store.ticks import Block, Row, Sub, TickStore, kst_minute

log = logging.getLogger(__name__)

# 연결 라이브러리는 DEBUG에서 요청 줄(주소, 곧 웹소켓 키)을 적는다. 어디에도 매달리지 않은 로거를
# 가장 높은 단계로 막아 넘긴다.
_SILENT = logging.Logger("nh_mcp.realtime.wire", level=logging.CRITICAL + 1)
_SILENT.disabled = True
MAX_MESSAGE_BYTES = 1 << 20

# (사용자, 토큰을 새로 받을지) → 연결 한 번에 쓸 키와 토큰.
type Login = Callable[[UserId, bool], Awaitable[StreamLogin]]
type Sleep = Callable[[float], Awaitable[None]]


class Socket(Protocol):
    async def send(self, message: str | bytes) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


type Connect = Callable[[str], AbstractAsyncContextManager[Socket]]


def open_socket(url: str) -> AbstractAsyncContextManager[Socket]:
    # NH는 프로토콜 ping이 아니라 앱 수준 pingpong을 쓴다(실제 연결로 확인).
    return websockets_connect(
        url, ping_interval=None, max_size=MAX_MESSAGE_BYTES, open_timeout=10, logger=_SILENT
    )


@dataclass(frozen=True)
class Tuning:
    backoff_base: float = 1.0
    backoff_cap: float = 60.0
    # 아무 메시지도 이만큼 오지 않으면 끊긴 것으로 본다. pingpong이 약 30초마다 온다.
    idle_timeout: float = 120.0
    init_timeout: float = 10.0
    flush_every: float = 1.0
    # 이만큼 버틴 연결이 끊기면 늦춤을 처음부터 다시 센다.
    healthy_after: float = 60.0


@dataclass(frozen=True)
class Link:
    """세션이 NH에 닿는 방법. 테스트는 연결·늦춤·시계를 바꿔 끼운다."""

    login: Login
    clock: Clock
    connect: Connect = open_socket
    tuning: Tuning = Tuning()
    sleep: Sleep = asyncio.sleep
    jitter: Callable[[], float] = random.random


@dataclass
class Counts:
    tick: int = 0
    book: int = 0
    not_trade: int = 0
    night: int = 0
    unknown: int = 0
    malformed: int = 0
    pings: int = 0
    connects: int = 0


@dataclass(frozen=True)
class Status:
    desired: frozenset[Sub]
    live: frozenset[Sub]
    # 구독이 거절된 것과 NH가 준 사유.
    failed: Mapping[Sub, str]
    connected: bool
    # 마지막 연결 실패의 종류(예외 이름 등). 메시지는 담지 않는다.
    error: str | None
    counts: Counts = field(default_factory=Counts)


class _Failed(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Buffer:
    """열린 분의 행. (구독, 받은 분) → 받은 순서의 행."""

    def __init__(self) -> None:
        self._open: dict[tuple[Sub, datetime], list[Row]] = {}

    def add(self, sub: Sub, minute: datetime, row: Row) -> None:
        rows = self._open.get((sub, minute))
        if rows is None:
            self._open[(sub, minute)] = [row]
        else:
            rows.append(row)

    def take(self, before: datetime | None) -> list[Block]:
        keys = [key for key in self._open if before is None or key[1] < before]
        return [Block(sub, minute, self._open.pop((sub, minute))) for sub, minute in keys]


def _hider(login: StreamLogin) -> Callable[[str], str]:
    def hide(text: str) -> str:
        for secret in (login.access_key, login.session_token):
            text = text.replace(secret, "***")
        return text

    return hide


class Session:
    def __init__(self, user_id: UserId, store: TickStore, link: Link) -> None:
        self._user = user_id
        self._store = store
        self._login = link.login
        self._connect = link.connect
        self._clock = link.clock
        self._tuning = link.tuning
        self._sleep = link.sleep
        self._jitter = link.jitter
        self._desired: frozenset[Sub] = frozenset()
        # 이번 연결에서 구독(A)을 보내고 해제(D)를 보내지 않은 것.
        self._requested: set[Sub] = set()
        self._sent_at: dict[Sub, datetime] = {}
        self._live: set[Sub] = set()
        self._failed: dict[Sub, str] = {}
        self._ws: Socket | None = None
        self._hide: Callable[[str], str] = str
        self._renew_token = False
        self._error: str | None = None
        self._lock = asyncio.Lock()
        self._flush_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._closing = asyncio.Event()
        self._buffer = _Buffer()
        self._flushed_minute = kst_minute(self._clock())
        self._seq = store.max_seq(user_id)
        self._last_seen = self._clock()
        self._counts = Counts()
        self._runner: asyncio.Task[None] | None = None
        self._flusher: asyncio.Task[None] | None = None

    def __repr__(self) -> str:
        return f"Session(user={self._user})"

    def start(self, desired: frozenset[Sub]) -> None:
        self._desired = desired
        self._runner = asyncio.create_task(self._run())
        self._flusher = asyncio.create_task(self._flush_loop())

    def status(self) -> Status:
        return Status(
            desired=self._desired,
            live=frozenset(self._live),
            failed=dict(self._failed),
            connected=self._ws is not None,
            error=self._error,
            counts=replace(self._counts),
        )

    async def set(self, desired: frozenset[Sub]) -> None:
        """원하는 집합을 바꾼다. 연결돼 있으면 차이만 보낸다. 비면 연결을 닫는다."""
        now = self._clock()
        async with self._lock:
            for sub in self._desired - desired:
                # 일부러 멈춘 것이라 빈 구간이 아니다. 다시 켤 때 빈 구간이 생기지 않게 지운다.
                self._store.forget(self._user, sub, now)
                self._live.discard(sub)
                self._failed.pop(sub, None)
            self._desired = desired
            self._wake.set()
            ws = self._ws
            if ws is None:
                return
            try:
                await self._converge(ws)
                if not desired:
                    await ws.close()
            except ConnectionClosed:
                return  # 받는 쪽이 끊김을 보고 다시 연결한다.

    async def stop(self) -> None:
        """서버가 내려간다. 열린 분까지 저장하고, 살아 있던 구독에 빈 구간을 연다."""
        # 쓰던 묶음은 마저 쓰게 둔다(취소하면 뒤늦은 쓰기가 마지막 쓰기를 덮을 수 있다).
        self._closing.set()
        if self._flusher is not None:
            await self._flusher
        if self._runner is not None:
            self._runner.cancel()
            await asyncio.gather(self._runner, return_exceptions=True)
        now = self._clock()
        await self._flush(final=True)
        for sub in self._live:
            self._store.open_gap(self._user, sub, now, "server_stop")

    async def _run(self) -> None:
        attempt = 0
        while True:
            self._wake.clear()
            if not self._desired:
                await self._wake.wait()
                continue
            opened = time.monotonic()
            try:
                await self._connection()
            except Exception as exc:
                # 메시지에 주소가 들어갈 수 있어 종류만 적는다.
                self._error = exc.reason if isinstance(exc, _Failed) else type(exc).__name__
                log.warning("사용자 %s 실시간 연결이 끊겼습니다: %s", self._user, self._error)
            else:
                self._error = None
                continue
            finally:
                self._ws = None
            self._lost()
            if time.monotonic() - opened >= self._tuning.healthy_after:
                attempt = 0
            delay = min(self._tuning.backoff_cap, self._tuning.backoff_base * 2**attempt)
            attempt += 1
            await self._sleep(delay * (0.5 + 0.5 * self._jitter()))

    async def _connection(self) -> None:
        """연결 하나. 원하는 집합이 비어 일부러 닫으면 그냥 돌아오고, 그 밖의 끝은 예외다."""
        renew, self._renew_token = self._renew_token, False
        login = await self._login(self._user, renew)
        async with self._connect(stream_url(NH_STREAM_URL, login.access_key)) as ws:
            self._counts.connects += 1
            await ws.send(session_init(login.session_token))
            await self._wait_init(ws)
            self._hide = _hider(login)
            self._last_seen = self._clock()
            log.info("사용자 %s 실시간 연결 완료", self._user)
            async with self._lock:
                self._ws = ws
                self._requested.clear()
                await self._converge(ws)
            await self._receive(ws)

    async def _wait_init(self, ws: Socket) -> None:
        async with asyncio.timeout(self._tuning.init_timeout):
            while True:
                raw = await ws.recv()
                message = parse(raw)
                if isinstance(message, Pingpong):
                    await ws.send(raw)
                elif isinstance(message, InitReply):
                    if not message.ok:
                        # 토큰이 거절됐을 수 있다. 다음 연결은 토큰을 새로 받는다.
                        self._renew_token = True
                        raise _Failed("init_rejected")
                    return

    async def _receive(self, ws: Socket) -> None:
        idle = self._tuning.idle_timeout
        loop = asyncio.get_running_loop()
        try:
            async with asyncio.timeout(idle) as deadline:
                while True:
                    raw = await ws.recv()
                    deadline.reschedule(loop.time() + idle)
                    await self._handle(ws, raw)
        except ConnectionClosed:
            if self._desired:
                raise
        except TimeoutError:
            raise _Failed("idle") from None

    async def _handle(self, ws: Socket, raw: str | bytes) -> None:
        now = self._clock()
        self._last_seen = now
        message = parse(raw)
        if isinstance(message, Data):
            self._seq += 1
            self._buffer.add(message.sub, kst_minute(now), (self._seq, *message.values))
            if message.sub.stream == "tick":
                self._counts.tick += 1
            else:
                self._counts.book += 1
        elif isinstance(message, Pingpong):
            await ws.send(raw)
            self._counts.pings += 1
        elif isinstance(message, SubReply):
            self._on_reply(message, now)
        elif isinstance(message, Skipped):
            setattr(self._counts, message.reason, getattr(self._counts, message.reason) + 1)
        else:
            self._counts.unknown += 1

    def _on_reply(self, reply: SubReply, now: datetime) -> None:
        sub = reply.sub
        if sub is None:
            self._counts.unknown += 1
            return
        # 해제 확인이거나, 확인이 오기 전에 이미 뺀 구독이다.
        if reply.action != "A" or sub not in self._desired:
            return
        if reply.ok:
            self._live.add(sub)
            self._failed.pop(sub, None)
            self._store.close_gap(self._user, sub, now)
            self._store.write(self._user, [], [sub], now)
            log.info("사용자 %s 구독 %s %s", self._user, sub.symbol, sub.stream)
            return
        message = self._hide(reply.message)
        self._failed[sub] = message
        self._store.open_gap(self._user, sub, self._sent_at.get(sub, now), "subscribe_failed")
        log.warning("사용자 %s 구독 거절 %s %s: %s", self._user, sub.symbol, sub.stream, message)

    async def _converge(self, ws: Socket) -> None:
        """원하는 집합과 이 연결에 보낸 구독의 차이만 보낸다. 해제를 먼저 보내 20건 안에 든다."""
        for sub in sorted(self._requested - self._desired):
            self._requested.discard(sub)
            await ws.send(subscription(sub, on=False))
        for sub in sorted(self._desired - self._requested):
            self._requested.add(sub)
            self._sent_at[sub] = self._clock()
            await ws.send(subscription(sub, on=True))

    def _lost(self) -> None:
        for sub in self._live:
            self._store.open_gap(self._user, sub, self._last_seen, "disconnect")
        self._live.clear()
        self._requested.clear()

    async def _flush_loop(self) -> None:
        while not self._closing.is_set():
            try:
                await asyncio.wait_for(self._closing.wait(), self._tuning.flush_every)
            except TimeoutError:
                await self._flush(final=False)

    async def _flush(self, final: bool) -> None:
        """분이 바뀌었으면 지난 분의 묶음을 저장한다. final이면 열린 분까지."""
        async with self._flush_lock:
            now = self._clock()
            minute = kst_minute(now)
            if not final and minute == self._flushed_minute:
                return
            self._flushed_minute = minute
            blocks = self._buffer.take(None if final else minute)
            until = now if final else minute
            live = sorted(self._live)
            if blocks or live:
                # 누르기와 쓰기는 이벤트 루프 밖에서 한다. 그동안에도 받기는 이어진다.
                await asyncio.to_thread(self._store.write, self._user, blocks, live, until)
