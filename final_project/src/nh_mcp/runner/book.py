"""실행 하나의 장부와 체결 찾기. 입출력이 없는 계산이다.

- `RunBook`: 그 실행이 받은 체결로 센 종목별 포지션과 오늘 손익(확정 + 마지막 가격 평가). 수수료는
  NH 체결에서 알 수 없어 넣지 않는다. 날이 바뀌면 남은 포지션을 마지막 가격으로 다시 잡고 확정을
  0으로 돌린다(그래서 손익은 "오늘" 것이다).
- `track`: 그 실행이 낸 주문들을 오늘 주문 내역과 맞춰 새 체결과 끝난 주문을 찾는다. 맞지 않거나
  읽을 수 없는 것이 있으면 이유를 돌려준다(부르는 쪽이 전략을 멈춘다, 모르면 막는다).
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from nh_mcp.domain import Side
from nh_mcp.services.runs import NHOrder

_ZERO = Decimal(0)


def _direction(side: Side) -> int:
    return 1 if side == "buy" else -1


def legs(position: int, side: Side, qty: int) -> list[int]:
    """체결 하나를 0을 넘지 않는 조각으로. 0을 넘어 뒤집으면 청산과 진입 두 조각이다.

    백테스트의 체결 계산(`strategies/backtest.py`의 `Simulator._fill`)과 같은 나눔이다.
    """
    direction = _direction(side)
    if position * direction < 0 < qty - abs(position):
        return [abs(position), qty - abs(position)]
    return [qty]


@dataclass
class _Holding:
    position: int = 0
    # 지금 포지션의 원가(평균 단가 곱하기 계약 수, 부호 없음).
    cost: Decimal = _ZERO
    realized: Decimal = _ZERO
    last: Decimal | None = None


class RunBook:
    def __init__(self, multipliers: Mapping[str, Decimal], day: date) -> None:
        self._multipliers = dict(multipliers)
        self._held = {symbol: _Holding() for symbol in sorted(multipliers)}
        self._day = day

    def position(self, symbol: str) -> int:
        return self._held[symbol].position

    def mark(self, symbol: str, price: Decimal) -> None:
        self._held[symbol].last = price

    def roll(self, day: date) -> None:
        """날이 바뀌었으면 확정을 0으로, 남은 포지션의 원가를 마지막 가격으로 다시 잡는다."""
        if day == self._day:
            return
        self._day = day
        for held in self._held.values():
            held.realized = _ZERO
            if held.position and held.last is not None:
                held.cost = held.last * abs(held.position)

    def fill(self, symbol: str, side: Side, qty: int, price: Decimal) -> list[int]:
        """체결 하나를 적고 0에서 나눈 조각들의 수량을 돌려준다."""
        held = self._held[symbol]
        pieces = legs(held.position, side, qty)
        direction = _direction(side)
        for piece in pieces:
            if held.position == 0 or held.position * direction > 0:
                held.cost += price * piece
            else:
                size = abs(held.position)
                # 다 줄이면 원가를 통째로 덜어 나눗셈 오차를 없앤다.
                spent = held.cost if piece == size else held.cost * piece / size
                sign = 1 if held.position > 0 else -1
                held.realized += (price * piece - spent) * sign * self._multipliers[symbol]
                held.cost -= spent
            held.position += direction * piece
        held.last = price
        return pieces

    def pnl(self) -> Decimal | None:
        """오늘 손익. 포지션이 있는데 마지막 가격을 모르면 None(늘리는 주문이 막힌다)."""
        total = _ZERO
        for symbol, held in self._held.items():
            total += held.realized
            if held.position == 0:
                continue
            if held.last is None:
                return None
            sign = 1 if held.position > 0 else -1
            unrealized = held.last * abs(held.position) - held.cost
            total += unrealized * sign * self._multipliers[symbol]
        return total


# ---- 체결 찾기 -------------------------------------------------------------


@dataclass
class Resting:
    """그 실행이 낸, 아직 끝나지 않은 주문. filled·paid는 지금까지 본 체결수량과 체결 금액."""

    order_id: int
    order_no: str
    symbol: str
    side: Side
    quantity: int
    placed_at: datetime
    filled: int = 0
    paid: Decimal = field(default=_ZERO)

    @property
    def signed_rest(self) -> int:
        return (self.quantity - self.filled) * _direction(self.side)


@dataclass(frozen=True)
class Found:
    """새 체결 한 건(그 주문의 이번 체결 수량과 가격)."""

    order_id: int
    symbol: str
    side: Side
    qty: int
    price: Decimal


@dataclass(frozen=True)
class Tracked:
    fills: list[Found]
    done: frozenset[int]
    problem: str | None


def _problem(order: Resting, row: NHOrder | None, now: datetime, limit: timedelta) -> str | None:
    """이 주문의 내역 행을 믿을 수 없으면 이유. 아직 내역에 없지만 기다릴 만하면 None."""
    if row is None:
        return "주문 내역에 없습니다" if now - order.placed_at > limit else None
    if (row.symbol, row.side, row.quantity) != (order.symbol, order.side, order.quantity):
        return "주문 내역의 종목·방향·수량이 낸 것과 다릅니다"
    if not order.filled <= row.filled <= order.quantity:
        return "체결수량이 줄었거나 주문수량을 넘었습니다"
    if row.filled > order.filled and row.filled_price is None:
        return "체결가격이 없습니다"
    if row.state == "filled" and row.filled != order.quantity:
        return "전체체결인데 체결수량이 주문수량과 다릅니다"
    return None


def track(
    resting: Sequence[Resting],
    rows: Sequence[NHOrder],
    now: datetime,
    missing_limit: timedelta,
    same: Callable[[str, str], bool],
) -> Tracked:
    """주문마다 내역 행을 찾아 늘어난 체결을 적는다. resting의 filled·paid를 고친다.

    체결가격은 그때까지 체결된 수량 전체의 평균으로 읽는다(그래서 이번 체결 가격은 금액 차이 ÷
    수량이다). 가격이 없거나, 체결수량이 줄거나 주문수량을 넘거나, 전체체결인데 수량이 다르거나,
    종목·방향·수량이 다르거나, 낸 지 missing_limit이 지나도 내역에 없으면 문제다.
    """
    fills: list[Found] = []
    done: set[int] = set()
    for order in resting:
        row = next((r for r in rows if same(r.order_no, order.order_no)), None)
        problem = _problem(order, row, now, missing_limit)
        if problem is not None:
            return Tracked(fills, frozenset(done), f"주문 {order.order_no}: {problem}.")
        if row is None:
            continue
        if row.filled > order.filled and row.filled_price is not None:
            paid = row.filled_price * row.filled
            qty = row.filled - order.filled
            price = (paid - order.paid) / qty
            fills.append(Found(order.order_id, order.symbol, order.side, qty, price))
            order.filled, order.paid = row.filled, paid
        if row.state != "open" or row.remaining == 0:
            done.add(order.order_id)
    return Tracked(fills, frozenset(done), None)
