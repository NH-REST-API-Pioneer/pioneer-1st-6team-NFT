"""자동 실행기(runner)가 서비스 계층을 쓰는 입구.

runner는 markets·realtime·nh·guard를 직접 부르지 않는다(pyproject 계약). 여기서 승인 내용을 읽고,
종목 조건·계좌·오늘 주문 내역·봉을 서비스 예외로 가져오고, 수신기 구독을 runner가 쓰는 모양으로
바꿔 준다. 주문은 여기가 아니라 `OrderService.place_auto`·`cancel_run_order`로만 나간다.

도구(W5)는 runner를 import하지 않는다. `StrategyRuns`가 도구가 부를 모양이고, 서버 조립이 runner를
넣어 준다. 켜기를 거절하면 `StartRefused`다.
"""

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Literal, Protocol, cast

from nh_mcp.domain import (
    KST,
    AccountState,
    BarEvent,
    BookEvent,
    Candle,
    JsonValue,
    MarketEvent,
    ModelHash,
    Session,
    Side,
    StrategyScope,
    Stream,
    Streams,
    SymbolTerms,
    TickEvent,
    UserId,
    model_hash,
)
from nh_mcp.markets.domestic import CandleSeries, DomesticMarket, Interval
from nh_mcp.markets.domestic_products import DomesticProducts
from nh_mcp.realtime.feed import FeedClosed, FeedGap, FeedResumed, Subscription, recorded_events
from nh_mcp.realtime.recorder import BadSymbol, TooManySubscriptions
from nh_mcp.services.backtest import check_streams, params_key, place_days
from nh_mcp.services.queries import QueryService, call_nh
from nh_mcp.store.ticks import RecordStream, Sub, TickStore

_STREAMS: tuple[Stream, ...] = ("tick", "book", "minute", "day")


class StartRefused(Exception):
    """전략을 켜지 않았다. rule은 도구가 가르는 값, message는 사용자에게 보여 줄 문장이다."""

    def __init__(self, rule: str, message: str) -> None:
        super().__init__(message)
        self.rule = rule
        self.message = message


@dataclass(frozen=True)
class Standing:
    """도는 실행이 체결로 센 종목별 포지션과 오늘 손익(확정 + 마지막 가격 평가, 모르면 None)."""

    positions: dict[str, int]
    pnl: Decimal | None


class StrategyRuns(Protocol):
    """도구가 부르는 자동 실행 입구. 서버 조립이 runner를 넣는다."""

    async def start(
        self, user_id: UserId, approval: str, code: str, model: bytes | None
    ) -> str: ...

    async def stop(self, user_id: UserId, run_id: str) -> bool: ...

    def standing(self, user_id: UserId, run_id: str) -> Standing | None:
        """이 프로세스에서 도는 그 사용자의 실행이면 지금 장부, 아니면 None."""
        ...


# ---- 승인 내용 -----------------------------------------------------------


@dataclass(frozen=True)
class Approved:
    """소진한 `strategy` 확인 토큰에 담긴 것(`services/strategy.py`의 `_payload`를 거꾸로 읽음)."""

    streams: Streams
    code_hash: str
    params: dict[str, JsonValue]
    model_hash: ModelHash | None
    scope: StrategyScope


def _reject_float(_: str) -> JsonValue:
    raise ValueError("파라미터에 실수가 있습니다.")


def _object(raw: object) -> dict[str, object] | None:
    return cast(dict[str, object], raw) if isinstance(raw, dict) else None


def _streams(raw: object) -> Streams | None:
    values = _object(raw)
    if values is None:
        return None
    out: dict[str, frozenset[Stream]] = {}
    for symbol, names in values.items():
        if not isinstance(names, list):
            return None
        listed = cast(list[object], names)
        picked: frozenset[Stream] = frozenset(s for s in _STREAMS if s in listed)
        if len(picked) != len(listed):
            return None
        out[symbol] = picked
    return out if check_streams(out) is None else None


