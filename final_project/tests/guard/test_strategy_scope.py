"""승인 범위 규칙: 규칙마다 통과·차단·경계, 줄이는 주문, 모르는 값."""

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from builders import NOW, SYMBOL, order, position

from nh_mcp.domain import OrderRequest, RunSnapshot, ScopeInput, StrategyScope
from nh_mcp.guard.strategy_scope import (
    SCOPE_DAILY_LOSS,
    SCOPE_DAILY_ORDERS,
    SCOPE_EXPIRED,
    SCOPE_MODE,
    SCOPE_POSITION,
    SCOPE_QUANTITY,
    SCOPE_SYMBOL,
    evaluate_scope,
    net_position,
)

OTHER = "A656C000"
# 1회 2계약, 최대 3계약, 하루 5번, 손실 30만 원, 내일 이 시각 만료.
SCOPE = StrategyScope(2, 3, 5, Decimal("300000"), NOW + timedelta(days=1), "hold", "auto")
LOSS_HIT = Decimal("-300000")


def run(**changes: object) -> RunSnapshot:
    base = RunSnapshot("run-1", SCOPE, frozenset({SYMBOL}), 0, 0, Decimal("0"))
    return replace(base, **changes)


def rule(
    order_: OrderRequest | None = None,
    position_: int = 0,
    now: datetime = NOW,
    **changes: object,
) -> str | None:
    """이 주문·보유·시각에 run(**changes)로 돌린 결과의 규칙 이름(통과면 None)."""
    return evaluate_scope(ScopeInput(run(**changes), order_ or order(), position_, now)).rule


def scope(**changes: object) -> StrategyScope:
    return replace(SCOPE, **changes)


def buy(quantity: int = 1) -> OrderRequest:
    return order(quantity=quantity)


def sell(quantity: int = 1) -> OrderRequest:
    return order(side="sell", quantity=quantity)


def test_an_order_inside_the_scope_passes() -> None:
    decision = evaluate_scope(ScopeInput(run(), order(), 0, NOW))

    assert (decision.rule, decision.message) == (None, "")


def test_a_suggest_mode_run_places_nothing_not_even_a_closing_order() -> None:
    assert rule(scope=scope(mode="suggest")) == SCOPE_MODE
    assert rule(sell(), position_=1, scope=scope(mode="suggest")) == SCOPE_MODE


def test_a_symbol_outside_the_runs_streams_is_blocked() -> None:
    assert rule(symbols=frozenset({OTHER})) == SCOPE_SYMBOL
    assert rule(symbols=frozenset({OTHER, SYMBOL})) is None


@pytest.mark.parametrize(
    ("now", "blocked"),
    [
        (SCOPE.expires_at - timedelta(microseconds=1), False),
        (SCOPE.expires_at, True),
        (SCOPE.expires_at + timedelta(minutes=1), True),
    ],
)
def test_expiry_boundary(now: datetime, blocked: bool) -> None:
    assert rule(now=now) == (SCOPE_EXPIRED if blocked else None)


def test_expiry_also_blocks_a_closing_order() -> None:
    assert rule(sell(), position_=1, now=SCOPE.expires_at) == SCOPE_EXPIRED


@pytest.mark.parametrize(
    ("orders_today", "blocked"), [(0, False), (4, False), (5, True), (6, True)]
)
def test_daily_orders_boundary(orders_today: int, blocked: bool) -> None:
    assert rule(orders_today=orders_today) == (SCOPE_DAILY_ORDERS if blocked else None)


def test_unknown_daily_order_count_blocks_increasing_orders() -> None:
    assert rule(orders_today=None) == SCOPE_DAILY_ORDERS


def test_quantity_boundary_applies_to_increasing_orders() -> None:
    assert rule(buy(2)) is None
    assert rule(buy(3)) == SCOPE_QUANTITY


def test_closing_orders_pass_after_the_daily_orders_and_quantity_caps() -> None:
    # 손실 한도에서 정리(on_loss=liquidate)할 때 하루 주문 수·1회 수량에 막히면 포지션에 갇힌다.
    assert rule(sell(3), position_=3) is None
    assert rule(sell(), position_=1, orders_today=None) is None
    assert rule(sell(), position_=1, orders_today=99) is None
    assert rule(buy(), position_=1, orders_today=99) == SCOPE_DAILY_ORDERS
    assert rule(sell(4), position_=3) == SCOPE_QUANTITY  # 0을 넘어 뒤집으면 늘리는 주문이다


@pytest.mark.parametrize(
    ("order_", "position_", "resting", "blocked"),
    [
        (buy(1), 2, 0, False),  # 2 + 1 = 3: 딱 최대
        (buy(1), 3, 0, True),  # 3 + 1 = 4
        (buy(2), 1, 1, True),  # 보유 1 + 이 전략 미체결 1 + 2 = 4
        (buy(2), 1, 0, False),
        (sell(2), -1, 0, False),  # -1 - 2 = -3: 딱 최대
        (sell(2), -2, 0, True),  # -4
        (sell(2), 0, -2, True),  # 걸어 둔 매도 2 + 2 = -4
    ],
)
def test_position_boundary_counts_the_position_and_the_runs_resting_orders(
    order_: OrderRequest, position_: int, resting: int, blocked: bool
) -> None:
    assert rule(order_, position_, resting_qty=resting) == (SCOPE_POSITION if blocked else None)


