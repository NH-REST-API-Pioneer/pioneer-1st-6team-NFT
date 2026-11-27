from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from builders import (
    account,
    instrument,
    open_order,
    order,
    position,
    risk_input,
)

from nh_mcp.domain import KST, OrderRequest, RiskLimits, TickBand
from nh_mcp.guard.risk import (
    ACCOUNT_UNKNOWN,
    DAILY_LOSS,
    DEMO_ONLY,
    EXPIRY_WARN_DAYS,
    MARGIN,
    MAX_ORDER_QTY,
    MAX_POSITION,
    ORDER_FORMAT,
    TRADING_HOURS,
    evaluate,
    increases_position,
    nearest_ticks,
)


def at(day: int, hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, second, tzinfo=KST)


def test_default_input_passes_without_warnings() -> None:
    d = evaluate(risk_input())
    assert d.allowed is True
    assert d.rule is None
    assert d.warnings == []
    assert d.increases_position is True


def test_limits_not_set_blocks_before_every_rule() -> None:
    d = evaluate(risk_input(limits=None, orders_enabled=False))
    assert (d.allowed, d.rule) == (False, "limits_not_set")


# 1. demo_only
def test_demo_only_blocks_when_switch_off() -> None:
    d = evaluate(risk_input(orders_enabled=False))
    assert (d.allowed, d.rule) == (False, DEMO_ONLY)


# 1-2. account_unknown: 계좌를 다 읽지 못하면 줄이는 주문도 막는다
ACCOUNT_UNKNOWN_MESSAGE = (
    "계좌 정보를 다 읽지 못해 주문을 막았습니다. "
    "잠시 뒤 다시 시도하거나 get_account로 확인해 주세요."
)


def test_incomplete_account_blocks_increasing_and_reducing_orders() -> None:
    reducing = risk_input(
        order=order(side="sell"), account=account(positions=[position()], complete=False)
    )
    increasing = risk_input(account=account(complete=False))

    for inp in (reducing, increasing):
        d = evaluate(inp)
        assert (d.allowed, d.rule, d.message) == (False, ACCOUNT_UNKNOWN, ACCOUNT_UNKNOWN_MESSAGE)
    assert evaluate(reducing).increases_position is False


def test_account_unknown_comes_after_demo_only_and_before_order_format() -> None:
    incomplete = account(complete=False)

    assert evaluate(risk_input(account=incomplete, orders_enabled=False)).rule == DEMO_ONLY
    assert evaluate(risk_input(account=incomplete, order=order(quantity=0))).rule == (
        ACCOUNT_UNKNOWN
    )


# 2. order_format
@pytest.mark.parametrize(
    "bad",
    [
        order(quantity=0),
        order(price=None),
        order(price=Decimal("0")),
        order(price=Decimal("-0.05")),
        order(type_="market", price=Decimal("400.05")),
    ],
)
def test_order_format_blocks(bad: OrderRequest) -> None:
    d = evaluate(risk_input(order=bad))
    assert (d.allowed, d.rule) == (False, ORDER_FORMAT)


def test_order_format_tick_mismatch_names_nearest_ticks() -> None:
    d = evaluate(risk_input(order=order(price=Decimal("400.07"))))
    assert (d.allowed, d.rule) == (False, ORDER_FORMAT)
    assert "400.05" in d.message
    assert "400.10" in d.message


def test_order_format_blocks_without_instrument() -> None:
    d = evaluate(risk_input(instrument=None))
    assert (d.allowed, d.rule) == (False, ORDER_FORMAT)


def test_order_format_passes_on_tick() -> None:
    assert evaluate(risk_input(order=order(price=Decimal("400.05")))).allowed is True


def test_market_order_passes_format_with_one_warning() -> None:
    d = evaluate(risk_input(order=order(type_="market", price=None)))
    assert d.allowed is True
    assert len(d.warnings) == 1


# 코스피200옵션 호가 단위: 10 미만 0.01, 10 이상 0.05
OPTION_TICKS = (TickBand(Decimal(0), Decimal("0.01")), TickBand(Decimal(10), Decimal("0.05")))


@pytest.mark.parametrize(
    ("price", "allowed"),
    [
        ("0.01", True),
        ("9.98", True),
        ("9.99", True),
        ("10.00", True),
        ("10.05", True),
        ("32.15", True),
        ("9.995", False),
        ("10.01", False),
        ("10.03", False),
        ("32.16", False),
    ],
)
def test_order_format_uses_the_tick_of_the_price_band(price: str, allowed: bool) -> None:
    option = replace(instrument(), ticks=OPTION_TICKS)

    d = evaluate(risk_input(instrument=option, order=order(price=Decimal(price))))

    assert (d.allowed, d.rule) == (allowed, None if allowed else ORDER_FORMAT)


