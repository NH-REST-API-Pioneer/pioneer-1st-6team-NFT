"""옵션 리스크 규칙(`docs/options.md` 규칙 표). 늘리는 주문에만 걸리고, 청산은 지나간다.

기본 입력(`option_input`)은 옵션 묶음을 켜고 옵션 한도(프리미엄 100만 원, 계약 합계 4,
매도 합계 2)를 정한 사용자의 B016C725 2.00 지정가 매수 1계약이다. 프리미엄은 50만 원이다.
"""

from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal

import pytest
from builders import (
    LIMITS_WITH_OPTIONS,
    OPTION,
    OPTION_LIMITS,
    account,
    book,
    open_order,
    option_input,
    option_instrument,
    option_order,
    position,
    risk_input,
)

from nh_mcp.domain import KST, RiskInput, RiskLimits
from nh_mcp.guard.risk import (
    DAILY_LOSS,
    LIMITS_NOT_SET,
    MARGIN,
    MAX_ORDER_QTY,
    MAX_POSITION,
    OPTION_BOOK_UNKNOWN,
    OPTION_POSITION_LIMIT,
    OPTION_SELL_OFF,
    OPTION_SHORT_LIMIT,
    OPTIONS_OFF,
    PREMIUM_LIMIT,
    TRADING_HOURS,
    evaluate,
)

# 옵션 규칙이 막을 수 있는 것을 모두 꺼 둔 사용자: 묶음 꺼짐, 매도 꺼짐, 옵션 한도 없음, 장부 모름.
EVERYTHING_OFF: dict[str, object] = {
    "options_enabled": False,
    "option_sell": False,
    "limits": replace(LIMITS_WITH_OPTIONS, options=None),
    "option_book": None,
}


def rule(inp: RiskInput) -> str | None:
    return evaluate(inp).rule


def held(side: str, quantity: int) -> dict[str, object]:
    """B016C725를 그 방향으로 quantity계약 들고 있는 계좌."""
    assert side in ("buy", "sell")
    holding = position("buy" if side == "buy" else "sell", quantity, symbol=OPTION)
    return {"account": account(positions=[holding])}


def test_option_buy_within_every_limit_passes_and_increases() -> None:
    d = evaluate(option_input())

    assert (d.allowed, d.rule, d.warnings) == (True, None, [])
    assert d.increases_position is True


# ---- 청산은 규칙 1~7에 걸리지 않는다 ----------------------------------------------


def test_closing_sell_of_a_held_long_passes_with_everything_off() -> None:
    inp = option_input(order=option_order("sell"), **held("buy", 1) | EVERYTHING_OFF)

    d = evaluate(inp)

    assert (d.allowed, d.increases_position) == (True, False)


def test_closing_buy_of_a_held_short_passes_with_everything_off() -> None:
    inp = option_input(order=option_order("buy"), **held("sell", 2) | EVERYTHING_OFF)

    d = evaluate(inp)

    assert (d.allowed, d.increases_position) == (True, False)


def test_closing_option_order_still_obeys_trading_hours() -> None:
    late = datetime(2026, 11, 25, 15, 45, tzinfo=KST)
    inp = option_input(order=option_order("sell"), now=late, **held("buy", 1))

    assert rule(inp) == TRADING_HOURS


# ---- 1. options_off ----------------------------------------------------------------


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_options_off_blocks_increasing_orders(side: str) -> None:
    order = option_order("buy" if side == "buy" else "sell")
    inp = option_input(order=order, options_enabled=False, option_sell=True)

    d = evaluate(inp)

    assert (d.allowed, d.rule) == (False, OPTIONS_OFF)
    assert "'옵션' 스위치" in d.message


# ---- 2. LIMITS_NOT_SET -------------------------------------------------------------


def test_missing_option_limits_block_increasing_option_orders_even_with_futures_limits() -> None:
    futures_only = replace(LIMITS_WITH_OPTIONS, options=None)

    assert rule(option_input(limits=futures_only)) == LIMITS_NOT_SET
    # 같은 한도로 선물 주문은 지나간다.
    assert evaluate(risk_input(limits=futures_only)).allowed is True


def test_missing_all_limits_blocks_option_orders_like_futures() -> None:
    assert rule(option_input(limits=None, **held("buy", 1))) == LIMITS_NOT_SET


# ---- 3. option_book_unknown --------------------------------------------------------


