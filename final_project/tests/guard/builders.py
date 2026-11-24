"""리스크 규칙 테스트가 같이 쓰는 기본 입력. 바꿀 것만 인자로 넘긴다."""

from dataclasses import replace
from datetime import date, datetime, time
from decimal import Decimal

from nh_mcp.domain import (
    KST,
    AccountState,
    Contract,
    InstrumentRules,
    Order,
    OrderRequest,
    OrderType,
    Position,
    RiskInput,
    RiskLimits,
    Session,
    Side,
)

SYMBOL = "A016C000"
NOW = datetime(2026, 11, 25, 10, 0, tzinfo=KST)  # 수요일


def contract(expiry: date = date(2026, 12, 10)) -> Contract:
    return Contract(
        "domestic", SYMBOL, "F 202612", "KOSPI200", "future", expiry, Decimal("250000"), None
    )


def instrument(expiry: date = date(2026, 12, 10)) -> InstrumentRules:
    return InstrumentRules(
        contract(expiry), Decimal("0.05"), Session(time(8, 45), time(15, 35), time(15, 45))
    )


def order(
    side: Side = "buy",
    type_: OrderType = "limit",
    quantity: int = 1,
    price: Decimal | None = Decimal("400.05"),
) -> OrderRequest:
    return OrderRequest("domestic", SYMBOL, side, type_, quantity, price)


def position(side: Side = "buy", quantity: int = 1, closable: int | None = None) -> Position:
    return Position(
        SYMBOL,
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
    )
    return replace(base, **changes)