def test_the_position_message_shows_the_sum() -> None:
    inp = ScopeInput(run(resting_qty=1), buy(2), 1, NOW)

    decision = evaluate_scope(inp)

    assert decision.rule == SCOPE_POSITION
    assert decision.message == (
        "이 전략의 최대 포지션 3계약을 넘습니다. "
        "보유와 이 전략의 미체결 +2, 이번 주문 +2, 합계 +4계약."
    )
    short = evaluate_scope(ScopeInput(run(), sell(2), -2, NOW))
    assert "+2" not in short.message
    assert "합계 -4계약" in short.message


def test_unknown_resting_quantity_blocks_even_a_closing_order() -> None:
    assert rule(sell(), position_=1, resting_qty=None) == SCOPE_POSITION
    assert rule(buy(), resting_qty=None) == SCOPE_POSITION


@pytest.mark.parametrize(
    ("pnl", "blocked"),
    [
        (Decimal("0"), False),
        (Decimal("-299999.99"), False),
        (LOSS_HIT, True),  # 딱 -daily_loss면 막는다(guard.risk와 같다)
        (Decimal("-1000000"), True),
    ],
)
def test_daily_loss_boundary_for_increasing_orders(pnl: Decimal, blocked: bool) -> None:
    assert rule(strategy_pnl=pnl) == (SCOPE_DAILY_LOSS if blocked else None)


def test_unknown_strategy_pnl_blocks_increasing_orders_only() -> None:
    assert rule(strategy_pnl=None) == SCOPE_DAILY_LOSS
    assert rule(sell(), position_=1, strategy_pnl=None) is None


@pytest.mark.parametrize(
    ("order_", "position_", "resting"),
    [
        (sell(1), 1, 0),  # 정리
        (sell(1), 2, 0),  # 일부 정리
        (buy(2), -2, 0),  # 매도 정리
        (sell(1), 2, -1),  # 매도 1이 걸린 채 1 더 정리 (2 - 1 - 1 = 0)
    ],
)
def test_closing_orders_pass_when_the_strategy_loss_limit_is_hit(
    order_: OrderRequest, position_: int, resting: int
) -> None:
    assert rule(order_, position_, resting_qty=resting, strategy_pnl=LOSS_HIT) is None


def test_a_closing_order_passes_even_when_the_position_is_already_over_the_scope() -> None:
    # 수동 주문으로 범위보다 많이 들고 있어도 줄이는 것은 막지 않는다.
    assert rule(sell(1), position_=5) is None
    assert rule(buy(1), position_=5) == SCOPE_POSITION


def test_flipping_through_zero_counts_as_increasing() -> None:
    # 매수 1 보유에 매도 2: 매도 1 신규 진입. 크기는 범위 안이지만 손실 한도에 걸린다.
    assert rule(sell(2), position_=1) is None
    assert rule(sell(2), position_=1, strategy_pnl=LOSS_HIT) == SCOPE_DAILY_LOSS


def test_a_resting_closing_order_leaves_nothing_more_to_close() -> None:
    # 매수 1 보유, 이 전략이 매도 1을 이미 걸어 둠: 매도 1 더는 신규 진입이다.
    assert rule(sell(1), 1, resting_qty=-1, strategy_pnl=LOSS_HIT) == SCOPE_DAILY_LOSS


@pytest.mark.parametrize(
    ("changes", "first"),
    [
        ({"scope": scope(mode="suggest"), "symbols": frozenset({OTHER})}, SCOPE_MODE),
        ({"symbols": frozenset({OTHER}), "orders_today": 5}, SCOPE_SYMBOL),
        ({"scope": scope(expires_at=NOW), "orders_today": 5}, SCOPE_EXPIRED),
        ({"orders_today": 5, "scope": scope(quantity=0)}, SCOPE_DAILY_ORDERS),
        ({"scope": scope(quantity=0, max_position=0)}, SCOPE_QUANTITY),
        ({"scope": scope(max_position=0), "strategy_pnl": LOSS_HIT}, SCOPE_POSITION),
    ],
)
def test_rules_run_in_order_and_stop_at_the_first_block(
    changes: dict[str, object], first: str
) -> None:
    assert evaluate_scope(ScopeInput(run(**changes), order(), 0, NOW)).rule == first


def test_net_position_is_buys_minus_sells_of_that_symbol() -> None:
    other = replace(position(quantity=7), symbol=OTHER)
    rows = [position("buy", 3), position("sell", 1), other]

    assert net_position(rows, SYMBOL) == 2
    assert net_position([position("sell", 2)], SYMBOL) == -2
    assert net_position([], SYMBOL) == 0
