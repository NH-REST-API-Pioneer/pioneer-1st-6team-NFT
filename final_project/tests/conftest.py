import asyncio
import contextlib
import socket
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager

import pytest
from fastmcp import FastMCP

type Serve = Callable[..., AbstractAsyncContextManager[str]]


@contextlib.asynccontextmanager
async def serving(
    server: FastMCP,
    allowed_hosts: list[str] | None = None,
    allowed_origins: list[str] | None = None,
) -> AsyncGenerator[str]:
    """서버를 실제 HTTP로 띄우고 MCP 주소를 돌려준다."""
    # 미리 열어 둔 소켓을 넘겨 포트 경합 없이 띄운다.
    # listen 중이라 서버가 뜨기 전에 들어온 연결도 대기열에서 기다린다.
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    port: int = sock.getsockname()[1]
    running = asyncio.create_task(
        server.run_http_async(
            show_banner=False,
            log_level="warning",
            sockets=[sock],
            # 운영 서버처럼 Host 머리말을 거를 때만 켠다.
            host_origin_protection=allowed_hosts is not None,
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
        )
    )
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        running.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await running
        sock.close()


@pytest.fixture
def serve() -> Serve:
    return serving
