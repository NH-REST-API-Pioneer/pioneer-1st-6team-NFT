"""여러 계층이 함께 쓰는 타입. 다른 nh_mcp 모듈을 import하지 않는다."""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
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


# 전략이 받는 데이터 흐름. 봉 실행은 minute·day, 틱 실행은 tick·book·day.
type Stream = Literal["tick", "book", "minute", "day"]
# 종목코드 → 받는 흐름. 저장·비교할 때는 종목·흐름을 정렬한 JSON으로 쓴다.
type Streams = Mapping[str, frozenset[Stream]]


@dataclass(frozen=True)
class BarEvent:
    """봉 하나. bar.at은 봉이 시작한 시각이다(NH 분봉은 시작 시각으로 찍힌다)."""

    symbol: str
    interval: Literal["minute", "day"]
    bar: Candle


@dataclass(frozen=True)
class TickEvent:
    """체결 하나. bid·ask는 그때의 최우선 호가(없으면 None)."""

    symbol: str
    at: datetime
    price: Decimal
    qty: int
    bid: Decimal | None
    ask: Decimal | None


@dataclass(frozen=True)
class BookEvent:
    """호가. 단계마다 (가격, 잔량), 최우선부터."""

    symbol: str
    at: datetime
    bids: tuple[tuple[Decimal, int], ...]
    asks: tuple[tuple[Decimal, int], ...]


@dataclass(frozen=True)
class FillEvent:
    """서버가 만든 체결. 백테스트는 모의 체결, 실시간은 NH 체결."""

    symbol: str
    at: datetime
    side: Side
    qty: int
    price: Decimal


type MarketEvent = BarEvent | TickEvent | BookEvent
type StrategyEvent = MarketEvent | FillEvent


@dataclass(frozen=True)
class Target:
    """종목 하나의 목표 포지션(부호 있는 계약 수). price가 None이면 시장가, 있으면 지정가."""

    position: int
    price: Decimal | None = None


# 이벤트 하나에 대한 전략 출력. None은 그대로 둔다.
type Output = Mapping[str, Target] | None


@dataclass(frozen=True)
class SymbolTerms:
    """종목 하나의 체결 조건. 수수료는 체결 1건·1계약당 금액이다."""

    tick: Decimal
    multiplier: Decimal
    fee_per_fill: Decimal


# 격리 실행이 실패한 이유. 사용자 말로 바꾸는 일은 위쪽 계층이 한다.
type SandboxFailure = Literal[
    "invalid_code",
    "invalid_output",
    "exception",
    "cpu",
    "memory",
    "processes",
    "output",
    "timeout",
]


# 사용자가 올리는 모델 파일 하나의 상한. 50MiB(사람 결정, 2026-11-25).
MAX_MODEL_BYTES = 50 * 1024 * 1024

# 모델 파일 내용의 SHA-256(소문자 16진수 64자). 전략은 이 값으로 모델을 고르고, 보관 경로와
# 백테스트 키에도 이 값이 들어간다. model_hash를 지난 값만 이 타입이 된다.
ModelHash = NewType("ModelHash", str)
_MODEL_HASH = re.compile(r"[0-9a-f]{64}")


def model_hash(text: str) -> ModelHash | None:
    """소문자 16진수 64자면 ModelHash, 아니면 None."""
    return ModelHash(text) if _MODEL_HASH.fullmatch(text) else None


@dataclass(frozen=True)
class SandboxJob:
    """격리 실행기에 보내는 일 한 건. 코드는 문자열 그대로 넘기기만 한다.

    streams는 종목마다 받는 흐름이다. 전략은 이 종목에만 목표를 낼 수 있고, 목표의 절댓값은
    max_position을 넘을 수 없다. terms는 streams와 같은 종목마다 체결 조건이다.
    model은 사용자가 올린 모델 파일의 내용(없으면 None)이다. 서버는 열어 보지 않고 넘기기만 하고,
    전략 컨테이너 안에서만 읽힌다. 수십 MB라 repr에 넣지 않는다.
    """

    code: str
    params: dict[str, JsonValue]
    streams: Streams
    max_position: int
    terms: Mapping[str, SymbolTerms]
    events: list[MarketEvent]
    model: bytes | None = field(default=None, repr=False)


@dataclass(frozen=True)
class SandboxResult:
    """격리 실행 결과. 출력은 전략에 넣은 순서(체결 이벤트 포함)와 한 줄씩 맞는다.

    실패하면 outputs는 비고, index는 문제가 난 시장 이벤트의 순번이다(그 이벤트가 낸 체결
    이벤트에서 났어도 같다. 모르면 None).
    """

    status: Literal["ok"] | SandboxFailure
    outputs: list[Output]
    index: int | None
    detail: str


@dataclass(frozen=True)
class SessionJob:
    """띄워 두는 전략 컨테이너 하나를 여는 일(M4b 자동 실행).

    SandboxJob에서 이벤트를 빼고 만료를 더했다. 이벤트는 연 뒤에 하나씩 넣는다. 체결 계산은 없고
    체결 이벤트도 부르는 쪽(실시간이면 NH 체결)이 넣는다. expires_at(승인 만료, 실행기가 최대 7일로
    자른다)이 지나면 실행기가 닫는다.
    """

    code: str
    params: dict[str, JsonValue]
    streams: Streams
    max_position: int
    terms: Mapping[str, SymbolTerms]
    expires_at: datetime
    model: bytes | None = field(default=None, repr=False)


@dataclass(frozen=True)
class SessionFailed:
    """띄워 둔 전략 컨테이너가 실패로 끝났다. 컨테이너는 이미 지웠고 그 세션은 다시 쓸 수 없다."""

    reason: SandboxFailure
    detail: str


# 하루 손실 한도에 닿았을 때: 정리하고 멈춘다 / 그대로 두고 멈춘다.
type OnLoss = Literal["liquidate", "hold"]
# auto: 목표대로 주문한다. suggest: 제안만 하고 사용자가 확인한다.
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


@dataclass(frozen=True)
class RunSnapshot:
    """실행 중인 전략의 지금 모습. 자동 주문 한 건마다 실행기가 채워 `place_auto`에 넘긴다.

    symbols는 그 전략이 받는 흐름의 종목, resting_qty는 그 전략이 그 종목에 걸어 둔 미체결
    (매수 +, 매도 -), orders_today는 오늘 그 전략이 낸 주문 수, strategy_pnl은 오늘 그 전략의
    손익(실제 체결, 미청산은 마지막 가격 평가)이다. 모르는 값은 None이고, None이면 막는다.
    """

    run_id: str
    scope: StrategyScope
    symbols: frozenset[str]
    resting_qty: int | None
    orders_today: int | None
    strategy_pnl: Decimal | None


@dataclass(frozen=True)
class ScopeInput:
    """승인 범위 규칙이 보는 모든 것. position은 계좌에서 읽은 그 종목의 순포지션(매수 +)."""

    run: RunSnapshot
    order: OrderRequest
    position: int
    now: datetime


@dataclass(frozen=True)
class ScopeDecision:
    """승인 범위 규칙의 결과. 통과면 rule이 None이다."""

    rule: str | None
    message: str