def test_unknown_option_book_blocks_increasing_but_not_closing() -> None:
    assert rule(option_input(option_book=None)) == OPTION_BOOK_UNKNOWN
    closing = option_input(order=option_order("sell"), option_book=None, **held("buy", 1))
    assert evaluate(closing).allowed is True


# ---- 4. option_sell_off ------------------------------------------------------------


def test_naked_new_sell_is_blocked_when_option_selling_is_off() -> None:
    d = evaluate(option_input(order=option_order("sell")))

    assert (d.allowed, d.rule) == (False, OPTION_SELL_OFF)
    assert "'옵션 매도' 스위치" in d.message


def test_selling_more_than_the_held_long_is_a_new_sell() -> None:
    inp = option_input(order=option_order("sell", quantity=2), **held("buy", 1))

    d = evaluate(inp)

    assert (d.rule, d.increases_position) == (OPTION_SELL_OFF, True)


def test_new_sell_passes_when_option_selling_is_on() -> None:
    assert evaluate(option_input(order=option_order("sell"), option_sell=True)).allowed is True


# ---- 5. option_short_limit ---------------------------------------------------------


@pytest.mark.parametrize(("short_total", "pending_short", "allowed"), [(1, 0, True), (1, 1, False)])
def test_short_limit_counts_held_shorts_pending_new_sells_and_this_order(
    short_total: int, pending_short: int, allowed: bool
) -> None:
    # 매도 한도 2: 보유 매도 + 미체결 새 매도 + 이번 1계약.
    current = book(
        short_total=short_total, pending_short=pending_short, pending_increase=pending_short
    )
    inp = option_input(order=option_order("sell"), option_sell=True, option_book=current)

    d = evaluate(inp)

    assert d.allowed is allowed
    assert d.rule == (None if allowed else OPTION_SHORT_LIMIT)


def test_zero_short_limit_means_no_new_sells() -> None:
    no_shorts = replace(LIMITS_WITH_OPTIONS, options=replace(OPTION_LIMITS, max_short=0))
    inp = option_input(order=option_order("sell"), option_sell=True, limits=no_shorts)

    assert rule(inp) == OPTION_SHORT_LIMIT


def test_short_limit_does_not_apply_to_buys() -> None:
    assert evaluate(option_input(option_book=book(short_total=5))).rule == OPTION_POSITION_LIMIT
    assert evaluate(option_input(option_book=book(pending_short=5))).allowed is True


# ---- 6. option_position_limit ------------------------------------------------------


@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize(("pending", "allowed"), [(1, True), (2, False)])
def test_position_limit_counts_every_option_contract(
    side: str, pending: int, allowed: bool
) -> None:
    # 한도 4: 보유 매수 1 + 보유 매도 1 + 늘리는 미체결 + 이번 1계약.
    current = book(long_total=1, short_total=1, pending_increase=pending)
    order = option_order("buy" if side == "buy" else "sell")
    inp = option_input(order=order, option_sell=True, option_book=current)

    d = evaluate(inp)

    assert d.allowed is allowed
    assert d.rule == (None if allowed else OPTION_POSITION_LIMIT)


def test_option_orders_skip_the_futures_per_symbol_position_limit() -> None:
    no_futures = RiskLimits(Decimal("500000"), 0, 10, OPTION_LIMITS)

    assert evaluate(option_input(limits=no_futures)).allowed is True
    assert evaluate(risk_input(limits=no_futures)).rule == MAX_POSITION


def test_futures_positions_do_not_count_toward_the_option_position_limit() -> None:
    futures = account(positions=[position(quantity=10)], open_orders=[open_order(remaining=10)])

    assert evaluate(option_input(account=futures)).allowed is True


def test_futures_orders_ignore_the_option_book_and_switches() -> None:
    crowded = book(long_total=100, short_total=100, premium_spent=Decimal("99999999"))
    inp = risk_input(limits=LIMITS_WITH_OPTIONS, option_book=crowded, options_enabled=False)

    assert evaluate(inp).allowed is True


# ---- 7. premium_limit --------------------------------------------------------------


