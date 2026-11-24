import asyncio
import sqlite3
from datetime import UTC, datetime

import httpx2
import pytest
from conftest import Serve
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from mcp.shared.exceptions import MCPError

from nh_mcp.auth.access import AccessPolicy, make_gate
from nh_mcp.server import build_server
from nh_mcp.store import db, users

NOW = datetime(2026, 12, 1, 1, 15, 3, tzinfo=UTC)
ADMIN = "admin@example.com"


def claims(sub: str, email: str, verified: bool = True) -> dict[str, object]:
    return {
        "client_id": "test",
        "scopes": [],
        "sub": sub,
        "email": email,
        "email_verified": verified,
    }


TOKENS = {
    "admin-token": claims("g-admin", ADMIN),
    "outsider-token": claims("g-out", "outsider@example.com"),
    "unverified-token": claims("g-unv", ADMIN, verified=False),
}


def make_server(conn: sqlite3.Connection) -> FastMCP:
    # 테스트만 미리 정한 토큰으로 사용자를 흉내 낸다. 운영 서버는 Google 로그인만 쓴다.
    gate = make_gate(AccessPolicy(False, frozenset({ADMIN})), conn, lambda: NOW)
    return build_server(StaticTokenVerifier(TOKENS), gate, clock=lambda: NOW)


async def tools_and_ping(url: str, token: str) -> tuple[list[str], object]:
    async with Client(url, auth=token) as client:
        names = [tool.name for tool in await client.list_tools()]
        try:
            result: object = (await client.call_tool("ping", {})).structured_content
        except ToolError:
            result = "denied"
    return names, result


def test_admin_lists_and_calls_ping(serve: Serve) -> None:
    conn = db.connect(":memory:")

    async def scenario() -> tuple[list[str], object]:
        async with serve(make_server(conn)) as url:
            return await tools_and_ping(url, "admin-token")

    names, result = asyncio.run(scenario())
    assert names == ["ping"]
    assert result == {
        "ok": True,
        "data": {"pong": True},
        "meta": {"env": "demo", "as_of": "2026-12-01T10:15:03+09:00"},
    }


@pytest.mark.parametrize("token", [None, "made-up-token"])
def test_request_without_valid_login_is_refused(serve: Serve, token: str | None) -> None:
    conn = db.connect(":memory:")

    async def scenario() -> int:
        async with serve(make_server(conn)) as url:
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            async with httpx2.AsyncClient() as http:
                response = await http.post(
                    url,
                    headers={**headers, "Accept": "application/json, text/event-stream"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                )
            # MCP 클라이언트도 연결 단계에서 거부된다.
            with pytest.raises(MCPError):
                async with Client(url, auth=token) as client:
                    await client.list_tools()
            return response.status_code

    assert asyncio.run(scenario()) == 401


@pytest.mark.parametrize("token", ["outsider-token", "unverified-token"])
def test_logged_in_but_not_allowed_sees_no_tools(serve: Serve, token: str) -> None:
    conn = db.connect(":memory:")

    async def scenario() -> tuple[list[str], object]:
        async with serve(make_server(conn)) as url:
            return await tools_and_ping(url, token)

    assert asyncio.run(scenario()) == ([], "denied")
    # 막힌 사용자는 기록하지 않는다.
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


def test_suspended_admin_sees_no_tools(serve: Serve) -> None:
    conn = db.connect(":memory:")
    users.set_suspended(conn, users.get_or_create(conn, "g-admin", ADMIN, NOW), True)

    async def scenario() -> tuple[list[str], object]:
        async with serve(make_server(conn)) as url:
            return await tools_and_ping(url, "admin-token")

    assert asyncio.run(scenario()) == ([], "denied")