def test_tick_mismatch_above_a_band_edge_names_ticks_of_that_band() -> None:
    option = replace(instrument(), ticks=OPTION_TICKS)

    d = evaluate(risk_input(instrument=option, order=order(price=Decimal("10.03"))))

    assert "10.00" in d.message
    assert "10.05" in d.message


def test_order_format_blocks_when_no_band_covers_the_price() -> None:
    no_ticks = replace(instrument(), ticks=())

    d = evaluate(risk_input(instrument=no_ticks))

    assert (d.allowed, d.rule) == (False, ORDER_FORMAT)


def test_nearest_ticks() -> None:
    tick = Decimal("0.05")
    assert nearest_ticks(Decimal("400.07"), tick) == (Decimal("400.05"), Decimal("400.10"))
    assert nearest_ticks(Decimal("400.05"), tick) == (Decimal("400.05"), Decimal("400.05"))


# 3. trading_hours
@pytest.mark.parametrize(
    ("now", "allowed"),
    [
        (at(25, 8, 44, 59), False),
        (at(25, 8, 45), True),
        (at(25, 15, 44, 59), True),
        (at(25, 15, 45), False),
        (at(28, 10, 0), False),  # 토요일
        (at(29, 10, 0), False),  # 일요일
    ],
)
def test_trading_hours(now: datetime, allowed: bool) -> None:
    d = evaluate(risk_input(now=now))
    assert d.allowed is allowed
    if not allowed:
        assert d.rule == TRADING_HOURS


def test_trading_hours_converts_other_timezones_to_kst() -> None:
    # 2026-11-25 01:00 UTC = 10:00 KST
    assert evaluate(risk_input(now=datetime(2026, 11, 25, 1, 0, tzinfo=UTC))).allowed is True
    # 2026-11-24 23:50 UTC = 08:50 KST(수요일): UTC로는 화요일이어도 통과
    assert evaluate(risk_input(now=datetime(2026, 11, 24, 23, 50, tzinfo=UTC))).allowed is True
    # 2026-11-27 16:00 UTC = 토요일 01:00 KST
    d = evaluate(risk_input(now=datetime(2026, 11, 27, 16, 0, tzinfo=UTC)))
    assert (d.allowed, d.rule) == (False, TRADING_HOURS)


# 4. max_order_qty
def test_max_order_qty_boundary() -> None:
    assert evaluate(risk_input(order=order(quantity=2))).allowed is True
    d = evaluate(risk_input(order=order(quantity=3)))
    assert (d.allowed, d.rule) == (False, MAX_ORDER_QTY)


# 5. max_position
def test_max_position_counts_holding_and_same_side_open_orders() -> None:
    acc = account(positions=[position("buy", 1)], open_orders=[open_order("buy", 1)])
    assert evaluate(risk_input(account=acc, order=order(quantity=1))).allowed is True
    d = evaluate(risk_input(account=acc, order=order(quantity=2)))
    assert (d.allowed, d.rule) == (False, MAX_POSITION)


def test_max_position_ignores_opposite_side_open_orders() -> None:
    acc = account(positions=[position("buy", 1)], open_orders=[open_order("sell", 5)])
    assert evaluate(risk_input(account=acc, order=order(quantity=2))).allowed is True


def test_max_position_ignores_other_symbols() -> None:
    acc = account(open_orders=[open_order("buy", 5, symbol="A016D000")])
    assert evaluate(risk_input(account=acc, order=order(quantity=2))).allowed is True


# 6. daily_loss
def test_daily_loss_boundary() -> None:
    assert evaluate(risk_input(account=account("-300000", "-199999"))).allowed is True
    d = evaluate(risk_input(account=account("-300000", "-200000")))
    assert (d.allowed, d.rule) == (False, DAILY_LOSS)


@pytest.mark.parametrize(("realized", "unrealized"), [(None, "0"), ("0", None), (None, None)])
def test_daily_loss_blocks_missing_pnl(realized: str | None, unrealized: str | None) -> None:
    d = evaluate(risk_input(account=account(realized, unrealized)))
    assert (d.allowed, d.rule) == (False, DAILY_LOSS)


# 7. margin
def test_margin_boundary() -> None:
    assert evaluate(risk_input(orderable_qty=1)).allowed is True
    for qty in (0, None):
        d = evaluate(risk_input(orderable_qty=qty))
        assert (d.allowed, d.rule) == (False, MARGIN)


