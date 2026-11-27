"""리스크 규칙 테스트가 같이 쓰는 기본 입력. 바꿀 것만 인자로 넘긴다."""

from dataclasses import replace
from datetime import date, datetime, time
from decimal import Decimal

from nh_mcp.domain import (
    KST,
    AccountState,
    Contract,
    ContractKind,
    InstrumentRules,
    OptionBook,
    OptionLimits,
    Order,
    OrderRequest,
    OrderType,
    Position,
    RiskInput,
    RiskLimits,
    Session,
    Side,
    TickBand,
)

SYMBOL = "A016C000"
OPTION = "B016C725"  # 코스피200 C 202612 725.0, 승수 250000, 마스터 상한가 103.25
NOW = datetime(2026, 11, 25, 10, 0, tzinfo=KST)  # 수요일
SESSION = Session(time(8, 45), time(15, 35), time(15, 45))
# 옵션 한도: 하루 프리미엄 100만 원, 옵션 계약 합계 4, 매도 합계 2. 1회 주문 수량은 10까지.
OPTION_LIMITS = OptionLimits(Decimal("1000000"), 4, 2)
LIMITS_WITH_OPTIONS = RiskLimits(Decimal("500000"), 3, 10, OPTION_LIMITS)


def contract(expiry: date = date(2026, 12, 10)) -> Contract:
    return Contract(
        "domestic", SYMBOL, "F 202612", "KOSPI200", "future", expiry, Decimal("250000"), None
    )


def instrument(expiry: date = date(2026, 12, 10)) -> InstrumentRules:
    return InstrumentRules(contract(expiry), (TickBand(Decimal(0), Decimal("0.05")),), SESSION)


def option_instrument(
    kind: ContractKind = "call",
    upper_limit: Decimal | None = Decimal("103.25"),
    expiry: date = date(2026, 12, 10),
) -> InstrumentRules:
    option = Contract(
        "domestic",
        OPTION,
        "코스피200 C 202612 725.0",
        "KOSPI",
        kind,
        expiry,
        Decimal("250000"),
        Decimal("725.00"),
        upper_limit,
    )
    ladder = (TickBand(Decimal(0), Decimal("0.01")), TickBand(Decimal(10), Decimal("0.05")))
    return InstrumentRules(option, ladder, SESSION)


def order(
    side: Side = "buy",
    type_: OrderType = "limit",
    quantity: int = 1,
    price: Decimal | None = Decimal("400.05"),
) -> OrderRequest:
    return OrderRequest("domestic", SYMBOL, side, type_, quantity, price)


def position(
    side: Side = "buy", quantity: int = 1, closable: int | None = None, symbol: str = SYMBOL
) -> Position:
    return Position(
        symbol,
        "F 202612",
        side,
        quantity,
        quantity if closable is None else closable,
        None,
        None,
        None,
    )


def open_order(side: Side = "buy", remaining: int = 1, symbol: str = SYMBOL) -> Order:
    return Order("1", "", symbol, "F 202612", side, "접수", remaining, None, 0, None, remaining, "")


def account(
    realized: str | None = "0",
    unrealized: str | None = "0",
    positions: list[Position] | None = None,
    open_orders: list[Order] | None = None,
    complete: bool = True,
) -> AccountState:
    return AccountState(
        "domestic",
        "KRW",
        None,
        None,
        None if realized is None else Decimal(realized),
        None if unrealized is None else Decimal(unrealized),
        positions or [],
        open_orders or [],
        NOW,
        complete,
    )


def option_order(
    side: Side = "buy",
    type_: OrderType = "limit",
    quantity: int = 1,
    price: Decimal | None = Decimal("2.00"),
) -> OrderRequest:
    """기본은 2.00 지정가 매수 1계약. 프리미엄은 2.00, 승수 250000, 1계약을 곱한 50만 원."""
    return OrderRequest(
        "domestic", OPTION, side, type_, quantity, None if type_ == "market" else price
    )


EMPTY_BOOK = OptionBook(0, 0, 0, 0, Decimal(0), Decimal(0))


def book(**changes: object) -> OptionBook:
    """옵션 보유·미체결·체결이 없는 장부에서 바꿀 것만."""
    return replace(EMPTY_BOOK, **changes)


def option_input(**changes: object) -> RiskInput:
    """옵션 묶음을 켜고 옵션 한도를 정한 사용자의 옵션 매수 1계약. 옵션 매도는 꺼져 있다."""
    base = risk_input(
        order=option_order(),
        instrument=option_instrument(),
        limits=LIMITS_WITH_OPTIONS,
        options_enabled=True,
        option_book=book(),
    )
    return replace(base, **changes)


def risk_input(**changes: object) -> RiskInput:
    base = RiskInput(
        order=order(),
        env="demo",
        instrument=instrument(),
        limits=RiskLimits(Decimal("500000"), 3, 2),
        account=account(),
        orderable_qty=10,
        now=NOW,
        orders_enabled=True,
        options_enabled=False,
        option_sell=False,
        option_book=None,
    )
    return replace(base, **changes)
