"""녹화한 모의 서버 응답(2026-11-24 장중, A016C000)으로 도구를 돌린다.

가짜 응답은 명세를 읽고 만든 것이라, 실제 응답 모양과 어긋나도 테스트가 통과할 수 있다.
여기서는 NH가 실제로 돌려준 본문을 그대로 넣는다.
"""

import asyncio
from pathlib import Path
from typing import Any

from conftest import Serve
from fakes import World
from fastmcp import Client

from nh_mcp.nh.client import Credentials
from nh_mcp.nh.recording import Fixture, replay_transport
from nh_mcp.store import users

RECORDED = Path(__file__).resolve().parents[1] / "fixtures" / "nh" / "domestic"


def world_replaying(*names: str) -> World:
    fixtures = [Fixture.load(RECORDED / f"{name}.json") for name in names]
    world = World(nh=replay_transport(fixtures))
    user = users.get_or_create(world.conn, "g-admin", "admin@example.com", world.clock())
    credentials = Credentials("fake-app-key-1234", "fake-app-secret-0000")
    asyncio.run(world.store.register(user, "domestic", credentials))
    return world


def run(serve: Serve, world: World, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    async def call() -> dict[str, Any]:
        async with serve(world.mcp) as url, Client(url, auth="admin-token") as client:
            content = (await client.call_tool(tool, args)).structured_content
        assert content is not None
        return content

    return asyncio.run(call())


def test_quote_from_recorded_price_and_book(serve: Serve) -> None:
    world = world_replaying("price", "price-bid")
    result = run(serve, world, "get_quote", {"symbol": "A016C000"})

    assert result["ok"] is True
    data = result["data"]
    assert data["name"] == "코스피200 F 202612"
    assert (data["last"], data["change"], data["volume"]) == ("784.55", "57.90", 104109)
    assert (data["upper_limit"], data["lower_limit"]) == ("835.60", "668.55")
    assert len(data["book"]) == 10
    assert data["book"][0] == {"bid": "784.50", "bid_qty": 3, "ask": "784.60", "ask_qty": 3}


def test_empty_quote_for_a_symbol_nh_does_not_know_is_blocked(serve: Serve) -> None:
    # 마스터에는 있지만 NH가 빈 칸만 돌려준 경우. 호가는 녹화에 없으므로 부르면 실패한다.
    world = world_replaying("price_unknown_symbol")
    result = run(serve, world, "get_quote", {"symbol": "A016C000"})

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"


def test_minute_candles_carry_the_volume_of_each_bar(serve: Serve) -> None:
    world = world_replaying("chart-minute")
    period = {"start": "2026-11-24", "end": "2026-11-24"}
    args = {"symbol": "A016C000", "interval": "minute", "period": period, "limit": 30}
    result = run(serve, world, "get_candles", args)

    candles = result["data"]["candles"]
    assert len(candles) == 30
    # 오래된 것부터 온다. 마지막 봉은 13:35.
    assert candles[-1]["at"] == "2026-11-24T13:35:00+09:00"
    assert (candles[-1]["close"], candles[-1]["volume"]) == ("784.55", 75)
    assert candles[-2]["volume"] == 162


def test_daily_candles_from_recording(serve: Serve) -> None:
    world = world_replaying("chart-daily")
    period = {"start": "2026-10-01", "end": "2026-11-24"}
    args = {"symbol": "A016C000", "interval": "day", "period": period, "limit": 30}
    result = run(serve, world, "get_candles", args)

    candles = result["data"]["candles"]
    assert len(candles) == 30
    assert candles[-1]["at"].startswith("2026-11-24")
    assert (candles[-1]["close"], candles[-1]["volume"]) == ("784.55", 104109)


def test_account_of_an_empty_demo_account(serve: Serve) -> None:
    world = world_replaying("deposit", "open-interest", "order-history")
    result = run(serve, world, "get_account", {})

    data = result["data"]
    # 앞자리 0으로 채운 금액도 Decimal로 읽는다.
    assert (data["deposit"], data["orderable_amount"]) == ("100000000.000", "100000000.000")
    assert data["realized_pnl"] == "0.000"
    assert data["positions"] == [] and data["open_orders"] == []
    assert data["complete"] is True


def test_orders_of_a_day_without_orders(serve: Serve) -> None:
    world = world_replaying("order-history", "order-error")
    result = run(serve, world, "get_orders", {"day": "2026-11-24"})

    assert result["ok"] is True
    assert result["data"]["orders"] == [] and result["data"]["order_errors"] == []
