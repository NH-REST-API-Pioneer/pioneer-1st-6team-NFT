"""여러 계층이 함께 쓰는 타입. 다른 nh_mcp 모듈을 import하지 않는다."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal, NewType

# 한국은 서머타임이 없어 고정 오프셋으로 충분하다.
KST = timezone(timedelta(hours=9))

# NH는 모의 서버만 쓴다. 다른 환경 값은 두지 않는다.
type Env = Literal["demo"]

type Market = Literal["domestic", "overseas"]

# 로그인한 사용자. 키, 한도, 주문, 전략은 모두 이 값으로 나뉜다.
UserId = NewType("UserId", int)

# float는 넣지 않는다. 가격과 금액은 Decimal을 문자열로 바꿔 담는다.
type JsonValue = str | int | bool | list[JsonValue] | dict[str, JsonValue] | None

# 지금 시각을 돌려주는 함수. 테스트가 시간을 고정할 수 있게 주입한다.
type Clock = Callable[[], datetime]

type ContractKind = Literal["future", "spread", "call", "put"]


@dataclass(frozen=True)
class Contract:
    """종목 하나. 국내는 종목 마스터에서 온다."""

    market: Market
    symbol: str
    name: str
    underlying: str
    kind: ContractKind
    expiry: date
    multiplier: Decimal
    strike: Decimal | None


type Side = Literal["buy", "sell"]


@dataclass(frozen=True)
class BookLevel:
    bid: Decimal | None
    bid_qty: int | None
    ask: Decimal | None
    ask_qty: int | None


@dataclass(frozen=True)
class Quote:
    symbol: str
    name: str
    last: Decimal | None
    change: Decimal | None
    change_rate: Decimal | None
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    volume: int | None
    upper_limit: Decimal | None
    lower_limit: Decimal | None
    book: list[BookLevel]


@dataclass(frozen=True)
class Candle:
    at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int | None


@dataclass(frozen=True)
class Position:
    symbol: str
    name: str
    side: Side
    quantity: int
    closable: int
    avg_price: Decimal | None
    close_price: Decimal | None
    unrealized_pnl: Decimal | None


@dataclass(frozen=True)
class Order:
    order_no: str
    original_order_no: str
    symbol: str
    name: str
    side: Side
    status: str
    quantity: int
    price: Decimal | None
    filled_quantity: int
    filled_price: Decimal | None
    remaining_quantity: int
    reject_reason: str


@dataclass(frozen=True)
class OrderError:
    received_at: str
    symbol: str
    name: str
    side: Side | None
    code: str
    message: str
    quantity: int | None
    price: Decimal | None


@dataclass(frozen=True)
class AccountState:
    """get_account와 리스크 규칙이 함께 쓴다. 손익은 확정과 평가를 따로 둔다."""

    market: Market
    currency: str
    deposit: Decimal | None
    orderable_amount: Decimal | None
    realized_pnl: Decimal | None
    unrealized_pnl: Decimal | None
    positions: list[Position]
    open_orders: list[Order]
    as_of: datetime
