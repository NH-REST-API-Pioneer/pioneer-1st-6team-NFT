"""주문 가능 환경 판정."""

from nh_mcp.domain import Env


def orders_allowed(env: Env, orders_enabled: bool) -> bool:
    """모의 환경이고 주문 스위치가 켜져 있을 때만 True."""
    return env == "demo" and orders_enabled
