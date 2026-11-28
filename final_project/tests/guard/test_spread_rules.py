"""묶음 파는 다리의 보호와 보호를 풀어 버리는 청산(`docs/options.md` "묶음 주문", M6).

기본 입력(`option_input`)은 옵션 묶음을 켜고 옵션 한도(계약 합계 4, 매도 합계 2)를 정한 사용자의
B016C725(코스피200 C 2026-12-10) 주문이다. 옵션 매도는 꺼져 있다. 장부의 계열별·종목별 수는 테스트가
직접 넣는다(계좌에서 세는 것은 `tests/services/test_option_orders.py`).
"""

from dataclasses import replace
from datetime import date
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
    option_order,
    position,
)

from nh_mcp.domain import (
    AccountState,
    ComboContext,
    ComboPlan,
    ComboRequest,
    ContractKind,
    Leg,
    OptionBook,
    OptionSeries,
    Order,
    Position,
    RiskInput,
    SeriesCount,
    Unbounded,
)
from nh_mcp.guard.risk import (
    COMBO_UNCOVERED,
    DAILY_LOSS,
    MARGIN,
    OPTION_BOOK_UNKNOWN,
    OPTION_POSITION_LIMIT,
    OPTION_SELL_OFF,
    OPTION_SHORT_LIMIT,
    OPTION_UNCOVER,
    evaluate,
)

DEC = date(2026, 12, 10)


def series(
    kind: ContractKind = "call", expiry: date = DEC, underlying: str = "KOSPI"
) -> OptionSeries:
    return OptionSeries(underlying, expiry, kind)


# 이 주문(B016C725)의 계열.
CALLS = series()
# 묶음의 사는 다리 720 콜. 파는 다리는 이 주문(725 콜)이다.
BOUGHT = "B016C720"
VERTICAL = ComboRequest(
    "domestic",
    (Leg(BOUGHT, "buy", Decimal("2.00")), Leg(OPTION, "sell", Decimal("1.00"))),
    1,
    Decimal(0),
)
STRADDLE = ComboRequest(
    "domestic",
    (Leg(OPTION, "sell", Decimal("1.50")), Leg("C016C725", "sell", Decimal("1.50"))),
    1,
    Decimal(0),
)
# 최대 손실 25만 원인 매수형 수직 스프레드와, 한정되지 않는 매도 스트래들.
SPREAD = ComboContext(
    VERTICAL,
    ComboPlan("debit_vertical", (0, 1), Decimal("1.00"), Decimal("1.00"), Decimal("250000")),
)
NAKED = ComboContext(
    STRADDLE,
    ComboPlan("short_straddle", (0, 1), Decimal("-3.00"), Decimal("-3.00"), Unbounded()),
)


def counted(
    where: OptionSeries = CALLS,
    long: int = 0,
    short: int = 0,
    pending_short: int = 0,
    closing: int = 0,
) -> OptionBook:
    """한 계열에만 보유·미체결 매도가 있는 장부. 합계 칸도 같은 수로 채운다."""
    return book(
        long_total=long,
        short_total=short,
        pending_short=pending_short,
        pending_increase=pending_short,
        series={where: SeriesCount(long, short, pending_short, closing)},
    )


def by_symbol(counts: dict[str, SeriesCount], where: OptionSeries = CALLS) -> OptionBook:
    """종목마다 센 장부. 모두 한 계열(where)이고, 계열 칸과 합계 칸은 종목 수의 합이다."""
    total = SeriesCount(0, 0, 0, 0)
    for count in counts.values():
        total = total.plus(count)
    return book(
        long_total=total.long,
        short_total=total.short,
        pending_short=total.pending_short,
        pending_increase=total.pending_short,
        series={where: total},
        symbols=counts,
    )


def bought(long: int = 1, closing: int = 0, short: int = 0, pending_short: int = 0) -> OptionBook:
    """사는 다리 720 콜 보유 long(그중 closing은 되파는 미체결 매도)과, 파는 다리 725 콜의 보유
    매도·미체결 새 매도.
    """
    return by_symbol(
        {BOUGHT: SeriesCount(long, 0, 0, closing), OPTION: SeriesCount(0, short, pending_short, 0)}
    )


def sell_leg(**changes: object) -> RiskInput:
    """매도를 켜지 않은 사용자의 묶음 파는 다리 B016C725 1계약. 사는 다리 1계약이 체결됐다."""
    base = option_input(order=option_order("sell"), combo=SPREAD, option_book=bought())
    return replace(base, **changes)


# ---- 묶음 파는 다리의 보호 ---------------------------------------------------------------


def test_sell_leg_passes_once_the_buy_leg_is_held() -> None:
    d = evaluate(sell_leg())

    assert (d.allowed, d.rule, d.increases_position) == (True, None, True)


def test_sell_leg_is_blocked_before_the_buy_leg_fills() -> None:
    d = evaluate(sell_leg(option_book=book()))

    assert d.rule == COMBO_UNCOVERED
    assert BOUGHT in d.message


