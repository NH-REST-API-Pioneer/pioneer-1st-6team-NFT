import asyncio
from typing import Any

from conftest import Serve
from fakes import World
from fastmcp import Client

from nh_mcp.nh.client import Credentials
from nh_mcp.store import users

FAKE_KEY = "fake-app-key-1234"


async def call(serve: Serve, world: World, token: str, tool: str) -> dict[str, Any]:
    async with serve(world.mcp) as url, Client(url, auth=token) as client:
        content = (await client.call_tool(tool, {})).structured_content
    assert content is not None
    return content


def register(world: World, sub: str, email: str) -> None:
    user = users.get_or_create(world.conn, sub, email, world.clock())
    credentials = Credentials(FAKE_KEY, "fake-app-secret-0000")
    asyncio.run(world.store.register(user, "domestic", credentials))


def test_status_without_keys_points_to_the_registration_page(serve: Serve) -> None:
    world = World()
    result = asyncio.run(call(serve, world, "admin-token", "get_connection_status"))

    assert result == {
        "ok": True,
        "data": {
            "markets": {"domestic": {"registered": False}, "overseas": {"registered": False}},
            "keys_url": "http://localhost:8000/keys",
        },
        "meta": {"env": "demo", "as_of": "2026-12-01T10:00:00+09:00"},
    }


def test_status_shows_only_last4_and_only_my_keys(serve: Serve) -> None:
    world = World()
    register(world, "g-admin", "admin@example.com")

    mine = asyncio.run(call(serve, world, "admin-token", "get_connection_status"))
    other = asyncio.run(call(serve, world, "second-admin-token", "get_connection_status"))

    assert mine["data"]["markets"]["domestic"] == {"registered": True, "app_key_last4": "1234"}
    assert other["data"]["markets"]["domestic"] == {"registered": False}
    assert FAKE_KEY not in str(mine)


def test_disconnect_removes_only_my_keys(serve: Serve) -> None:
    world = World()
    register(world, "g-admin", "admin@example.com")
    register(world, "g-admin2", "second@example.com")

    result = asyncio.run(call(serve, world, "admin-token", "disconnect_account"))

    assert result["ok"] is True
    me = users.find_by_google_sub(world.conn, "g-admin")
    other = users.find_by_google_sub(world.conn, "g-admin2")
    assert me is not None and other is not None
    assert world.store.summaries(me) == []
    assert len(world.store.summaries(other)) == 1
