"""주문 리스크 규칙. 순수 함수이고 입력이 모자라면 막는다."""

from decimal import Decimal

from nh_mcp.domain import (
    KST,
    InstrumentRules,
    Order,
    OrderRequest,
    Position,
    RiskDecision,
    RiskInput,
    RiskLimits,
)
from nh_mcp.guard.env import orders_allowed

LIMITS_NOT_SET = "limits_not_set"
DEMO_ONLY = "demo_only"
ACCOUNT_UNKNOWN = "account_unknown"
ORDER_FORMAT = "order_format"
TRADING_HOURS = "trading_hours"
MAX_ORDER_QTY = "max_order_qty"
MAX_POSITION = "max_position"
DAILY_LOSS = "daily_loss"
MARGIN = "margin"

# 만기까지 이 일수 이하로 남으면 경고한다.
EXPIRY_WARN_DAYS = 3

# datetime.weekday(): 월=0 … 토=5, 일=6
SATURDAY = 5

type Block = tuple[str, str]


def _plain(value: Decimal) -> str:
    return format(value, "f")


def nearest_ticks(price: Decimal, tick: Decimal) -> tuple[Decimal, Decimal]:
    """가격 바로 아래·위 틱. 틱에 정확히 맞으면 둘 다 그 가격."""
    below = (price // tick) * tick
    if below == price:
        return below, below
    return below, below + tick


def increases_position(
    order: OrderRequest, positions: list[Position], open_orders: list[Order]
) -> bool:
    """줄이는 주문이 아니면 True.

    줄이는 주문: 수량이 (반대 방향 보유의 청산가능수량 합) - (같은 방향 미체결 잔량 합) 이내.
    이미 걸어 둔 청산 주문이 청산가능수량에 빠져 있지 않을 수 있어 직접 뺀다.
    """
    closable = sum(
        p.closable for p in positions if p.symbol == order.symbol and p.side != order.side
    )
    resting = sum(
        o.remaining_quantity
        for o in open_orders
        if o.symbol == order.symbol and o.side == order.side
    )
    return order.quantity > closable - resting


def _check_demo(inp: RiskInput) -> Block | None:
    if orders_allowed(inp.env, inp.orders_enabled):
        return None
    return (
        DEMO_ONLY,
        "주문이 꺼져 있습니다. 모의 환경에서 주문 스위치를 켠 때만 주문할 수 있습니다.",
    )


def _check_account(inp: RiskInput) -> Block | None:
    """계좌를 다 읽지 못하면 줄이는 주문도 막는다. 보유·미체결을 몰라 줄이는지도 알 수 없다."""
    if inp.account.complete:
        return None
    return (
        ACCOUNT_UNKNOWN,
        "계좌 정보를 다 읽지 못해 주문을 막았습니다. "
        "잠시 뒤 다시 시도하거나 get_account로 확인해 주세요.",
    )


def _check_tick(price: Decimal, tick: Decimal) -> Block | None:
    if tick <= 0:
        return ORDER_FORMAT, "종목의 호가 단위를 알 수 없어 주문할 수 없습니다."
    if price % tick == 0:
        return None
    below, above = nearest_ticks(price, tick)
    return (
        ORDER_FORMAT,
        f"가격 {_plain(price)}은(는) 호가 단위 {_plain(tick)}에 맞지 않습니다. "
        f"가까운 가격은 {_plain(below)}, {_plain(above)}입니다.",
    )


def _check_price(order: OrderRequest, instrument: InstrumentRules) -> Block | None:
    if order.type == "market":
        if order.price is None:
            return None
        return ORDER_FORMAT, "시장가 주문에는 가격을 넣을 수 없습니다."
    if order.price is None:
        return ORDER_FORMAT, "지정가 주문에는 가격이 필요합니다."
    if order.price <= 0:
        return ORDER_FORMAT, "가격은 0보다 커야 합니다."
    return _check_tick(order.price, instrument.tick_size)


def _check_format(inp: RiskInput) -> Block | None:
    if inp.order.quantity < 1:
        return ORDER_FORMAT, "수량은 1 이상이어야 합니다."
    if inp.instrument is None:
        return ORDER_FORMAT, "종목 정보를 찾을 수 없어 주문할 수 없습니다."
    return _check_price(inp.order, inp.instrument)


def _check_hours(inp: RiskInput) -> Block | None:
    if inp.instrument is None:
        return TRADING_HOURS, "종목 정보를 찾을 수 없어 주문할 수 없습니다."
    now = inp.now.astimezone(KST)
    session = inp.instrument.session
    if now.weekday() >= SATURDAY or not session.opens <= now.time() < session.closes:
        return (
            TRADING_HOURS,
            f"거래 시간이 아닙니다. 평일 {session.opens:%H:%M}~{session.closes:%H:%M}에 "
            "주문할 수 있습니다.",
        )
    return None


def _check_order_qty(inp: RiskInput, limits: RiskLimits) -> Block | None:
    if inp.order.quantity <= limits.max_order_qty:
        return None
    return MAX_ORDER_QTY, f"1회 주문 한도 {limits.max_order_qty}계약을 넘었습니다."


def _check_position(inp: RiskInput, limits: RiskLimits) -> Block | None:
    order = inp.order
    held = sum(
        p.quantity
        for p in inp.account.positions
        if p.symbol == order.symbol and p.side == order.side
    )
    pending = sum(
        o.remaining_quantity
        for o in inp.account.open_orders
        if o.symbol == order.symbol and o.side == order.side
    )
    total = held + pending + order.quantity
    if total <= limits.max_position:
        return None
    return (
        MAX_POSITION,
        f"포지션 한도 {limits.max_position}계약을 넘습니다. "
        f"보유 {held} + 미체결 {pending} + 이번 주문 {order.quantity} = {total}계약.",
    )


def _check_daily_loss(inp: RiskInput, limits: RiskLimits) -> Block | None:
    realized = inp.account.realized_pnl
    unrealized = inp.account.unrealized_pnl
    if realized is None or unrealized is None:
        return DAILY_LOSS, "오늘 손익을 확인할 수 없어 포지션을 늘리는 주문은 막습니다."
    if realized + unrealized > -limits.daily_loss:
        return None
    return DAILY_LOSS, f"일일 손실 한도 {_plain(limits.daily_loss)}원에 닿았습니다."


def _check_margin(inp: RiskInput) -> Block | None:
    if inp.orderable_qty is None:
        return MARGIN, "주문 가능 수량을 확인할 수 없어 포지션을 늘리는 주문은 막습니다."
    if inp.order.quantity <= inp.orderable_qty:
        return None
    return MARGIN, f"증거금이 부족합니다. 주문 가능 수량은 {inp.orderable_qty}계약입니다."


def _warnings(inp: RiskInput) -> list[str]:
    warnings: list[str] = []
    if inp.instrument is None:
        return warnings
    now = inp.now.astimezone(KST)
    days = (inp.instrument.contract.expiry - now.date()).days
    if days <= EXPIRY_WARN_DAYS:
        warnings.append(f"만기까지 {days}일 남았습니다.")
    if inp.order.type == "market":
        warnings.append("시장가 주문은 체결 가격이 예상과 크게 다를 수 있습니다.")
    session = inp.instrument.session
    if session.closing_auction <= now.time() < session.closes:
        warnings.append(
            f"종가 단일가 구간입니다. {session.closes:%H:%M}에 종가로 한 번에 체결됩니다."
        )
    return warnings


def _first_block(inp: RiskInput, limits: RiskLimits, increasing: bool) -> Block | None:
    blocks = (
        _check_demo(inp),
        _check_account(inp),
        _check_format(inp),
        _check_hours(inp),
        _check_order_qty(inp, limits),
    )
    for block in blocks:
        if block is not None:
            return block
    if not increasing:
        return None
    for check in (_check_position, _check_daily_loss):
        block = check(inp, limits)
        if block is not None:
            return block
    return _check_margin(inp)


def evaluate(inp: RiskInput) -> RiskDecision:
    """규칙을 순서대로 돌려 첫 차단에서 멈춘다. 늘리는 주문에만 5~7번을 건다."""
    increasing = increases_position(inp.order, inp.account.positions, inp.account.open_orders)
    if inp.limits is None:
        return RiskDecision(
            False, LIMITS_NOT_SET, "리스크 한도가 설정되지 않아 주문할 수 없습니다.", [], increasing
        )
    block = _first_block(inp, inp.limits, increasing)
    if block is not None:
        return RiskDecision(False, block[0], block[1], [], increasing)
    return RiskDecision(True, None, "", _warnings(inp), increasing)