# 청산 허용
def test_reducing_order_passes_despite_loss_margin_and_position() -> None:
    acc = account("-600000", "0", positions=[position("buy", 2, 2)])
    d = evaluate(
        risk_input(
            account=acc,
            order=order("sell", quantity=2),
            orderable_qty=0,
            limits=RiskLimits(Decimal("500000"), 1, 2),
        )
    )
    assert d.allowed is True
    assert d.increases_position is False


def test_reducing_order_still_obeys_trading_hours() -> None:
    acc = account(positions=[position("buy", 2, 2)])
    d = evaluate(risk_input(account=acc, order=order("sell", quantity=2), now=at(25, 16, 0)))
    assert (d.allowed, d.rule, d.increases_position) == (False, TRADING_HOURS, False)


def test_flip_over_order_counts_as_increasing_and_hits_loss_limit() -> None:
    acc = account("-600000", "0", positions=[position("buy", 2, 2)])
    d = evaluate(
        risk_input(
            account=acc,
            order=order("sell", quantity=3),
            limits=RiskLimits(Decimal("500000"), 3, 3),
        )
    )
    assert (d.allowed, d.rule, d.increases_position) == (False, DAILY_LOSS, True)


def test_reducing_is_bounded_by_closable_not_quantity() -> None:
    held = [position("buy", 2, 1)]
    assert increases_position(order("sell", quantity=1), held, []) is False
    assert increases_position(order("sell", quantity=2), held, []) is True


def test_same_side_or_no_position_is_increasing() -> None:
    assert increases_position(order("buy"), [position("buy", 2)], []) is True
    assert increases_position(order("buy"), [], []) is True


def test_resting_closing_order_is_subtracted_from_closable() -> None:
    held = [position("buy", 2, 2)]
    resting = [open_order("sell", 2)]
    assert increases_position(order("sell", quantity=2), held, resting) is True
    assert increases_position(order("sell", quantity=1), held, [open_order("sell", 1)]) is False
    # 반대 방향 미체결은 빼지 않는다
    assert increases_position(order("sell", quantity=2), held, [open_order("buy", 2)]) is False
    # 다른 종목 미체결은 빼지 않는다
    other = [open_order("sell", 2, symbol="A016D000")]
    assert increases_position(order("sell", quantity=2), held, other) is False


def test_resting_closing_order_makes_new_close_hit_loss_limit() -> None:
    acc = account(
        "-600000", "0", positions=[position("buy", 2, 2)], open_orders=[open_order("sell", 2)]
    )
    d = evaluate(
        risk_input(
            account=acc,
            order=order("sell", quantity=2),
            limits=RiskLimits(Decimal("500000"), 5, 2),
        )
    )
    assert (d.allowed, d.rule, d.increases_position) == (False, DAILY_LOSS, True)


def test_opposite_side_position_rows_are_summed() -> None:
    rows = [position("buy", 1, 1), position("buy", 1, 1)]
    assert increases_position(order("sell", quantity=2), rows, []) is False
    assert increases_position(order("sell", quantity=3), rows, []) is True


def test_same_side_position_listed_first_does_not_change_result() -> None:
    mixed = [position("sell", 5, 5), position("buy", 2, 2)]
    assert increases_position(order("sell", quantity=2), mixed, []) is False
    assert increases_position(order("sell", quantity=3), mixed, []) is True


# 경고
def test_expiry_warning_boundary() -> None:
    assert EXPIRY_WARN_DAYS == 3
    three = evaluate(risk_input(instrument=instrument(date(2026, 11, 28))))
    assert three.allowed is True
    assert len(three.warnings) == 1
    assert evaluate(risk_input(instrument=instrument(date(2026, 11, 29)))).warnings == []


@pytest.mark.parametrize(
    ("now", "warned"),
    [(at(25, 15, 34, 59), False), (at(25, 15, 35), True), (at(25, 15, 44, 59), True)],
)
def test_closing_auction_warning(now: datetime, warned: bool) -> None:
    d = evaluate(risk_input(now=now))
    assert d.allowed is True
    expected = ["종가 단일가 구간입니다. 15:45에 종가로 한 번에 체결됩니다."]
    assert d.warnings == (expected if warned else [])


def test_warnings_do_not_block_even_when_stacked() -> None:
    d = evaluate(
        risk_input(
            order=order(type_="market", price=None),
            now=at(25, 15, 40),
            instrument=instrument(date(2026, 11, 26)),
        )
    )
    assert d.allowed is True
    assert len(d.warnings) == 3
