from decimal import Decimal

from nh_mcp.domain import RiskLimits
from nh_mcp.guard.env import orders_allowed


def test_demo_with_switch_on_allows_orders() -> None:
    assert orders_allowed("demo", True) is True


def test_switch_off_blocks_orders() -> None:
    assert orders_allowed("demo", False) is False


def limits(daily_loss: str = "100", max_position: int = 3, max_order_qty: int = 2) -> RiskLimits:
    return RiskLimits(Decimal(daily_loss), max_position, max_order_qty)


def test_same_limits_do_not_raise() -> None:
    assert limits().raises(limits()) is False


def test_bigger_max_position_raises() -> None:
    assert limits().raises(limits(max_position=4)) is True


def test_bigger_max_order_qty_raises() -> None:
    assert limits().raises(limits(max_order_qty=3)) is True


def test_bigger_daily_loss_raises() -> None:
    assert limits().raises(limits(daily_loss="101")) is True


def test_smaller_daily_loss_does_not_raise() -> None:
    assert limits().raises(limits(daily_loss="99")) is False
