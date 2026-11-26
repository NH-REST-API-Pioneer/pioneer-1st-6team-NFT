"""실행 하나. asyncio 작업 하나가 그 실행의 모든 일을 차례로 한다.

- 이벤트 넣기, 출력 받기, 주문, 취소, 체결 찾기, 멈춤은 모두 이 작업 안에서만 일어난다. 그래서 한
  실행의 출력·체결·주문이 서로 끼어들지 않는다(실행마다 잠금을 두는 대신 일을 한 줄로 세웠다).
  봉 받기와 실시간 구독 읽기는 따로 돌며 받은 것을 크기 1인 우편함에 넣기만 한다. 전략이 밀리면
  구독이 쌓이다 수신기가 그 구독만 끊는다(`slow_consumer`). 수신기는 기다리지 않는다.
- 넣는 순서는 백테스트와 같다: 체결(그 체결을 낳은 주문 뒤 처음 본 것)이 다음 시장 이벤트보다
  먼저, 봉은 `feed_order`, 틱 실행의 일봉은 그날 뒤 첫 체결·호가 바로 앞(`place_days`와 같은 뜻).
- 출력(자동 모드): 종목마다 이 실행의 걸린 주문을 먼저 취소하고 끝난 것(취소·체결)을 주문 내역으로
  확인한 뒤, 목표와 NH 포지션(새로 읽음)의 차이만큼을 한 번에 1회 수량까지 `place_auto`로 낸다. 남은
  차이는 다음 이벤트에서 이어 낸다. 주문 가능 시간 밖이면 목표를 두고 다음 개장에 낸다.
- 제안 모드는 목표를 종목마다 가장 최근 것 하나로 기록만 한다.
- 멈춤(`Stop`): 상태를 먼저 `stopped(사유)`로 바꾸고, 걸린 주문 취소(최선), 세션 닫기, 구독 해제.
  늘리는 주문은 새로 내지 않는다. 손실 한도·정리면 정리 주문(시장가, 0까지)을 낸 뒤 멈춘다.
- 예상 못 한 예외는 그 실행만 `sandbox_failed`로 멈춘다. 다른 실행에는 닿지 않는다.
"""

import asyncio
import logging
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from nh_mcp.domain import (
    BarEvent,
    BookEvent,
    FillEvent,
    JsonValue,
    MarketEvent,
    OrderRequest,
    Output,
    Position,
    RunSnapshot,
    SessionFailed,
    SessionJob,
    StrategyEvent,
    Target,
    TickEvent,
    UserId,
)
from nh_mcp.runner import machine
from nh_mcp.runner.bars import BarPlan, BarPoller, Bars, KeyGone, Stale
from nh_mcp.runner.book import Resting, RunBook, track
from nh_mcp.runner.timing import (
    FILL_POLL,
    MISSING_LIMIT,
    OPEN_DELAY,
    SETTLE_LIMIT,
    STALE_GAP,
    TOUCH_EVERY,
    Timer,
    in_session,
    next_open,
    session_start,
    today,
)
from nh_mcp.sandbox.client import SandboxBusy, SandboxDown, SessionGone, SessionHandle
from nh_mcp.services.orders import (
    InvalidCancel,
    LimitsNotSet,
    MarketClosed,
    NotRunOrder,
    OrdersDisabled,
    OrderService,
    RiskBlocked,
    UnknownState,
    same_order_no,
)
from nh_mcp.services.queries import BrokerRejected, KeyMissing, Unavailable, UnknownSymbol
from nh_mcp.services.runs import (
    FeedBack,
    FeedEnded,
    FeedLost,
    LiveFeed,
    LiveFeeds,
    RunMarket,
    Standing,
    SymbolInfo,
)
from nh_mcp.store import orders, strategy_runs
from nh_mcp.store.strategy_runs import EventKind, RunEvent, StopReason, StrategyRun

log = logging.getLogger(__name__)

# 연달아 이만큼 NH가 거부하면 멈춘다.
MAX_REJECTIONS = 3
# 손실 한도 정리 주문을 내 보는 횟수(장 밖이면 개장까지 기다리는 것도 한 번).
LIQUIDATE_TRIES = 3
# 계좌를 읽지 못해 미룬 목표를 다시 보는 간격.
RETRY_SOON = timedelta(seconds=10)
# 기록에 남기는 전략 쪽 문구의 길이.
MAX_DETAIL = 500


