"""실행 하나의 장부(포지션·오늘 손익)와 주문 내역에서 체결 찾기."""

from datetime import date, timedelta
from decimal import Decimal

from nh_mcp.runner.book import Resting, RunBook, legs, track
from nh_mcp.services.orders import same_order_no
from nh_mcp.services.runs import NHOrder, NHOrderState

from .rig import SYMBOL, T0

MULTIPLIER = Decimal("250000")
DAY = date(2026, 12, 1)
LIMIT = timedelta(seconds=30)


def book() -> RunBook:
    return RunBook({SYMBOL: MULTIPLIER}, DAY)


def test_a_fill_that_crosses_zero_is_two_legs_like_the_backtest() -> None:
    assert legs(-1, "buy", 2) == [1, 1]
    assert legs(-1, "buy", 1) == [1]
    assert legs(0, "sell", 2) == [2]
    assert legs(2, "buy", 1) == [1]


def test_pnl_is_realized_plus_unrealized_at_the_last_price() -> None:
    b = book()
    b.fill(SYMBOL, "buy", 2, Decimal("780.00"))
    b.mark(SYMBOL, Decimal("781.00"))
    # (781 - 780) x 2 x 250000
    assert b.pnl() == Decimal("500000")
    b.fill(SYMBOL, "sell", 1, Decimal("782.00"))
    # 확정 (782 - 780) x 1 x 250000 + 평가 (782 - 780) x 1 x 250000
    assert b.pnl() == Decimal("1000000")
    assert b.position(SYMBOL) == 1


def test_a_reversal_realizes_the_old_side_and_opens_the_new_one() -> None:
    b = book()
    b.fill(SYMBOL, "sell", 1, Decimal("780.00"))
    assert b.fill(SYMBOL, "buy", 2, Decimal("779.00")) == [1, 1]
    assert b.position(SYMBOL) == 1
    assert b.pnl() == Decimal("250000")


def test_a_new_day_keeps_the_position_and_counts_pnl_from_the_last_price() -> None:
    b = book()
    b.fill(SYMBOL, "buy", 1, Decimal("780.00"))
    b.mark(SYMBOL, Decimal("775.00"))
    b.roll(DAY + timedelta(days=1))
    assert b.pnl() == Decimal(0)
    b.mark(SYMBOL, Decimal("776.00"))
    assert b.pnl() == Decimal("250000")


def row(filled: int, price: str | None, state: NHOrderState = "open", qty: int = 2) -> NHOrder:
    return NHOrder(
        order_no="0000000101",
        symbol=SYMBOL,
        side="buy",
        quantity=qty,
        filled=filled,
        filled_price=None if price is None else Decimal(price),
        remaining=qty - filled,
        state=state,
    )


def resting() -> Resting:
    return Resting(1, "101", SYMBOL, "buy", 2, T0)


def test_partial_fills_become_one_fill_each_at_its_own_price() -> None:
    order = resting()

    first = track([order], [row(1, "780.00")], T0, LIMIT, same_order_no)
    # 평균 780.025 x 2 = 1560.05, 앞 체결 780.00을 빼면 이번 체결 780.05.
    second = track([order], [row(2, "780.025", "filled")], T0, LIMIT, same_order_no)

    assert [(f.qty, f.price) for f in first.fills] == [(1, Decimal("780.00"))]
    assert first.done == frozenset()
    assert [(f.qty, f.price) for f in second.fills] == [(1, Decimal("780.05"))]
    assert second.done == {1}
    assert second.problem is None


def test_what_cannot_be_trusted_is_a_problem() -> None:
    assert track([resting()], [row(1, None)], T0, LIMIT, same_order_no).problem
    assert track([resting()], [row(2, "780", "filled", qty=3)], T0, LIMIT, same_order_no).problem
    shrunk = resting()
    shrunk.filled = 2
    assert track([shrunk], [row(1, "780")], T0, LIMIT, same_order_no).problem
    # 내역에 아직 없으면 잠깐 기다리고, 오래 없으면 문제다.
    assert track([resting()], [], T0 + LIMIT, LIMIT, same_order_no).problem is None
    late = T0 + LIMIT + timedelta(seconds=1)
    assert track([resting()], [], late, LIMIT, same_order_no).problem


def test_a_cancelled_order_with_nothing_left_is_done_without_a_fill() -> None:
    cancelled = NHOrder("0000000101", SYMBOL, "buy", 2, 0, None, 0, "open")

    found = track([resting()], [cancelled], T0, LIMIT, same_order_no)

    assert (found.fills, found.done, found.problem) == ([], {1}, None)