def _scope(raw: object) -> StrategyScope | None:
    values = _object(raw)
    if values is None:
        return None
    quantity, cap = values.get("quantity"), values.get("max_position")
    orders, loss, until = (
        values.get("daily_orders"),
        values.get("daily_loss"),
        values.get("expires_at"),
    )
    on_loss, mode = values.get("on_loss"), values.get("mode")
    if (
        type(quantity) is not int
        or type(cap) is not int
        or type(orders) is not int
        or not isinstance(loss, str)
        or not isinstance(until, str)
        or on_loss not in ("liquidate", "hold")
        or mode not in ("auto", "suggest")
    ):
        return None
    try:
        daily_loss = Decimal(loss)
        expires_at = datetime.fromisoformat(until)
    except (InvalidOperation, ValueError):
        return None
    if not daily_loss.is_finite() or daily_loss <= 0 or expires_at.utcoffset() is None:
        return None
    if quantity < 1 or cap < 1 or orders < 1:
        return None
    return StrategyScope(
        quantity,
        cap,
        orders,
        daily_loss,
        expires_at,
        "liquidate" if on_loss == "liquidate" else "hold",
        "auto" if mode == "auto" else "suggest",
    )


def _params(raw: object) -> dict[str, JsonValue] | None:
    """정렬한 JSON 문자열로 저장한 파라미터. 실수가 있거나 정렬 모양이 다르면 None."""
    if not isinstance(raw, str):
        return None
    try:
        values = _object(json.loads(raw, parse_float=_reject_float))
    except ValueError:
        return None
    typed = None if values is None else cast(dict[str, JsonValue], values)
    return typed if typed is not None and params_key(typed) == raw else None


def _model(raw: object) -> tuple[ModelHash | None] | None:
    """모델 해시(없으면 (None,)). 모양이 틀리면 None."""
    if raw is None:
        return (None,)
    hashed = model_hash(raw) if isinstance(raw, str) else None
    return None if hashed is None else (hashed,)


def parse_approval(payload: str) -> Approved | None:
    """우리가 쓴 승인 내용을 다시 읽는다. 모양이 하나라도 다르면 None(켜지 않는다)."""
    try:
        values = _object(json.loads(payload))
    except ValueError:
        return None
    if values is None or values.get("market") != "domestic":
        return None
    streams, scope = _streams(values.get("streams")), _scope(values.get("scope"))
    code_hash, params = values.get("code_hash"), _params(values.get("params"))
    model = _model(values.get("model_hash"))
    if streams is None or scope is None or params is None or model is None:
        return None
    if not isinstance(code_hash, str):
        return None
    return Approved(streams, code_hash, params, model[0], scope)


# ---- 종목 조건·계좌·주문 내역·봉 -------------------------------------------


@dataclass(frozen=True)
class SymbolInfo:
    """종목 하나의 체결 조건(틱·승수, 수수료는 모름이라 0)과 거래 시간."""

    terms: SymbolTerms
    session: Session


type NHOrderState = Literal["open", "filled", "cancelled", "rejected"]


@dataclass(frozen=True)
class NHOrder:
    """오늘 주문 내역의 신규 주문 한 행.

    filled는 체결수량(`occurs1_exec_qty`), filled_price는 체결가격(`occurs1_exec_pric`)이다
    (명세 `domestic_order-history.md`). 체결가격이 체결수량 전체의 평균인지는 녹화로 본다(Task 9).
    """

    order_no: str
    symbol: str
    side: Side
    quantity: int
    filled: int
    filled_price: Decimal | None
    remaining: int
    state: NHOrderState


_STATES: dict[str, NHOrderState] = {
    "accepted": "open",
    "filled": "filled",
    "cancelled": "cancelled",
    "rejected": "rejected",
}


@dataclass(frozen=True)
class RunMarket:
    queries: QueryService
    domestic: DomesticMarket
    products: DomesticProducts

    async def symbol(self, user_id: UserId, symbol: str) -> SymbolInfo | None:
        """종목 마스터와 품목 정보로 만든 조건. 틱·거래 시간을 모르면 None(켜지 않는다).

        종목이 없으면 UnknownSymbol, 마스터를 못 받으면 Unavailable.
        """
        contract = await self.queries.contract(symbol)
        rules = await self.products.rules_for(user_id, contract)
        if rules is None:
            return None
        return SymbolInfo(
            SymbolTerms(rules.tick_size, contract.multiplier, Decimal(0)), rules.session
        )

    async def account(self, user_id: UserId) -> AccountState:
        return await self.queries.account(user_id)

    async def bars(
        self,
        user_id: UserId,
        symbol: str,
        interval: Interval,
        period: tuple[date, date],
        limit: int,
    ) -> list[Candle]:
        series = await self.queries.candle_series(user_id, symbol, interval, period, limit)
        return series.candles

    async def orders(self, user_id: UserId) -> list[NHOrder]:
        """오늘 주문 내역의 신규 주문들(정정·취소 행과 구분을 모르는 행은 뺀다)."""
        rows = await call_nh(self.domestic.todays_orders(user_id))
        return [
            NHOrder(
                order_no=row.order.order_no,
                symbol=row.order.symbol,
                side=row.order.side,
                quantity=row.order.quantity,
                filled=row.order.filled_quantity,
                filled_price=row.order.filled_price,
                remaining=row.order.remaining_quantity,
                state=_STATES[row.state],
            )
            for row in rows
            if row.division == "new"
        ]


