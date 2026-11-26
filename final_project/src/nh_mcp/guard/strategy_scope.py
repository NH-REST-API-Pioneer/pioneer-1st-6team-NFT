"""자동 주문의 승인 범위 규칙. 순수 함수이고 입력이 모자라면 막는다.

계좌 규칙(한도, 증거금, 거래 시간 등)은 수동 주문과 똑같이 `guard.risk`가 먼저 본다. 이 규칙은
그 위에 `start_strategy`에서 사람이 승인한 범위를 더한다.
"""

from decimal import Decimal

from nh_mcp.domain import OrderRequest, Position, ScopeDecision, ScopeInput

SCOPE_MODE = "scope_mode"
SCOPE_SYMBOL = "scope_symbol"
SCOPE_EXPIRED = "scope_expired"
SCOPE_DAILY_ORDERS = "scope_daily_orders"
SCOPE_QUANTITY = "scope_quantity"
SCOPE_POSITION = "scope_position"
SCOPE_DAILY_LOSS = "scope_daily_loss"

type Block = tuple[str, str]


def _plain(value: Decimal) -> str:
    return format(value, "f")


def net_position(positions: list[Position], symbol: str) -> int:
    """그 종목의 순포지션. 매수는 +, 매도는 -."""
    return sum(
        p.quantity if p.side == "buy" else -p.quantity for p in positions if p.symbol == symbol
    )


def _signed(order: OrderRequest) -> int:
    return order.quantity if order.side == "buy" else -order.quantity


def _reduces(exposure: int, delta: int) -> bool:
    """줄이는 주문인가: 걸린 쪽의 반대 방향이고 0을 넘어 뒤집지 않는다."""
    return exposure * delta < 0 and abs(delta) <= abs(exposure)


def _check_mode(inp: ScopeInput) -> Block | None:
    if inp.run.scope.mode == "auto":
        return None
    return SCOPE_MODE, "제안 모드 전략은 주문하지 않습니다."


def _check_symbol(inp: ScopeInput) -> Block | None:
    if inp.order.symbol in inp.run.symbols:
        return None
    return SCOPE_SYMBOL, f"{inp.order.symbol}은(는) 이 전략이 승인받은 종목이 아닙니다."


def _check_expiry(inp: ScopeInput) -> Block | None:
    if inp.now < inp.run.scope.expires_at:
        return None
    return SCOPE_EXPIRED, "전략 승인이 만료됐습니다."


def _check_daily_orders(inp: ScopeInput) -> Block | None:
    count, cap = inp.run.orders_today, inp.run.scope.daily_orders
    if count is None:
        return SCOPE_DAILY_ORDERS, "오늘 이 전략이 낸 주문 수를 알 수 없어 주문을 막습니다."
    if count < cap:
        return None
    return SCOPE_DAILY_ORDERS, f"이 전략의 하루 주문 횟수 {cap}번을 다 썼습니다."


def _check_quantity(inp: ScopeInput) -> Block | None:
    cap = inp.run.scope.quantity
    if inp.order.quantity <= cap:
        return None
    return SCOPE_QUANTITY, f"이 전략의 1회 수량 {cap}계약을 넘었습니다."


def _check_position(inp: ScopeInput, exposure: int, delta: int) -> Block | None:
    """수량은 부호 있는 값(매수 +, 매도 -)으로 보여 준다."""
    cap, after = inp.run.scope.max_position, exposure + delta
    if abs(after) <= cap:
        return None
    return (
        SCOPE_POSITION,
        f"이 전략의 최대 포지션 {cap}계약을 넘습니다. "
        f"보유와 이 전략의 미체결 {exposure:+d}, 이번 주문 {delta:+d}, 합계 {after:+d}계약.",
    )


def _check_daily_loss(inp: ScopeInput) -> Block | None:
    pnl, cap = inp.run.strategy_pnl, inp.run.scope.daily_loss
    if pnl is None:
        return SCOPE_DAILY_LOSS, "이 전략의 오늘 손익을 알 수 없어 포지션을 늘리는 주문은 막습니다."
    if pnl > -cap:
        return None
    return SCOPE_DAILY_LOSS, f"이 전략의 일일 손실 한도 {_plain(cap)}원에 닿았습니다."


def _first_block(inp: ScopeInput) -> Block | None:
    for check in (_check_mode, _check_symbol, _check_expiry):
        block = check(inp)
        if block is not None:
            return block
    resting = inp.run.resting_qty
    if resting is None:
        # 걸어 둔 수량을 모르면 이 주문이 줄이는지도 알 수 없다.
        return SCOPE_POSITION, "이 전략의 미체결 수량을 알 수 없어 주문을 막습니다."
    exposure, delta = inp.position + resting, _signed(inp.order)
    if _reduces(exposure, delta):
        # 하루 주문 수·1회 수량을 다 써도 정리는 된다(손실 한도에서 정리할 때 막히지 않게).
        return None
    for check in (_check_daily_orders, _check_quantity):
        block = check(inp)
        if block is not None:
            return block
    block = _check_position(inp, exposure, delta)
    if block is not None:
        return block
    return _check_daily_loss(inp)


def evaluate_scope(inp: ScopeInput) -> ScopeDecision:
    """규칙을 순서대로 돌려 첫 차단에서 멈춘다. 모드·종목·만료는 모든 주문에, 하루 주문 수·1회
    수량·손실·포지션은 늘리는 주문에만 건다.

    청산 주문은 막지 않는다(`guard.risk`와 같은 원칙). 줄이는 주문은 (보유 + 이 전략의 미체결)의
    반대 방향이고 0을 넘어 뒤집지 않는 주문이다.
    """
    block = _first_block(inp)
    if block is None:
        return ScopeDecision(None, "")
    return ScopeDecision(block[0], block[1])
