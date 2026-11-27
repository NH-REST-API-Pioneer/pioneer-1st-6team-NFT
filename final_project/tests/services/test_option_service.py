"""등가격 고르기와 위아래 자르기."""

from decimal import Decimal

from nh_mcp.domain import ChainRow
from nh_mcp.services.options import near_money

ROWS = tuple(ChainRow(Decimal(k), None, None) for k in ("700", "702.5", "705", "707.5", "710"))


def strikes(rows: tuple[ChainRow, ...]) -> list[str]:
    return [str(r.strike) for r in rows]


def test_at_the_money_is_the_strike_nearest_the_spot() -> None:
    atm, rows = near_money(ROWS, Decimal("703.4"), 1)
    assert atm == Decimal("702.5")
    assert strikes(rows) == ["700", "702.5", "705"]


def test_a_spot_halfway_between_two_strikes_takes_the_lower() -> None:
    atm, _ = near_money(ROWS, Decimal("703.75"), 1)
    assert atm == Decimal("702.5")


def test_the_window_stops_at_the_ends_of_the_board() -> None:
    atm, rows = near_money(ROWS, Decimal("650"), 2)
    assert atm == Decimal("700")
    assert strikes(rows) == ["700", "702.5", "705"]
    _, rows = near_money(ROWS, Decimal("709"), 40)
    assert strikes(rows) == strikes(ROWS)


def test_zero_around_gives_the_at_the_money_row_only() -> None:
    _, rows = near_money(ROWS, Decimal("707"), 0)
    assert strikes(rows) == ["707.5"]