# ---- 실시간 체결·호가 -------------------------------------------------------


@dataclass(frozen=True)
class FeedLost:
    """(종목, 흐름)이 since부터 들어오지 않는다."""

    symbol: str
    stream: RecordStream
    since: datetime


@dataclass(frozen=True)
class FeedBack:
    symbol: str
    stream: RecordStream
    at: datetime


@dataclass(frozen=True)
class FeedEnded:
    """구독이 끝났다. slow_consumer는 전략이 밀려 끊긴 것, server_stop은 서버가 내려간 것."""

    reason: Literal["slow_consumer", "unsubscribed", "server_stop"]


type LiveItem = TickEvent | BookEvent | FeedLost | FeedBack | FeedEnded


class Subscriber(Protocol):
    """수신기(`realtime/recorder.py`의 Recorder)에서 쓰는 것."""

    async def subscribe(
        self, user_id: UserId, owner: str, subs: frozenset[Sub]
    ) -> Subscription: ...

    async def unsubscribe(self, handle: Subscription) -> None: ...

    @property
    def store(self) -> TickStore: ...


class LiveFeed:
    """전략 하나의 실시간 구독. next는 수신기가 넘긴 순서대로, FeedEnded가 마지막이다."""

    def __init__(self, handle: Subscription) -> None:
        self.handle = handle

    async def next(self) -> LiveItem:
        item = await anext(self.handle)
        match item:
            case FeedGap(sub=sub, start=start):
                return FeedLost(sub.symbol, sub.stream, start)
            case FeedResumed(sub=sub, at=at):
                return FeedBack(sub.symbol, sub.stream, at)
            case FeedClosed(reason=reason):
                return FeedEnded(reason)
            case TickEvent() | BookEvent():
                return item


@dataclass(frozen=True)
class LiveFeeds:
    recorder: Subscriber

    async def open(self, user_id: UserId, owner: str, subs: frozenset[Sub]) -> LiveFeed:
        """구독을 연다. 구독 수가 넘치거나 수신기가 꺼졌으면 StartRefused(아무것도 바뀌지 않음)."""
        try:
            return LiveFeed(await self.recorder.subscribe(user_id, owner, subs))
        except TooManySubscriptions as exc:
            raise StartRefused("too_many_subscriptions", str(exc)) from None
        except BadSymbol:
            raise StartRefused("invalid", "종목코드 모양이 아닙니다.") from None
        except RuntimeError:
            raise StartRefused("unavailable", "시세 수신기가 켜지지 않았습니다.") from None

    async def close(self, feed: LiveFeed) -> None:
        await self.recorder.unsubscribe(feed.handle)

    async def recorded(
        self, user_id: UserId, subs: frozenset[Sub], day: date, before: datetime, limit: int
    ) -> list[TickEvent | BookEvent]:
        """이 사용자가 그날 저장한 체결·호가 중 before 전의 것, 가장 최근 limit개(받은 순서)."""
        start = datetime.combine(day, time(), KST)
        recorder = self.recorder

        def read() -> list[TickEvent | BookEvent]:
            window = recorder.store.latest(user_id, subs, start, before, limit)
            return [e for e in recorded_events(window.rows) if e.at < before]

        return await asyncio.to_thread(read)


def with_days(
    recorded: list[TickEvent | BookEvent], days: Mapping[str, list[Candle]]
) -> list[MarketEvent]:
    """틱 실행의 데우기: 저장한 체결·호가 사이에 일봉을 백테스트와 같은 자리(`place_days`)에 넣고,
    뒤에 이벤트가 없어 들어가지 못한 일봉은 끝에 붙인다(지난 날의 것이라 데우기에 넣는다)."""
    placed = place_days(
        list(recorded), {s: CandleSeries(candles, True) for s, candles in days.items()}
    )
    used = {(e.symbol, e.bar.at) for e in placed if isinstance(e, BarEvent)}
    rest = sorted(
        (BarEvent(s, "day", c) for s, candles in days.items() for c in candles),
        key=lambda e: (e.bar.at, e.symbol),
    )
    return placed + [e for e in rest if (e.symbol, e.bar.at) not in used]
