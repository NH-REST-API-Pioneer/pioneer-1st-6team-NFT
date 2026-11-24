import asyncio
from typing import Any

from conftest import Serve
from fakes import World
from fastmcp import Client


async def search(serve: Serve, world: World, args: dict[str, Any]) -> dict[str, Any]:
    async with serve(world.mcp) as url, Client(url, auth="admin-token") as client:
        content = (await client.call_tool("search_symbol", args)).structured_content
    assert content is not None
    return content


def test_search_symbol_returns_codes_expiry_and_multiplier(serve: Serve) -> None:
    result = asyncio.run(search(serve, World(), {"query": "코스피200"}))

    assert result["ok"] is True
    first = result["data"]["symbols"][0]
    assert first == {
        "symbol": "A016C000",
        "name": "코스피200 F 202612",
        "underlying": "KOSPI",
        "kind": "future",
        "expiry": "2026-12-10",
        "multiplier": "250000",
    }
    assert result["data"]["truncated"] is False
    assert result["data"]["master_date"] == "2026-12-01"


def test_search_symbol_rejects_bad_input(serve: Serve) -> None:
    world = World()
    overseas = asyncio.run(search(serve, world, {"query": "ES", "market": "overseas"}))
    empty = asyncio.run(search(serve, world, {"query": "  "}))

    for result in (overseas, empty):
        assert result["ok"] is False
        assert result["error"]["code"] == "INVALID_INPUT"


def test_search_symbol_says_when_the_list_is_unavailable(serve: Serve) -> None:
    world = World()
    world.master.down = True
    result = asyncio.run(search(serve, world, {"query": "코스피"}))

    assert result["ok"] is False
    assert result["error"]["code"] == "TEMPORARILY_UNAVAILABLE"
    assert result["error"]["retryable"] is True