def test_an_uncovered_sell_leg_is_blocked_even_with_selling_on() -> None:
    assert evaluate(sell_leg(option_book=book(), option_sell=True)).rule == COMBO_UNCOVERED


def test_the_same_sell_as_a_single_order_gets_no_exemption() -> None:
    assert evaluate(sell_leg(combo=None)).rule == OPTION_SELL_OFF


def test_an_unbounded_combo_gets_no_exemption() -> None:
    assert evaluate(sell_leg(combo=NAKED)).rule == OPTION_SELL_OFF


@pytest.mark.parametrize(
    ("symbol", "where"),
    [
        ("B016C745", CALLS),
        ("C016C720", series(kind="put")),
        ("B01701720", series(expiry=date(2027, 1, 14))),
        ("B056C720", series(underlying="MKOSPI")),  # 미니코스피 옵션(승수 5만)
    ],
    ids=["same_series_other_strike", "other_kind", "other_expiry", "other_underlying"],
)
def test_a_long_other_than_the_buy_leg_does_not_cover_the_sell_leg(
    symbol: str, where: OptionSeries
) -> None:
    longs = by_symbol({symbol: SeriesCount(1, 0, 0, 0)}, where)

    assert evaluate(sell_leg(option_book=longs)).rule == COMBO_UNCOVERED


@pytest.mark.parametrize(
    ("long", "short", "pending_short", "quantity", "allowed"),
    [
        (2, 1, 0, 1, True),  # 2 >= 1 + 0 + 1
        (2, 1, 1, 1, False),  # 미체결 새 매도를 센다
        (2, 0, 0, 2, True),
        (2, 0, 0, 3, False),  # 이번 수량을 센다
    ],
)
def test_the_cover_counts_held_shorts_pending_new_shorts_and_this_quantity(
    long: int, short: int, pending_short: int, quantity: int, allowed: bool
) -> None:
    wide = replace(LIMITS_WITH_OPTIONS, options=replace(OPTION_LIMITS, max_position=10))
    inp = sell_leg(
        order=option_order("sell", quantity=quantity),
        option_book=bought(long=long, short=short, pending_short=pending_short),
        limits=wide,
    )

    d = evaluate(inp)

    assert d.allowed is allowed
    assert d.rule == (None if allowed else COMBO_UNCOVERED)


def test_a_buy_leg_already_being_sold_does_not_cover_the_sell_leg() -> None:
    # 사는 다리 1을 되파는 미체결 매도가 있으면, 그 매도가 체결된 뒤 이 다리는 보호가 없다.
    assert evaluate(sell_leg(option_book=bought(long=1, closing=1))).rule == COMBO_UNCOVERED
    assert evaluate(sell_leg(option_book=bought(long=2, closing=1))).allowed is True


def test_one_long_does_not_cover_the_sells_of_two_combos() -> None:
    # 720 콜 1계약이 이미 다른 묶음의 730 콜 매도 1계약을 덮고 있다. 종목으로는 덮이지만 계열로는
    # 매수 1 < 매도 1 + 이번 1이다.
    covering_another = by_symbol(
        {BOUGHT: SeriesCount(1, 0, 0, 0), "B016C730": SeriesCount(0, 1, 0, 0)}
    )

    assert evaluate(sell_leg(option_book=covering_another)).rule == COMBO_UNCOVERED


def test_an_order_that_is_not_the_combos_sell_leg_gets_no_cover() -> None:
    other_sell = replace(VERTICAL, legs=(VERTICAL.legs[0], Leg("B016C730", "sell", Decimal(1))))

    d = evaluate(sell_leg(combo=replace(SPREAD, request=other_sell)))

    assert d.rule == COMBO_UNCOVERED


def test_the_exemption_also_skips_the_short_limit() -> None:
    no_shorts = replace(LIMITS_WITH_OPTIONS, options=replace(OPTION_LIMITS, max_short=0))

    assert evaluate(sell_leg(option_sell=True, limits=no_shorts)).allowed is True
    assert evaluate(sell_leg(option_sell=True, limits=no_shorts, combo=None)).rule == (
        OPTION_SHORT_LIMIT
    )


def test_position_limit_daily_loss_and_margin_still_apply_to_a_covered_sell_leg() -> None:
    # 계약 합계 한도 4: 보유 매수 4 + 이번 1 = 5.
    assert evaluate(sell_leg(option_book=bought(long=4))).rule == OPTION_POSITION_LIMIT
    assert evaluate(sell_leg(account=account(realized="-500000"))).rule == DAILY_LOSS
    assert evaluate(sell_leg(orderable_qty=0)).rule == MARGIN


# ---- 보호를 풀어 버리는 청산 -------------------------------------------------------------


def holding(
    longs: int = 1, short_symbol: str | None = "B016C730", resting: Order | None = None
) -> AccountState:
    """B016C725 매수 longs계약과, 있으면 같은 계열의 다른 종목 매도 1계약을 든 계좌."""
    rows: list[Position] = [position("buy", longs, symbol=OPTION)]
    if short_symbol is not None:
        rows.append(position("sell", 1, symbol=short_symbol))
    return account(positions=rows, open_orders=[] if resting is None else [resting])


