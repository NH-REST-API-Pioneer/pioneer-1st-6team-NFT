"""실행기 클라이언트. httpx2 MockTransport로 응답을 만들어 준다."""

import asyncio
import json
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal

import httpx2
import pytest

from nh_mcp.domain import KST, Candle, SandboxJob, SandboxResult
from nh_mcp.sandbox.client import TIMEOUT_SECONDS, SandboxBusy, SandboxClient, SandboxDown
from nh_mcp.sandbox.wire import MAX_DETAIL, Reply, bar_to_wire, reply_to_json, result_from_json

TOKEN = "secret-token-" + "z" * 30
START = datetime(2026, 11, 2, 9, 0, tzinfo=KST)
GOOD_BODY = {"status": "ok", "signals": ["none", "buy"], "bar_index": None, "detail": ""}


def candle(i: int, volume: int | None = 10) -> Candle:
    price = Decimal("350.25") + i
    return Candle(START + timedelta(minutes=i), price, price + 1, price - 1, price, volume)


def job() -> SandboxJob:
    return SandboxJob("code", {"window": 5}, [candle(0), candle(1, None)])


def run(handler: Callable[[httpx2.Request], httpx2.Response]) -> SandboxResult:
    async def scenario() -> SandboxResult:
        client = SandboxClient("http://executor:8100", TOKEN, httpx2.MockTransport(handler))
        try:
            return await client.run(job())
        finally:
            await client.aclose()

    return asyncio.run(scenario())


def test_sends_token_and_wire_bars() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=GOOD_BODY)

    result = run(handler)
    assert result == SandboxResult("ok", ["none", "buy"], None, "")
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == "http://executor:8100/run"
    assert request.headers["x-sandbox-token"] == TOKEN
    sent = json.loads(request.content)
    assert set(sent) == {"code", "params", "bars"}
    assert sent["code"] == "code"
    assert sent["params"] == {"window": 5}
    assert sent["bars"][0] == {
        "at": "2026-11-02T09:00:00+09:00",
        "open": "350.25",
        "high": "351.25",
        "low": "349.25",
        "close": "350.25",
        "volume": 10,
    }
    assert sent["bars"][1]["volume"] is None
    assert all(isinstance(v, str) for v in sent["bars"][0].values() if v != 10)


def test_failure_reply_is_returned_as_result() -> None:
    reply: dict[str, object] = {
        "status": "timeout",
        "signals": [],
        "bar_index": 1,
        "detail": "느림",
    }
    result = run(lambda _: httpx2.Response(200, json=reply))
    assert result == SandboxResult("timeout", [], 1, "느림")


