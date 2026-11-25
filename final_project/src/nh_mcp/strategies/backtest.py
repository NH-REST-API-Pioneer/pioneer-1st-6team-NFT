"""목표 포지션으로 체결을 흉내 내고 성과를 계산한다. 이벤트 하나씩 나아가는 순수 계산이다.

주문은 판단 시각 이후에 시작한 그 종목의 체결 이벤트(봉·틱)에서만 체결된다. 호가로는 체결하지
않는다. 시장가는 그 이벤트 가격(봉은 시가)에서 불리한 쪽으로 1틱, 지정가는 체결가가 지정가를
넘어설 때만 지정가에 전량이다.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal

from nh_mcp.domain import (
    BarEvent,
    BookEvent,
    FillEvent,
    MarketEvent,
    Output,
    Side,
    StrategyEvent,
    SymbolTerms,
    TickEvent,
)

# 승률은 소수 넷째 자리까지 쓴다.
_RATE_STEP = Decimal("0.0001")
_ZERO = Decimal(0)


@dataclass(frozen=True)
class Trade:
    """끝난 거래 하나. 포지션이 0을 떠나 다시 0이 되거나 뒤집힐 때까지다.

    quantity는 거래 중 가장 컸던 계약 수, 가격은 진입·청산 체결의 평균이다.
    pnl은 이 거래의 모든 체결 수수료를 뺀 값이다.
    """

    symbol: str
    side: Side
    quantity: int
    entry_at: datetime
    entry_price: Decimal
    exit_at: datetime
    exit_price: Decimal
    pnl: Decimal


@dataclass(frozen=True)
class Holding:
    """끝까지 정리되지 않은 포지션. 마지막 체결가로 평가만 하고 거래로 치지 않는다.

    entry_price는 지금 포지션의 평균 단가다. realized_pnl은 이 거래에서 이미 줄인 계약의
    확정 손익(수수료 전), fees는 이 거래의 체결 수수료다.
    """

    symbol: str
    side: Side
    quantity: int
    entry_at: datetime
    entry_price: Decimal
    mark_price: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal
    fees: Decimal


@dataclass(frozen=True)
class PendingOrder:
    """아직 체결되지 않은 주문. 목표 포지션과 지정가(None이면 시장가)를 담는다."""

    symbol: str
    target: int
    price: Decimal | None
    decided_at: datetime


@dataclass(frozen=True)
class SymbolSummary:
    symbol: str
    trades: int
    fills: int
    net_pnl: Decimal
    fees: Decimal


@dataclass(frozen=True)
class EventReport:
    """net_pnl은 끝난 거래의 합이다. fees에는 남은 포지션의 수수료도 들어 있다."""

    trades: list[Trade]
    fills: int
    win_rate: Decimal | None
    net_pnl: Decimal
    max_drawdown: Decimal
    fees: Decimal
    open_positions: list[Holding]
    pending: list[PendingOrder]
    symbols: list[SymbolSummary]


def decision_time(event: StrategyEvent) -> datetime:
    """이 이벤트에 낸 출력의 판단 시각. 봉은 봉이 끝난 시각, 나머지는 이벤트 시각이다."""
    match event:
        case BarEvent(interval="minute", bar=bar):
            return bar.at + timedelta(minutes=1)
        case BarEvent(bar=bar):
            return bar.at + timedelta(days=1)
        case TickEvent(at=at) | BookEvent(at=at) | FillEvent(at=at):
            return at


@dataclass(frozen=True)
class _Print:
    """체결 이벤트 하나를 봉 모양으로. 틱은 네 값이 모두 체결가다."""

    at: datetime
    open: Decimal
    low: Decimal
    high: Decimal
    close: Decimal


def _print(event: MarketEvent) -> _Print | None:
    match event:
        case BarEvent(bar=bar):
            return _Print(bar.at, bar.open, bar.low, bar.high, bar.close)
        case TickEvent(at=at, price=price):
            return _Print(at, price, price, price, price)
        case BookEvent():
            return None


@dataclass
class _OpenTrade:
    """진행 중인 거래. cost는 지금 포지션의 평균 단가에 계약 수를 곱한 값이다."""

    direction: int
    entry_at: datetime
    peak: int = 0
    cost: Decimal = _ZERO
    entry_value: Decimal = _ZERO
    entry_qty: int = 0
    exit_value: Decimal = _ZERO
    exit_qty: int = 0
    fees: Decimal = _ZERO

    @property
    def held(self) -> int:
        return self.entry_qty - self.exit_qty


def _side(direction: int) -> Side:
    return "buy" if direction > 0 else "sell"


@dataclass
class _Book:
    """종목 하나의 상태. 진행 중인 거래가 없으면 포지션은 0이다."""

    symbol: str
    terms: SymbolTerms
    last: Decimal = _ZERO
    order: PendingOrder | None = None
    trade: _OpenTrade | None = None
    fills: int = 0
    fees: Decimal = _ZERO

    @property
    def position(self) -> int:
        return 0 if self.trade is None else self.trade.direction * self.trade.held

    def fill(self, at: datetime, direction: int, qty: int, price: Decimal) -> Trade | None:
        """0을 넘지 않는 체결 한 건을 적는다. 포지션이 0이 되면 끝난 거래를 돌려준다."""
        fee = self.terms.fee_per_fill * qty
        self.fills += 1
        self.fees += fee
        if self.trade is None:
            self.trade = _OpenTrade(direction, at)
        trade = self.trade
        trade.fees += fee
        if direction == trade.direction:
            trade.entry_value += price * qty
            trade.entry_qty += qty
            trade.cost += price * qty
            trade.peak = max(trade.peak, trade.held)
            return None
        # 줄여도 평균 단가는 그대로다. 다 줄이면 남은 cost를 통째로 덜어 나눗셈 오차를 없앤다.
        held = trade.held
        trade.cost = _ZERO if qty == held else trade.cost - trade.cost * qty / held
        trade.exit_value += price * qty
        trade.exit_qty += qty
        if trade.held > 0:
            return None
        self.trade = None
        gross = (trade.exit_value - trade.entry_value) * trade.direction
        return Trade(
            symbol=self.symbol,
            side=_side(trade.direction),
            quantity=trade.peak,
            entry_at=trade.entry_at,
            entry_price=trade.entry_value / trade.entry_qty,
            exit_at=at,
            exit_price=trade.exit_value / trade.exit_qty,
            pnl=gross * self.terms.multiplier - trade.fees,
        )

    def open_equity(self) -> Decimal:
        """진행 중 거래의 확정 + 평가 - 수수료. 평가는 마지막 체결가로 한다."""
        t = self.trade
        if t is None:
            return _ZERO
        gross = (t.exit_value - t.entry_value + self.last * t.held) * t.direction
        return gross * self.terms.multiplier - t.fees

    def holding(self) -> Holding | None:
        t = self.trade
        if t is None:
            return None
        held = t.held
        scale = self.terms.multiplier * t.direction
        return Holding(
            symbol=self.symbol,
            side=_side(t.direction),
            quantity=held,
            entry_at=t.entry_at,
            entry_price=t.cost / held,
            mark_price=self.last,
            unrealized_pnl=(self.last * held - t.cost) * scale,
            realized_pnl=(t.exit_value - t.entry_value + t.cost) * scale,
            fees=t.fees,
        )


def _fill_price(order: PendingOrder, p: _Print, direction: int, tick: Decimal) -> Decimal | None:
    if order.price is None:
        return p.open + tick * direction
    crossed = p.low < order.price if direction > 0 else p.high > order.price
    return order.price if crossed else None


class Simulator:
    """체결 계산기. on_market으로 이벤트를, on_output으로 그 이벤트에 낸 출력을 넣는다."""

    def __init__(self, terms: Mapping[str, SymbolTerms]) -> None:
        self._books = {symbol: _Book(symbol, t) for symbol, t in sorted(terms.items())}
        self._trades: list[Trade] = []
        self._closed_pnl = _ZERO
        self._peak = _ZERO  # 시작 자본 0도 최고점에 든다.
        self._max_drawdown = _ZERO

    @property
    def positions(self) -> Mapping[str, int]:
        return {symbol: book.position for symbol, book in self._books.items()}

    def on_market(self, event: MarketEvent) -> list[FillEvent]:
        """걸린 주문을 이 이벤트에서 체결하고 자본을 잰다. 체결은 이 이벤트보다 먼저 전략에 간다."""
        book = self._book(event.symbol)
        fills: list[FillEvent] = []
        p = _print(event)
        if p is not None:
            order = book.order
            if order is not None and p.at >= order.decided_at:
                direction = 1 if order.target > book.position else -1
                price = _fill_price(order, p, direction, book.terms.tick)
                if price is not None:
                    fills = self._fill(book, order.target, p.at, price)
                    book.order = None
            book.last = p.close
        equity = self._closed_pnl + sum((b.open_equity() for b in self._books.values()), _ZERO)
        self._peak = max(self._peak, equity)
        self._max_drawdown = max(self._max_drawdown, self._peak - equity)
        return fills

    def on_output(self, output: Output, decided_at: datetime) -> None:
        """출력에 든 종목마다 걸린 주문을 취소하고, 지금 포지션과 목표가 다르면 새로 건다."""
        if output is None:
            return
        for symbol, target in output.items():
            book = self._book(symbol)
            book.order = (
                None
                if target.position == book.position
                else PendingOrder(symbol, target.position, target.price, decided_at)
            )

    def report(self) -> EventReport:
        books = list(self._books.values())
        trades = list(self._trades)
        wins = sum(1 for t in trades if t.pnl > 0)
        win_rate = (
            (Decimal(wins) / Decimal(len(trades))).quantize(_RATE_STEP, rounding=ROUND_HALF_EVEN)
            if trades
            else None
        )
        return EventReport(
            trades=trades,
            fills=sum(b.fills for b in books),
            win_rate=win_rate,
            net_pnl=self._closed_pnl,
            max_drawdown=self._max_drawdown,
            fees=sum((b.fees for b in books), _ZERO),
            open_positions=[h for b in books if (h := b.holding()) is not None],
            pending=[b.order for b in books if b.order is not None],
            symbols=[
                SymbolSummary(
                    symbol=b.symbol,
                    trades=sum(1 for t in trades if t.symbol == b.symbol),
                    fills=b.fills,
                    net_pnl=sum((t.pnl for t in trades if t.symbol == b.symbol), _ZERO),
                    fees=b.fees,
                )
                for b in books
            ],
        )

    def _book(self, symbol: str) -> _Book:
        book = self._books.get(symbol)
        if book is None:
            raise ValueError(f"체결 조건이 없는 종목이다: {symbol}")
        return book

    def _fill(self, book: _Book, target: int, at: datetime, price: Decimal) -> list[FillEvent]:
        """목표까지 체결한다. 0을 넘어 뒤집으면 청산과 진입 두 건이고 가격은 같다."""
        position = book.position
        direction = 1 if target > position else -1
        if position * direction < 0 < target * direction:
            legs = [abs(position), abs(target)]
        else:
            legs = [abs(target - position)]
        fills: list[FillEvent] = []
        for qty in legs:
            closed = book.fill(at, direction, qty, price)
            if closed is not None:
                self._trades.append(closed)
                self._closed_pnl += closed.pnl
            fills.append(FillEvent(book.symbol, at, _side(direction), qty, price))
        return fills


def simulate_events(
    events: Sequence[MarketEvent], outputs: Sequence[Output], terms: Mapping[str, SymbolTerms]
) -> EventReport:
    """기록한 실행을 다시 계산한다.

    출력은 전략에 넣은 순서와 한 줄씩 맞아야 한다. 이벤트마다 그 이벤트가 낸 체결들이 먼저,
    이벤트 자신이 다음이다.
    """
    sim = Simulator(terms)
    used = 0
    for event in events:
        for fed in [*sim.on_market(event), event]:
            if used == len(outputs):
                raise ValueError(f"출력이 {used}개뿐이라 넣은 이벤트보다 적다")
            sim.on_output(outputs[used], decision_time(fed))
            used += 1
    if used != len(outputs):
        raise ValueError(f"출력 {len(outputs)}개가 넣은 이벤트 {used}개보다 많다")
    return sim.report()
