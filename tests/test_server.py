import asyncio
import contextlib
import socket
from datetime import UTC, datetime

from fastmcp import Client

from nh_mcp.server import build_server

NOW = datetime(2026, 12, 1, 1, 15, 3, tzinfo=UTC)


def test_ping_over_http() -> None:
    asyncio.run(_ping_over_http())


async def _ping_over_http() -> None:
    server = build_server(clock=lambda: NOW)
    # 미리 열어 둔 소켓을 넘겨 포트 경합 없이 띄운다.
    # listen 중이라 서버가 뜨기 전에 들어온 연결도 대기열에서 기다린다.
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    port: int = sock.getsockname()[1]
    running = asyncio.create_task(
        server.run_http_async(show_banner=False, log_level="warning", sockets=[sock])
    )
    try:
        async with Client(f"http://127.0.0.1:{port}/mcp") as client:
            tools = await client.list_tools()
            result = await client.call_tool("ping", {})
    finally:
        running.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await running

    assert [tool.name for tool in tools] == ["ping"]
    assert result.structured_content == {
        "ok": True,
        "data": {"pong": True},
        "meta": {"env": "demo", "as_of": "2026-12-01T10:15:03+09:00"},
    }