class Sessions(Protocol):
    """띄워 두는 전략 컨테이너를 여는 곳(`sandbox/client.py`의 SandboxClient)."""

    async def open_session(self, job: SessionJob) -> SessionHandle | SessionFailed: ...

    async def feed(self, handle: SessionHandle, event: StrategyEvent) -> Output | SessionFailed: ...

    async def close(self, handle: SessionHandle) -> None: ...


@dataclass(frozen=True)
class Deps:
    conn: sqlite3.Connection
    timer: Timer
    market: RunMarket
    feeds: LiveFeeds
    orders: OrderService
    sessions: Sessions


class Stop(Exception):
    """이 실행을 멈춘다. 깊은 곳에서 던지면 실행 작업이 받아 정리한다."""

    def __init__(self, reason: StopReason, detail: str = "") -> None:
        super().__init__(reason)
        self.reason: StopReason = reason
        self.detail = detail[:MAX_DETAIL]


@dataclass(frozen=True)
class Halt:
    reason: StopReason


type Item = TickEvent | BookEvent | FeedLost | FeedBack | FeedEnded | Bars | Stale | KeyGone | Halt


def _failure(failed: SessionFailed) -> Stop:
    reason: StopReason = "invalid_output" if failed.reason == "invalid_output" else "sandbox_failed"
    return Stop(reason, f"{failed.reason}: 전략 코드가 낸 내용: {failed.detail}")


async def feed_one(sessions: Sessions, handle: SessionHandle, event: StrategyEvent) -> Output:
    """이벤트 하나를 넣고 출력을 받는다. 세션이 끝났거나 실행기에 닿지 못하면 Stop."""
    try:
        output = await sessions.feed(handle, event)
    except SessionGone:
        raise Stop("sandbox_failed", "전략 세션이 없어졌습니다.") from None
    except (SandboxDown, SandboxBusy) as exc:
        raise Stop("sandbox_failed", f"실행기에 닿지 못했습니다({type(exc).__name__}).") from None
    if isinstance(output, SessionFailed):
        raise _failure(output)
    return output


def _price(value: Decimal | None) -> JsonValue:
    return None if value is None else format(value, "f")


def _targets(output: Mapping[str, Target]) -> dict[str, JsonValue]:
    return {
        s: {"position": t.position, "price": _price(t.price)} for s, t in sorted(output.items())
    }


def _print(event: MarketEvent) -> Decimal | None:
    """평가에 쓰는 마지막 가격. 체결과 봉만(백테스트 체결 계산과 같음), 호가는 쓰지 않는다."""
    match event:
        case BarEvent(bar=bar):
            return bar.close
        case TickEvent(price=price):
            return price
        case BookEvent():
            return None


def _net(positions: list[Position], symbol: str) -> int:
    return sum(
        p.quantity if p.side == "buy" else -p.quantity for p in positions if p.symbol == symbol
    )


@dataclass(frozen=True)
class Started:
    """켜기가 끝난 실행: 세션, 종목 조건, 실시간 구독, 종목마다 이미 넣은 마지막 1분봉 시작 시각."""

    run: StrategyRun
    handle: SessionHandle
    infos: Mapping[str, SymbolInfo]
    live: LiveFeed | None
    last_minutes: dict[str, datetime]