@pytest.mark.parametrize(("price", "allowed"), [("2.00", True), ("2.01", False)])
def test_premium_limit_counts_filled_pending_and_this_order(price: str, allowed: bool) -> None:
    # 오늘 체결 25만 + 미체결 매수 25만 + 이번 주문(2.00이면 50만) = 100만 원(한도와 같으면 통과).
    current = book(premium_spent=Decimal("250000"), premium_pending=Decimal("250000"))
    inp = option_input(order=option_order(price=Decimal(price)), option_book=current)

    d = evaluate(inp)

    assert d.allowed is allowed
    assert d.rule == (None if allowed else PREMIUM_LIMIT)
    if not allowed:
        assert "250000 + 미체결 매수 250000 + 이번 주문 502500.00 = 1002500.00원" in d.message


@pytest.mark.parametrize(("daily", "allowed"), [("25812500", True), ("25812499.99", False)])
def test_market_buy_premium_is_counted_at_the_upper_limit(daily: str, allowed: bool) -> None:
    # 상한가 103.25, 승수 250000, 1계약을 곱하면 25,812,500원.
    wide = replace(OPTION_LIMITS, premium_daily=Decimal(daily))
    inp = option_input(
        order=option_order(type_="market"), limits=replace(LIMITS_WITH_OPTIONS, options=wide)
    )

    d = evaluate(inp)

    assert d.allowed is allowed
    assert d.rule == (None if allowed else PREMIUM_LIMIT)


def test_market_buy_without_an_upper_limit_is_blocked() -> None:
    inp = option_input(
        order=option_order(type_="market"), instrument=option_instrument(upper_limit=None)
    )

    assert rule(inp) == PREMIUM_LIMIT


def test_premium_limit_does_not_apply_to_sells() -> None:
    spent = book(premium_spent=Decimal("1000000"))
    sell = option_order("sell", price=Decimal("50.00"))

    assert evaluate(option_input(order=sell, option_sell=True, option_book=spent)).allowed is True
    assert rule(option_input(option_book=spent)) == PREMIUM_LIMIT


def test_put_buys_obey_the_same_rules() -> None:
    put = option_input(instrument=option_instrument(kind="put"))

    assert evaluate(put).allowed is True
    assert rule(replace(put, options_enabled=False)) == OPTIONS_OFF


# ---- 순서 ---------------------------------------------------------------------------


def test_option_rules_stop_at_the_first_block_in_order() -> None:
    inp = option_input(order=option_order("sell"), **EVERYTHING_OFF)
    full = book(long_total=3, short_total=2, premium_spent=Decimal("1000000"))

    assert rule(inp) == OPTIONS_OFF
    inp = replace(inp, options_enabled=True)
    assert rule(inp) == LIMITS_NOT_SET
    inp = replace(inp, limits=LIMITS_WITH_OPTIONS)
    assert rule(inp) == OPTION_BOOK_UNKNOWN
    inp = replace(inp, option_book=full)
    assert rule(inp) == OPTION_SELL_OFF
    inp = replace(inp, option_sell=True)
    assert rule(inp) == OPTION_SHORT_LIMIT
    inp = replace(inp, option_book=replace(full, short_total=1))
    assert rule(inp) == OPTION_POSITION_LIMIT
    # 매수로 바꾸면 매도 규칙은 건너뛰고 계약 합계, 그다음 프리미엄을 본다.
    buy = replace(inp, order=option_order())
    assert rule(buy) == OPTION_POSITION_LIMIT
    assert rule(replace(buy, option_book=replace(full, long_total=0, short_total=0))) == (
        PREMIUM_LIMIT
    )


def test_common_rules_come_before_the_option_rules() -> None:
    off = option_input(options_enabled=False)
    late = datetime(2026, 11, 25, 15, 45, tzinfo=KST)

    assert rule(replace(off, now=late)) == TRADING_HOURS
    assert rule(replace(off, order=option_order(quantity=11))) == MAX_ORDER_QTY


def test_daily_loss_and_margin_still_apply_after_the_option_rules() -> None:
    assert rule(option_input(account=account(realized="-500000"))) == DAILY_LOSS
    assert rule(option_input(orderable_qty=0)) == MARGIN
    holding = account(realized="-500000", positions=[position("buy", 1, symbol=OPTION)])
    closing = option_input(order=option_order("sell"), account=holding, orderable_qty=0)
    assert evaluate(closing).allowed is True


def test_option_expiry_warning_uses_the_futures_rule() -> None:
    inp = option_input(instrument=option_instrument(expiry=date(2026, 11, 28)))

    d = evaluate(inp)

    assert d.allowed is True
    assert d.warnings == ["만기까지 3일 남았습니다."]
