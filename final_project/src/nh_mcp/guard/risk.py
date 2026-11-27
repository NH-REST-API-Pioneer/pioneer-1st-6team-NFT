"""주문 리스크 규칙. 순수 함수이고 입력이 모자라면 막는다.

옵션(콜·풋)의 늘리는 주문은 선물의 종목별 포지션 한도 대신 옵션 규칙 7개(`_check_options`)를
지난다. 옵션 청산은 옵션 규칙에 걸리지 않는다(`docs/options.md`).
"""

from collections.abc import Callable
from decimal import Decimal

from nh_mcp.domain import (
    KST,
    Contract,
    ContractKind,
    InstrumentRules,
    OptionBook,
    OptionLimits,
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
OPTIONS_OFF = "options_off"
OPTION_BOOK_UNKNOWN = "option_book_unknown"
OPTION_SELL_OFF = "option_sell_off"
OPTION_SHORT_LIMIT = "option_short_limit"
OPTION_POSITION_LIMIT = "option_position_limit"
PREMIUM_LIMIT = "premium_limit"

OPTION_KINDS: frozenset[ContractKind] = frozenset({"call", "put"})

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
    return _check_tick(order.price, instrument.tick_at(order.price))


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


def premium(order: OrderRequest, contract: Contract) -> Decimal | None:
    """주문 하나의 프리미엄(원): 가격·승수·수량의 곱. 시장가는 종목의 상한가로 센다.

    주문 검사(services)는 오늘 시세와 마스터의 상한가 중 큰 값을 종목에 넣어 넘긴다. 모르면 None.
    """
    price = order.price if order.type == "limit" else contract.upper_limit
    if price is None:
        return None
    return price * contract.multiplier * order.quantity


type OptionCheck = Callable[[RiskInput, OptionLimits, OptionBook, Contract], Block | None]


def _check_option_short(
    inp: RiskInput, limits: OptionLimits, book: OptionBook, _: Contract
) -> Block | None:
    """규칙 4·5: 늘리는 매도는 "옵션 매도"가 켜져 있고 매도 계약 수 합계 안이어야 한다."""
    order = inp.order
    if order.side == "buy":
        return None
    if not inp.option_sell:
        return (
            OPTION_SELL_OFF,
            "'옵션 매도'가 꺼져 있어 보유하지 않은 옵션을 파는 새 매도는 막습니다. 보유 옵션을 "
            "되파는 청산은 됩니다. 사용자가 등록 화면의 '옵션 매도' 스위치를 켜야 합니다"
            "(AI는 켤 수 없습니다).",
        )
    shorts = book.short_total + book.pending_short + order.quantity
    if shorts <= limits.max_short:
        return None
    return (
        OPTION_SHORT_LIMIT,
        f"옵션 매도 한도 {limits.max_short}계약을 넘습니다. 보유 매도 {book.short_total} + "
        f"미체결 새 매도 {book.pending_short} + 이번 주문 {order.quantity} = {shorts}계약.",
    )


def _check_option_position(
    inp: RiskInput, limits: OptionLimits, book: OptionBook, _: Contract
) -> Block | None:
    """규칙 6: 모든 옵션 종목의 계약 수 합계(매수·매도 합)."""
    quantity = inp.order.quantity
    total = book.long_total + book.short_total + book.pending_increase + quantity
    if total <= limits.max_position:
        return None
    return (
        OPTION_POSITION_LIMIT,
        f"옵션 포지션 한도 {limits.max_position}계약(모든 옵션 합계)을 넘습니다. "
        f"보유 매수 {book.long_total} + 보유 매도 {book.short_total} + 늘리는 미체결 "
        f"{book.pending_increase} + 이번 주문 {quantity} = {total}계약.",
    )


def _check_premium(
    inp: RiskInput, limits: OptionLimits, book: OptionBook, contract: Contract
) -> Block | None:
    """규칙 7: 늘리는 매수는 오늘 체결 + 미체결 매수 + 이번 주문 프리미엄이 하루 한도 안이어야 한다.

    시장가는 상한가로 센다(`premium`).
    """
    if inp.order.side == "sell":
        return None
    cost = premium(inp.order, contract)
    if cost is None:
        return PREMIUM_LIMIT, "상한가를 몰라 시장가 매수의 프리미엄을 셀 수 없어 막습니다."
    total = book.premium_spent + book.premium_pending + cost
    if total <= limits.premium_daily:
        return None
    return (
        PREMIUM_LIMIT,
        f"하루 옵션 프리미엄 한도 {_plain(limits.premium_daily)}원을 넘습니다. "
        f"오늘 체결 {_plain(book.premium_spent)} + 미체결 매수 {_plain(book.premium_pending)} + "
        f"이번 주문 {_plain(cost)} = {_plain(total)}원.",
    )


OPTION_CHECKS: tuple[OptionCheck, ...] = (
    _check_option_short,
    _check_option_position,
    _check_premium,
)


def _check_options(inp: RiskInput, limits: RiskLimits, contract: Contract) -> Block | None:
    """늘리는 옵션 주문의 규칙 1~7. 순서대로 보고 첫 차단에서 멈춘다."""
    if not inp.options_enabled:
        return (
            OPTIONS_OFF,
            "옵션 묶음이 꺼져 있어 옵션 포지션을 늘리는 주문은 막습니다. 청산 주문은 됩니다. "
            "사용자가 등록 화면의 '옵션' 스위치를 켜야 합니다(AI는 켤 수 없습니다).",
        )
    if limits.options is None:
        return (
            LIMITS_NOT_SET,
            "옵션 한도가 설정되지 않아 옵션 포지션을 늘리는 주문은 할 수 없습니다.",
        )
    if inp.option_book is None:
        return (
            OPTION_BOOK_UNKNOWN,
            "계좌의 옵션 보유·미체결이나 오늘 옵션 체결을 확인할 수 없어 옵션 포지션을 늘리는 "
            "주문은 막습니다.",
        )
    for check in OPTION_CHECKS:
        block = check(inp, limits.options, inp.option_book, contract)
        if block is not None:
            return block
    return None


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
    instrument = inp.instrument
    if instrument is not None and instrument.contract.kind in OPTION_KINDS:
        block = _check_options(inp, limits, instrument.contract)
    else:
        block = _check_position(inp, limits)
    return block or _check_daily_loss(inp, limits) or _check_margin(inp)


def evaluate(inp: RiskInput) -> RiskDecision:
    """규칙을 순서대로 돌려 첫 차단에서 멈춘다. 늘리는 주문에만 5~7번을 건다.

    옵션은 5번(종목별 포지션 한도) 자리에 옵션 규칙 7개를 건다.
    """
    increasing = increases_position(inp.order, inp.account.positions, inp.account.open_orders)
    if inp.limits is None:
        return RiskDecision(
            False, LIMITS_NOT_SET, "리스크 한도가 설정되지 않아 주문할 수 없습니다.", [], increasing
        )
    block = _first_block(inp, inp.limits, increasing)
    if block is not None:
        return RiskDecision(False, block[0], block[1], [], increasing)
    return RiskDecision(True, None, "", _warnings(inp), increasing)
