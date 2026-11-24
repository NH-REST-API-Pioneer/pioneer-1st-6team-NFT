"""여러 계층이 함께 쓰는 타입. 다른 nh_mcp 모듈을 import하지 않는다."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
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
    """get_account와 리스크 규칙이 함께 쓴다. 손익은 확정과 평가를 따로 둔다.

    `complete`는 보유·미체결을 빠짐없이 읽었을 때만 True다. 읽지 못한 행이 있거나 페이지를
    끝까지 받지 못했으면 False이고, 그러면 리스크 규칙은 모든 주문을 막는다.
    """

    market: Market
    currency: str
    deposit: Decimal | None
    orderable_amount: Decimal | None
    realized_pnl: Decimal | None
    unrealized_pnl: Decimal | None
    positions: list[Position]
    open_orders: list[Order]
    as_of: datetime
    complete: bool


type OrderType = Literal["limit", "market"]


@dataclass(frozen=True)
class OrderRequest:
    """주문 한 건의 내용. 확인 토큰에 담기고, 실행 때 이 값으로만 보낸다."""

    market: Market
    symbol: str
    side: Side
    type: OrderType
    quantity: int
    price: Decimal | None


@dataclass(frozen=True)
class RiskLimits:
    """사용자별 한도. 하나라도 없으면 주문은 막힌다."""

    daily_loss: Decimal
    max_position: int
    max_order_qty: int

    def raises(self, new: "RiskLimits") -> bool:
        """새 한도가 하나라도 더 커지면 True. 한도를 올리는 변경은 확인을 거친다."""
        return (
            new.daily_loss > self.daily_loss
            or new.max_position > self.max_position
            or new.max_order_qty > self.max_order_qty
        )


@dataclass(frozen=True)
class Session:
    """거래 시간(KST). 주문은 opens <= t < closes에 허용, closing_auction <= t는 종가 단일가."""

    opens: time
    closing_auction: time
    closes: time


@dataclass(frozen=True)
class InstrumentRules:
    contract: Contract
    tick_size: Decimal
    session: Session


@dataclass(frozen=True)
class RiskInput:
    """리스크 규칙이 보는 모든 것. 없는 값은 None이고, None이면 막는다."""

    order: OrderRequest
    env: Env
    instrument: InstrumentRules | None
    limits: RiskLimits | None
    account: AccountState
    orderable_qty: int | None
    now: datetime
    orders_enabled: bool


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    rule: str | None
    message: str
    warnings: list[str]
    increases_position: bool


type TokenPurpose = Literal["order", "limits", "strategy"]


@dataclass(frozen=True)
class TokenRow:
    """확인 토큰 저장 행. payload는 주문 또는 한도 내용을 담은 JSON 문자열."""

    user_id: UserId
    purpose: TokenPurpose
    payload: str
    expires_at: datetime
    used_at: datetime | None


# 전략 신호. 목표 포지션으로 읽는다: buy=매수 보유, sell=매도 보유, exit=정리, none=유지.
type SignalName = Literal["buy", "sell", "exit", "none"]

# 격리 실행이 실패한 이유. 사용자 말로 바꾸는 일은 위쪽 계층이 한다.
type SandboxFailure = Literal[
    "invalid_code",
    "invalid_signal",
    "exception",
    "cpu",
    "memory",
    "processes",
    "output",
    "timeout",
]


@dataclass(frozen=True)
class SandboxJob:
    """격리 실행기에 보내는 일 한 건. 코드는 문자열 그대로 넘기기만 한다."""

    code: str
    params: dict[str, JsonValue]
    bars: list[Candle]


@dataclass(frozen=True)
class SandboxResult:
    """격리 실행 결과. 실패하면 signals는 비고, bar_index는 문제가 난 봉(모르면 None)."""

    status: Literal["ok"] | SandboxFailure
    signals: list[SignalName]
    bar_index: int | None
    detail: str


# 하루 손실 한도에 닿았을 때: 정리하고 멈춘다 / 그대로 두고 멈춘다.
type OnLoss = Literal["liquidate", "hold"]
# auto: 신호대로 주문한다. suggest: 제안만 하고 사용자가 확인한다.
type StrategyMode = Literal["auto", "suggest"]


@dataclass(frozen=True)
class StrategyScope:
    """전략을 돌리는 범위. 확인 토큰에 담기고, 실행 중에는 이 안에서만 움직인다."""

    quantity: int
    max_position: int
    daily_orders: int
    daily_loss: Decimal
    expires_at: datetime
    on_loss: OnLoss
    mode: StrategyMode