class RunTask:
    def __init__(self, deps: Deps, started: Started, on_exit: Callable[[str], None]) -> None:
        run = started.run
        infos = started.infos
        self.run = run
        self._on_exit = on_exit
        self._deps = deps
        self._conn = deps.conn
        self._timer = deps.timer
        self._handle = started.handle
        self._infos = dict(infos)
        self._live = started.live
        self._last_minutes = started.last_minutes
        self._inbox: asyncio.Queue[Item] = asyncio.Queue(maxsize=1)
        self._getter: asyncio.Task[Item] | None = None
        self._producers: list[asyncio.Task[None]] = []
        self._task: asyncio.Task[None] | None = None
        self._halt: StopReason | None = None
        self._halted = asyncio.Event()
        self._finished = False
        multipliers = {s: info.terms.multiplier for s, info in infos.items()}
        self._book = RunBook(multipliers, today(self._timer.now()))
        self._fills: list[FillEvent] = []
        self._resting: list[Resting] = []
        self._desired: dict[str, Target] = {}
        self._waiting: dict[str, datetime] = {}
        self._gaps: dict[tuple[str, str], datetime] = {}
        self._held_days: list[BarEvent] = []
        self._logged: dict[str, Target] = {}
        self._rejections = 0
        self._next_poll = self._timer.now()
        self._touched: datetime | None = None
        self._tick_run = any(names & {"tick", "book"} for names in run.streams.values())

    @property
    def user_id(self) -> UserId:
        return self.run.user_id

    # ---- 바깥에서 부르는 것 --------------------------------------------------

    def start(self) -> None:
        minutes = sorted(s for s, names in self.run.streams.items() if "minute" in names)
        days = sorted(s for s, names in self.run.streams.items() if "day" in names)
        if minutes or days:
            plan = BarPlan(self.run.user_id, self._infos, minutes, days, self._last_minutes)
            poller = BarPoller(self._deps.market, self._timer, plan, self._inbox.put)
            self._producers.append(asyncio.create_task(poller.run()))
        if self._live is not None:
            self._producers.append(asyncio.create_task(self._read(self._live)))
        self._task = asyncio.create_task(self._main())

    async def halt(self, reason: StopReason) -> None:
        """멈춰 달라고 하고 멈출 때까지 기다린다. 먼저 온 사유가 남는다."""
        if self._halt is None:
            self._halt = reason
        self._halted.set()
        if not self._inbox.full():
            self._inbox.put_nowait(Halt(reason))
        await self.wait()

    async def wait(self) -> None:
        if self._task is not None:
            await asyncio.shield(self._task)

    def standing(self) -> Standing:
        """지금 장부(읽기만). 손익은 날이 바뀐 뒤 첫 이벤트 전까지는 어제 것이다."""
        positions = {symbol: self._book.position(symbol) for symbol in sorted(self.run.streams)}
        return Standing(positions, self._book.pnl())

    async def cancel(self) -> None:
        """서버가 내려간다. 상태는 바꾸지 않는다(다시 뜰 때 일시정지로 바뀐다)."""
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    # ---- 작업 ---------------------------------------------------------------

    async def _read(self, live: LiveFeed) -> None:
        while True:
            item = await live.next()
            await self._inbox.put(item)
            if isinstance(item, FeedEnded):
                return

    async def _main(self) -> None:
        try:
            while not self._finished:
                item = await self._next()
                if self._halt is not None:
                    raise Stop(self._halt)
                await self._handle_item(item)
                await self._checks()
        except Stop as stop:
            await self._finish(stop.reason, stop.detail)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 예외 메시지는 남기지 않는다(키·토큰이 섞일 수 있는 문구를 로그에 쓰지 않는다).
            log.error(
                "전략 %s 실행 중 예상 못 한 오류(%s)로 멈춥니다.",
                self.run.run_id,
                type(exc).__name__,
            )
            await self._finish("sandbox_failed", f"서버 오류({type(exc).__name__})")
        finally:
            self._on_exit(self.run.run_id)
            for task in [*self._producers, *([self._getter] if self._getter else [])]:
                task.cancel()
            await asyncio.gather(*self._producers, return_exceptions=True)

    async def _next(self) -> Item | None:
        """우편함의 다음 것. 그 전에 해야 할 일(체결 확인·만료·끊김·개장)의 시각이 오면 None."""
        if self._getter is None:
            self._getter = asyncio.ensure_future(self._inbox.get())
        sleeper = asyncio.ensure_future(self._timer.sleep_until(self._wake_at()))
        halted = asyncio.ensure_future(self._halted.wait())
        try:
            await asyncio.wait({self._getter, sleeper, halted}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            sleeper.cancel()
            halted.cancel()
        if self._getter.done():
            item = self._getter.result()
            self._getter = None
            return item
        return None

    def _wake_at(self) -> datetime:
        now = self._timer.now()
        times = [self.run.scope.expires_at, *self._waiting.values()]
        if self._resting:
            times.append(self._next_poll)
        times += [
            self._gap_deadline(symbol, since, now) for (symbol, _), since in self._gaps.items()
        ]
        return min(times)

    def _gap_deadline(self, symbol: str, since: datetime, now: datetime) -> datetime:
        """끊긴 구간을 장중으로만 센다. 장중이면 (끊긴 때와 오늘 개장 중 늦은 것) + STALE_GAP,
        장 밖이면 다음 개장 + STALE_GAP이다. 장 밖에서는 멈추지 않는다."""
        session = self._infos[symbol].session
        if in_session(now, session):
            return max(since, session_start(now, session)) + STALE_GAP
        return next_open(now, session) + STALE_GAP

    async def _handle_item(self, item: Item | None) -> None:
        match item:
            case None:
                pass
            case TickEvent() | BookEvent():
                await self._flush_days(item)
                await self._market(item)
            case Bars(events=events):
                await self._bars(events)
            case FeedLost(symbol=symbol, stream=stream, since=since):
                self._gaps.setdefault((symbol, stream), since)
            case FeedBack(symbol=symbol, stream=stream):
                self._gaps.pop((symbol, stream), None)
            case FeedEnded() | Stale() | KeyGone() | Halt():
                self._ended(item)

    def _ended(self, item: FeedEnded | Stale | KeyGone | Halt) -> None:
        """끝내라는 것. 서버가 내려가는 것만 상태를 두고(다시 뜰 때 일시정지) 나머지는 Stop."""
        match item:
            case FeedEnded(reason="server_stop"):
                self._finished = True
            case FeedEnded(reason=reason):
                raise Stop("stale_feed", f"실시간 구독이 끊겼습니다({reason}).")
            case Stale(detail=detail):
                raise Stop("stale_feed", detail)
            case KeyGone():
                raise Stop("key_removed")
            case Halt(reason=reason):
                raise Stop(reason)

    async def _bars(self, events: list[BarEvent]) -> None:
        """봉 실행은 바로 넣고, 틱 실행의 일봉은 그날 뒤 첫 체결·호가까지 둔다."""
        if not self._tick_run:
            for event in events:
                await self._market(event)
            return
        self._held_days.extend(e for e in events if e.interval == "day")
        self._held_days.sort(key=lambda e: (e.bar.at, e.symbol))

    async def _flush_days(self, event: TickEvent | BookEvent) -> None:
        """그날이 끝난 일봉을 그 뒤 첫 체결·호가 바로 앞에 넣는다."""
        day = today(event.at)
        while self._held_days and today(self._held_days[0].bar.at) < day:
            await self._market(self._held_days.pop(0))

    async def _checks(self) -> None:
        """이벤트 하나를 처리한 뒤와 깰 때마다: 만료, 끊김, 체결 확인, 개장 뒤 미룬 목표, 손실."""
        if self._finished:
            return
        now = self._timer.now()
        if now >= self.run.scope.expires_at:
            raise Stop("expired")
        for (symbol, stream), since in self._gaps.items():
            session = self._infos[symbol].session
            if in_session(now, session) and now >= self._gap_deadline(symbol, since, now):
                raise Stop("stale_feed", f"{symbol} {stream} 실시간이 장중 3분 넘게 끊겼습니다.")
        if self._resting and now >= self._next_poll:
            await self._poll()
            await self._drain_fills()
        due = [s for s, when in self._waiting.items() if when <= now]
        for symbol in due:
            self._waiting.pop(symbol, None)
        await self._continue()
        await self._check_loss()

    # ---- 이벤트 넣기 ----------------------------------------------------------

    async def _market(self, event: MarketEvent) -> None:
        await self._drain_fills()
        now = self._timer.now()
        self._book.roll(today(now))
        price = _print(event)
        if price is not None:
            self._book.mark(event.symbol, price)
        output = await feed_one(self._deps.sessions, self._handle, event)
        if self._touched is None or now - self._touched >= TOUCH_EVERY:
            self._touched = now
            strategy_runs.touch(self._conn, self.run.user_id, self.run.run_id, now)
        await self._act(output)
        await self._drain_fills()
        await self._continue()
        await self._check_loss()

    async def _drain_fills(self) -> None:
        """찾아 둔 체결을 찾은 순서대로 넣고, 그 출력도 다른 출력과 똑같이 다룬다."""
        while self._fills:
            fill = self._fills.pop(0)
            detail: dict[str, JsonValue] = {
                "side": fill.side,
                "qty": fill.qty,
                "price": format(fill.price, "f"),
            }
            self._log("fill", detail, fill.symbol)
            await self._act(await feed_one(self._deps.sessions, self._handle, fill))

    async def _act(self, output: Output) -> None:
        if output is None:
            return
        changed = {s: t for s, t in output.items() if self._logged.get(s) != t}
        if changed:
            self._logged.update(changed)
            self._log("output", _targets(changed))
        if self.run.scope.mode == "suggest":
            for symbol, target in sorted(changed.items()):
                self._log("suggestion", _targets({symbol: target}), symbol)
            return
        for symbol, target in sorted(output.items()):
            self._desired[symbol] = target
            self._waiting.pop(symbol, None)
            await self._drive(symbol)

    async def _continue(self) -> None:
        """걸린 주문 없이 목표에 못 미친 종목을 이어 낸다(1회 수량에 잘렸거나 개장을 기다렸다)."""
        for symbol in sorted(self._desired):
            if symbol in self._waiting or self._resting_on(symbol):
                continue
            if self._desired[symbol].position == self._book.position(symbol):
                self._desired.pop(symbol, None)
                continue
            await self._drive(symbol)

    # ---- 주문 ---------------------------------------------------------------

    def _resting_on(self, symbol: str) -> list[Resting]:
        return [r for r in self._resting if r.symbol == symbol]

    async def _drive(self, symbol: str) -> None:
        """그 종목의 걸린 주문을 끝내고, 목표와 NH 포지션의 차이만큼(1회 수량까지) 낸다."""
        target = self._desired.get(symbol)
        if target is None:
            return
        await self._settle(symbol, cancel=True)
        held = await self._held(symbol)
        if held is None:
            # 계좌를 모른다. 목표는 두고 조금 뒤(또는 다음 이벤트에) 다시 본다.
            self._waiting[symbol] = self._timer.now() + RETRY_SOON
            return
        delta = target.position - held
        if delta == 0:
            self._desired.pop(symbol, None)
            return
        order = OrderRequest(
            "domestic",
            symbol,
            "buy" if delta > 0 else "sell",
            "market" if target.price is None else "limit",
            min(abs(delta), self.run.scope.quantity),
            target.price,
        )
        await self._place(order)

    async def _held(self, symbol: str) -> int | None:
        """NH에서 새로 읽은 그 종목의 순포지션. 계좌를 다 읽지 못하면 None."""
        try:
            account = await self._deps.market.account(self.run.user_id)
        except KeyMissing:
            raise Stop("key_removed") from None
        except (Unavailable, BrokerRejected):
            return None
        return _net(account.positions, symbol) if account.complete else None

    def _snapshot(self, symbol: str) -> RunSnapshot:
        return RunSnapshot(
            run_id=self.run.run_id,
            scope=self.run.scope,
            symbols=frozenset(self.run.streams),
            resting_qty=sum(r.signed_rest for r in self._resting_on(symbol)),
            orders_today=self._orders_today(),
            strategy_pnl=self._book.pnl(),
        )

    def _orders_today(self) -> int:
        day = today(self._timer.now())
        return sum(
            1
            for row in orders.list_for(self._conn, self.run.user_id)
            if row.run_id == self.run.run_id and today(row.submitted_at) == day
        )

    async def _place(self, order: OrderRequest, *, closing: bool = False) -> bool:
        """`place_auto`로 낸다. 접수되면 True. 모르는 결과·키 없음·거부 3번·하루 횟수는 Stop."""
        symbol = order.symbol
        detail: dict[str, JsonValue] = {
            "side": order.side,
            "type": order.type,
            "qty": order.quantity,
            "price": _price(order.price),
        }
        try:
            stored = await self._deps.orders.place_auto(
                self.run.user_id, self._snapshot(symbol), order
            )
        except MarketClosed:
            opens = next_open(self._timer.now(), self._infos[symbol].session)
            self._waiting[symbol] = opens + OPEN_DELAY
            self._log("warning", detail | {"outcome": "market_closed"}, symbol)
            return False
        except UnknownState:
            self._log("order", detail | {"outcome": "unknown"}, symbol)
            raise Stop("unknown_order", f"{symbol} 주문의 결과를 알 수 없습니다.") from None
        except KeyMissing:
            raise Stop("key_removed") from None
        except BrokerRejected as exc:
            self._rejections += 1
            self._log("order", detail | {"outcome": "rejected", "msg_cd": exc.code}, symbol)
            if self._rejections >= MAX_REJECTIONS:
                raise Stop(
                    "rejections", f"NH가 주문을 {MAX_REJECTIONS}번 연달아 거부했습니다."
                ) from None
            self._check_exhausted(closing)
            return False
        except RiskBlocked as exc:
            self._desired.pop(symbol, None)
            self._log("warning", detail | {"outcome": "blocked", "rule": exc.rule}, symbol)
            self._check_exhausted(closing)
            return False
        except Unavailable:
            self._waiting[symbol] = self._timer.now() + RETRY_SOON
            self._log("warning", detail | {"outcome": "unavailable"}, symbol)
            return False
        except (UnknownSymbol, LimitsNotSet, OrdersDisabled) as exc:
            self._desired.pop(symbol, None)
            self._log(
                "warning", detail | {"outcome": "blocked", "error": type(exc).__name__}, symbol
            )
            return False
        self._rejections = 0
        self._resting.append(
            Resting(
                order_id=stored.id,
                order_no=stored.nh_order_no or "",
                symbol=symbol,
                side=order.side,
                quantity=order.quantity,
                placed_at=self._timer.now(),
            )
        )
        self._next_poll = min(self._next_poll, self._timer.now() + FILL_POLL)
        self._log("order", detail | {"outcome": "accepted", "order_id": stored.id}, symbol)
        self._check_exhausted(closing)
        return True

    def _check_exhausted(self, closing: bool) -> None:
        """하루 주문 횟수를 다 썼으면 멈춘다(정리 주문은 승인 범위 규칙이 따로 받아 준다)."""
        if not closing and self._orders_today() >= self.run.scope.daily_orders:
            raise Stop("daily_orders", f"하루 주문 {self.run.scope.daily_orders}번을 다 썼습니다.")

    async def _settle(self, symbol: str, *, cancel: bool) -> None:
        """그 종목의 걸린 주문이 끝날 때까지(취소·체결) 주문 내역으로 본다. cancel이면 먼저 취소.

        SETTLE_LIMIT 안에 끝나지 않으면 모르는 주문이다.
        """
        mine = self._resting_on(symbol)
        if not mine:
            return
        if cancel:
            for order in mine:
                await self._cancel(order)
        deadline = self._timer.now() + SETTLE_LIMIT
        while True:
            await self._poll()
            if not self._resting_on(symbol):
                return
            now = self._timer.now()
            if now >= deadline:
                raise Stop("unknown_order", f"{symbol} 걸린 주문이 끝났는지 확인하지 못했습니다.")
            await self._timer.sleep_until(min(now + FILL_POLL, deadline))

    async def _cancel(self, order: Resting) -> None:
        try:
            await self._deps.orders.cancel_run_order(
                self.run.user_id, self.run.run_id, order.order_id
            )
        except UnknownState:
            raise Stop(
                "unknown_order", f"주문 {order.order_no} 취소의 결과를 알 수 없습니다."
            ) from None
        except KeyMissing:
            raise Stop("key_removed") from None
        except (InvalidCancel, NotRunOrder, BrokerRejected, Unavailable):
            # 이미 끝났거나 NH가 받지 않았다. 끝났는지는 주문 내역이 정한다.
            pass

    async def _poll(self) -> None:
        """주문 내역을 보고 새 체결을 장부에 적고 넣을 체결로 쌓는다. 끝난 주문은 뺀다."""
        now = self._timer.now()
        self._next_poll = now + FILL_POLL
        try:
            rows = await self._deps.market.orders(self.run.user_id)
        except KeyMissing:
            raise Stop("key_removed") from None
        except (Unavailable, BrokerRejected):
            return
        found = track(self._resting, rows, now, MISSING_LIMIT, same_order_no)
        for fill in found.fills:
            for qty in self._book.fill(fill.symbol, fill.side, fill.qty, fill.price):
                self._fills.append(FillEvent(fill.symbol, now, fill.side, qty, fill.price))
        self._resting = [r for r in self._resting if r.order_id not in found.done]
        if found.problem is not None:
            raise Stop("unknown_order", found.problem)

    async def _check_loss(self) -> None:
        pnl = self._book.pnl()
        cap = self.run.scope.daily_loss
        if pnl is None or pnl > -cap:
            return
        self._log("warning", {"outcome": "daily_loss", "pnl": format(pnl, "f")})
        if self.run.scope.on_loss == "liquidate":
            await self._liquidate()
        raise Stop(
            "daily_loss", f"이 전략의 오늘 손익 {format(pnl, 'f')}원이 손실 한도에 닿았습니다."
        )

    async def _liquidate(self) -> None:
        """종목마다 걸린 주문을 끝내고 NH 포지션을 시장가로 0까지 정리한다. 장 밖이면 개장을 기다림.

        정리 주문은 줄이는 주문이라 승인 범위 규칙이 하루 횟수·1회 수량과 상관없이 받는다.
        """
        self._desired.clear()
        for symbol in sorted(self.run.streams):
            await self._settle(symbol, cancel=True)
            if not await self._close_out(symbol):
                self._log("warning", {"outcome": "not_liquidated"}, symbol)

    async def _close_out(self, symbol: str) -> bool:
        """그 종목을 0으로. 이미 0이거나 정리 주문이 접수되면 True."""
        for _ in range(LIQUIDATE_TRIES):
            held = await self._held(symbol)
            if held == 0:
                return True
            if held is not None:
                side = "sell" if held > 0 else "buy"
                order = OrderRequest("domestic", symbol, side, "market", abs(held), None)
                if await self._place(order, closing=True):
                    await self._settle(symbol, cancel=False)
                    return True
            when = self._waiting.pop(symbol, None)
            if when is None or not await self._sleep_or_halt(when):
                return False
        return False

    async def _sleep_or_halt(self, when: datetime) -> bool:
        """when까지 기다린다. 그사이 멈춰 달라고 하면 False."""
        sleeper = asyncio.ensure_future(self._timer.sleep_until(when))
        halted = asyncio.ensure_future(self._halted.wait())
        try:
            await asyncio.wait({sleeper, halted}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            sleeper.cancel()
            halted.cancel()
        return not self._halted.is_set()

    # ---- 멈춤 ---------------------------------------------------------------

    async def _finish(self, reason: StopReason, detail: str) -> None:
        """멈춘다. 상태를 먼저 바꾸고, 정리는 하나가 실패해도 나머지를 한다."""
        if self._finished:
            return
        self._finished = True
        user, run_id = self.run.user_id, self.run.run_id
        try:
            machine.advance(self._conn, self.run, "stopped", reason, self._timer.now())
        except machine.IllegalTransition:
            log.warning("전략 %s은 이미 멈췄습니다.", run_id)
        for task in self._producers:
            task.cancel()
        # 손실 한도에서 정리했으면 정리 주문은 두고, 그 밖에는 걸린 주문을 모두 취소해 본다.
        if reason != "daily_loss" or self.run.scope.on_loss == "hold":
            await self._cancel_all()
        try:
            await self._deps.sessions.close(self._handle)
            strategy_runs.set_session(self._conn, user, run_id, None)
        except Exception as exc:
            log.warning("전략 %s의 세션을 닫지 못했습니다(%s).", run_id, type(exc).__name__)
        try:
            if self._live is not None:
                await self._deps.feeds.close(self._live)
            self._log("stop", {"reason": reason, "detail": detail})
        except Exception as exc:
            log.warning("전략 %s 정리 중 오류(%s).", run_id, type(exc).__name__)

    async def _cancel_all(self) -> None:
        """걸린 주문을 모두 취소해 본다(최선). 새 주문은 내지 않는다."""
        for order in list(self._resting):
            try:
                await self._deps.orders.cancel_run_order(
                    self.run.user_id, self.run.run_id, order.order_id
                )
            except Exception as exc:
                self._log(
                    "warning",
                    {"outcome": "cancel_failed", "error": type(exc).__name__},
                    order.symbol,
                )

    def _log(
        self, kind: EventKind, detail: dict[str, JsonValue], symbol: str | None = None
    ) -> None:
        event = RunEvent(self._timer.now(), kind, symbol, detail)
        strategy_runs.log(self._conn, self.run.user_id, self.run.run_id, event)