def test_busy_and_down() -> None:
    def busy(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(503, json={"status": "busy"})

    with pytest.raises(SandboxBusy):
        run(busy)

    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    def slow(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow", request=request)

    cases: list[Callable[[httpx2.Request], httpx2.Response]] = [
        refuse,
        slow,
        lambda _: httpx2.Response(500),
        lambda _: httpx2.Response(401, json={"status": "unauthorized"}),
        lambda _: httpx2.Response(400, json={"status": "bad_request"}),
        lambda _: httpx2.Response(200, content=b"not json"),
        lambda _: httpx2.Response(200, json={"status": "ok"}),
        lambda _: httpx2.Response(200, json=[1]),
        lambda _: httpx2.Response(200, json={**GOOD_BODY, "signals": ["hold"]}),
        lambda _: httpx2.Response(200, json={**GOOD_BODY, "status": "weird"}),
    ]
    for failing in cases:
        with pytest.raises(SandboxDown):
            run(failing)


def inconsistent_bodies() -> list[dict[str, object]]:
    """모양은 맞지만 일(봉 2개)과 맞지 않는 응답."""
    failed: dict[str, object] = {
        "status": "exception",
        "signals": [],
        "bar_index": 1,
        "detail": "x",
    }
    return [
        {**GOOD_BODY, "signals": ["none"]},  # 신호가 봉보다 적다
        {**GOOD_BODY, "signals": ["none", "buy", "exit"]},  # 신호가 봉보다 많다
        {**GOOD_BODY, "bar_index": 0},  # 성공인데 bar_index가 있다
        {**GOOD_BODY, "detail": "x"},  # 성공인데 detail이 있다
        {**failed, "signals": ["none"]},  # 실패인데 신호가 있다
        {**failed, "bar_index": -1},
        {**failed, "bar_index": 2},  # 봉 2개면 0, 1만 가능하다
        {**failed, "bar_index": 10**9},
        {**failed, "detail": "x" * (MAX_DETAIL + 1)},
    ]


@pytest.mark.parametrize("body", inconsistent_bodies())
def test_inconsistent_reply_is_down(body: dict[str, object]) -> None:
    with pytest.raises(SandboxDown):
        run(lambda _: httpx2.Response(200, json=body))


def test_consistent_edge_replies_pass() -> None:
    last: dict[str, object] = {
        "status": "exception",
        "signals": [],
        "bar_index": 1,
        "detail": "x" * MAX_DETAIL,
    }
    assert run(lambda _: httpx2.Response(200, json=last)).bar_index == 1
    no_index: dict[str, object] = {
        "status": "output",
        "signals": [],
        "bar_index": None,
        "detail": "",
    }
    assert run(lambda _: httpx2.Response(200, json=no_index)).status == "output"


def test_token_is_not_in_errors_or_repr() -> None:
    client = SandboxClient("http://executor:8100", TOKEN)
    assert TOKEN not in repr(client)
    assert TOKEN not in str(client)

    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError(f"refused {request.headers['x-sandbox-token']}", request=request)

    def leak(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(500, text=TOKEN)

    for failing in (refuse, leak):
        with pytest.raises(SandboxDown) as caught:
            run(failing)
        assert TOKEN not in str(caught.value)
        assert TOKEN not in repr(caught.value)
        assert caught.value.__cause__ is None


def test_client_timeout_is_90_seconds() -> None:
    assert TIMEOUT_SECONDS == 90.0


def test_wire_roundtrip_helpers() -> None:
    wire = bar_to_wire(
        Candle(START.astimezone(), Decimal("1E+2"), Decimal("1.50"), Decimal("1"), Decimal("1"), 3)
    )
    assert wire["open"] == "100"
    assert wire["high"] == "1.50"
    assert wire["at"] == "2026-11-02T09:00:00+09:00"
    reply = Reply("exception", [], 4, "x")
    assert result_from_json(reply_to_json(reply)) == SandboxResult("exception", [], 4, "x")
    assert result_from_json({**GOOD_BODY, "bar_index": True}) is None
    assert result_from_json({**GOOD_BODY, "extra": 1}) is None


PROXY_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


async def _answer_once(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """요청 하나를 끝까지 읽고 성공 응답을 준다. 실행기 대신 서는 아주 작은 HTTP 서버다."""
    head = await reader.readuntil(b"\r\n\r\n")
    length = 0
    for line in head.decode().split("\r\n"):
        name, _, value = line.partition(":")
        if name.strip().lower() == "content-length":
            length = int(value)
    await reader.readexactly(length)
    body = json.dumps(GOOD_BODY).encode()
    writer.write(
        b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\nconnection: close\r\n"
        + f"content-length: {len(body)}\r\n\r\n".encode()
        + body
    )
    await writer.drain()
    writer.close()


def test_proxy_settings_in_the_environment_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """프록시 환경 변수가 있어도 토큰과 코드는 실행기로 바로 간다(프록시를 거치지 않는다)."""
    for name in PROXY_NAMES:
        # 닫힌 포트. 프록시를 따르면 연결이 거절돼 SandboxDown이 난다.
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)

    async def scenario() -> SandboxResult:
        server = await asyncio.start_server(_answer_once, "127.0.0.1", 0)
        port: int = server.sockets[0].getsockname()[1]
        client = SandboxClient(f"http://127.0.0.1:{port}", TOKEN)
        try:
            return await client.run(job())
        finally:
            await client.aclose()
            server.close()
            await server.wait_closed()

    assert asyncio.run(scenario()) == SandboxResult("ok", ["none", "buy"], None, "")


def test_client_does_not_load_the_docker_launcher_or_the_executor_app() -> None:
    """MCP 서버 프로세스는 docker를 띄우는 코드와 실행기 앱을 메모리에 올리지 않는다."""
    probe = (
        "import json, sys\n"
        "import nh_mcp.sandbox.client\n"
        "names = ('nh_mcp.sandbox.container', 'nh_mcp.sandbox.executor', 'starlette', 'uvicorn')\n"
        "print(json.dumps(sorted(n for n in names if n in sys.modules)))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True, timeout=60
    )
    assert json.loads(out.stdout) == []