def close(quantity: int = 1, **changes: object) -> RiskInput:
    """매도를 켜지 않은 사용자가 보유 매수 B016C725를 되파는 청산."""
    return option_input(order=option_order("sell", quantity=quantity), **changes)


def test_closing_the_long_that_covers_a_short_is_blocked_when_selling_is_off() -> None:
    d = evaluate(close(option_book=counted(long=1, short=1), account=holding()))

    assert (d.allowed, d.rule, d.increases_position) == (False, OPTION_UNCOVER, False)
    assert (
        "보유 매수 1 - 미체결 매수 청산 0 - 이번 주문 1 = 0계약 < 보유 매도 1 + 미체결 새 매도 0 "
        "= 1계약" in d.message
    )


def test_closing_the_long_is_blocked_while_a_new_sell_rests_in_the_series() -> None:
    # 같은 계열에 걸어 둔 새 매도는 체결되면 보유 매도가 된다. 그것을 덮을 매수도 되팔 수 없다.
    inp = close(option_book=counted(long=1, pending_short=1), account=holding(short_symbol=None))

    d = evaluate(inp)

    assert (d.allowed, d.rule) == (False, OPTION_UNCOVER)
    assert "보유 매도 0 + 미체결 새 매도 1 = 1계약" in d.message


def test_buying_back_the_short_passes() -> None:
    short_held = account(
        positions=[position("sell", 1, symbol=OPTION), position("buy", 1, symbol="B016C720")]
    )
    inp = option_input(order=option_order("buy"), account=short_held)

    d = evaluate(replace(inp, option_book=counted(long=1, short=1)))

    assert (d.allowed, d.increases_position) == (True, False)


def test_closing_the_long_passes_when_selling_is_on() -> None:
    inp = close(option_book=counted(long=1, short=1), option_sell=True, account=holding())

    assert evaluate(inp).allowed is True


@pytest.mark.parametrize(
    "other", [series(kind="put"), series(expiry=date(2027, 1, 14))], ids=["put", "other_expiry"]
)
def test_closing_the_long_passes_when_the_short_is_in_another_series(other: OptionSeries) -> None:
    two_series = book(
        long_total=1,
        short_total=1,
        series={CALLS: SeriesCount(1, 0, 0, 0), other: SeriesCount(0, 1, 0, 0)},
    )

    assert evaluate(close(option_book=two_series, account=holding())).allowed is True


def test_closing_the_long_passes_when_nothing_is_short() -> None:
    inp = close(option_book=counted(long=1), account=holding(short_symbol=None))

    assert evaluate(inp).allowed is True


@pytest.mark.parametrize(("quantity", "allowed"), [(1, True), (2, False)])
def test_the_longs_left_may_equal_the_shorts(quantity: int, allowed: bool) -> None:
    # 매수 2 - 이번 수량 >= 매도 1이면 통과한다(같으면 통과).
    inp = close(quantity, option_book=counted(long=2, short=1), account=holding(longs=2))

    d = evaluate(inp)

    assert d.allowed is allowed
    assert d.rule == (None if allowed else OPTION_UNCOVER)


def test_resting_closing_sells_of_longs_are_counted() -> None:
    resting = open_order("sell", 1, symbol=OPTION)
    current = counted(long=2, short=1, closing=1)

    # 매수 2 - 미체결 청산 1 - 이번 1 = 0 < 매도 1. 미체결 청산이 없으면 1 >= 1로 통과한다.
    assert (
        evaluate(close(option_book=current, account=holding(2, resting=resting))).rule
        == OPTION_UNCOVER
    )
    assert evaluate(close(option_book=counted(long=2, short=1), account=holding(2))).allowed is True


@pytest.mark.parametrize("stale_sell_switch", [False, True])
def test_the_rule_applies_with_the_options_bundle_and_option_limits_off(
    stale_sell_switch: bool,
) -> None:
    # "옵션"이 꺼져 있으면 "옵션 매도" 행이 남아 있어도 꺼진 것으로 본다.
    inp = close(
        option_book=counted(long=1, short=1),
        options_enabled=False,
        option_sell=stale_sell_switch,
        limits=replace(LIMITS_WITH_OPTIONS, options=None),
        account=holding(),
    )

    assert evaluate(inp).rule == OPTION_UNCOVER


def test_an_unknown_book_blocks_closing_a_long_only_while_something_is_short() -> None:
    assert evaluate(close(option_book=None, account=holding())).rule == OPTION_BOOK_UNKNOWN
    assert evaluate(close(option_book=None, account=holding(short_symbol=None))).allowed is True
    # 다른 종목에 걸어 둔 매도도 체결되면 보유 매도가 된다.
    resting = open_order("sell", symbol="B016C730")
    blocked = evaluate(close(option_book=None, account=holding(short_symbol=None, resting=resting)))
    assert blocked.rule == OPTION_BOOK_UNKNOWN
    # 판 옵션을 되사는 청산은 장부를 몰라도 된다.
    short_held = account(positions=[position("sell", 1, symbol=OPTION)])
    assert evaluate(option_input(account=short_held, option_book=None)).allowed is True
