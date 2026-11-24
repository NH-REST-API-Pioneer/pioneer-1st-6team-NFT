"""모든 도구가 같은 응답 형식(ok / data / error / meta)으로 답하는지 도구 목록으로 확인한다.

주문 도구까지 보려고 주문 스위치를 켠 서버로 본다.
"""

import asyncio

from conftest import Serve
from fakes import World
from fastmcp import Client
from mcp_types import Tool


async def listed(serve: Serve) -> list[Tool]:
    async with (
        serve(World(orders_enabled=True).mcp) as url,
        Client(url, auth="admin-token") as client,
    ):
        return await client.list_tools()


def test_every_tool_answers_with_the_envelope_object(serve: Serve) -> None:
    tools = asyncio.run(listed(serve))
    assert len(tools) >= 4

    for tool in tools:
        schema = tool.output_schema
        assert schema is not None, tool.name
        # 결과를 {"result": ...}로 한 번 더 감싸면 AI가 받는 모양이 달라진다.
        assert schema.get("type") == "object", tool.name
        assert "x-fastmcp-wrap-result" not in schema, tool.name
        assert {"ok", "meta"} <= set(schema.get("required", [])), tool.name


def test_every_tool_declares_whether_it_changes_anything(serve: Serve) -> None:
    for tool in asyncio.run(listed(serve)):
        assert tool.annotations is not None, tool.name
        assert tool.annotations.read_only_hint is not None, tool.name
