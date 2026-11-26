"""실행기 클라이언트. httpx2 MockTransport로 응답을 만들어 준다."""

import asyncio
import json
import subprocess
import sys
from collections.abc import Callable, Coroutine
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

import httpx2
import pytest

from nh_mcp.domain import (
    KST,
    BarEvent,
    Candle,
    FillEvent,
    JsonValue,
    Output,
    SandboxJob,
    SandboxResult,
    SessionFailed,
    SessionJob,
    SymbolTerms,
    Target,
)
from nh_mcp.sandbox.client import (
    TIMEOUT_SECONDS,
    SandboxBusy,
    SandboxClient,
    SandboxDown,
    SessionGone,
    SessionHandle,
)
from nh_mcp.sandbox.wire import (
    MAX_DETAIL,
    event_to_wire,
    result_from_json,
    result_to_json,
    session_to_json,
)

TOKEN = "secret-token-" + "z" * 30
SYMBOL = "A016C000"
START = datetime(2026, 11, 2, 9, 0, tzinfo=KST)
# 봉 2개에 체결 이벤트 하나가 끼어 출력이 셋이다.
GOOD_BODY: dict[str, JsonValue] = {
    "status": "ok",
    "outputs": [{SYMBOL: {"position": 2, "price": None}}, None, None],
    "index": None,
    "detail": "",
}
GOOD = SandboxResult("ok", [{SYMBOL: Target(2)}, None, None], None, "")
FAILED_BODY: dict[str, JsonValue] = {
    "status": "exception",
    "outputs": [],
    "index": 1,
    "detail": "x",
}


def candle(i: int, volume: int | None = 10) -> Candle:
    price = Decimal("350.25") + i
    return Candle(START + timedelta(minutes=i), price, price + 1, price - 1, price, volume)


def job() -> SandboxJob:
    return SandboxJob(
        code="code",
        params={"window": 5},
        streams={SYMBOL: frozenset({"minute"})},
        max_position=2,
        terms={SYMBOL: SymbolTerms(Decimal("0.05"), Decimal("250000"), Decimal("1500"))},
        events=[BarEvent(SYMBOL, "minute", candle(0)), BarEvent(SYMBOL, "minute", candle(1, None))],
    )


def run(handler: Callable[[httpx2.Request], httpx2.Response]) -> SandboxResult:
    async def scenario() -> SandboxResult:
        client = SandboxClient("http://executor:8100", TOKEN, httpx2.MockTransport(handler))
        try:
            return await client.run(job())
        finally:
            await client.aclose()

    return asyncio.run(scenario())


def answer(body: dict[str, JsonValue]) -> Callable[[httpx2.Request], httpx2.Response]:
    return lambda _: httpx2.Response(200, json=body)


def test_sends_token_and_wire_job() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=GOOD_BODY)

    assert run(handler) == GOOD
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == "http://executor:8100/run"
    assert request.headers["x-sandbox-token"] == TOKEN
    assert json.loads(request.content) == {
        "code": "code",
        "params": {"window": 5},
        "streams": {SYMBOL: ["minute"]},
        "max_position": 2,
        "terms": {SYMBOL: {"tick": "0.05", "multiplier": "250000", "fee_per_fill": "1500"}},
        "events": [
            {
                "type": "bar",
                "symbol": SYMBOL,
                "interval": "minute",
                "at": "2026-11-02T09:00:00+09:00",
                "open": "350.25",
                "high": "351.25",
                "low": "349.25",
                "close": "350.25",
                "volume": 10,
            },
            {
                "type": "bar",
                "symbol": SYMBOL,
                "interval": "minute",
                "at": "2026-11-02T09:01:00+09:00",
                "open": "351.25",
                "high": "352.25",
                "low": "350.25",
                "close": "351.25",
                "volume": None,
            },
        ],
        "model": None,
    }


def test_limit_targets_are_read_as_decimals() -> None:
    body: dict[str, JsonValue] = {
        **GOOD_BODY,
        "outputs": [{SYMBOL: {"position": -2, "price": "350.05"}}, {SYMBOL: {"position": 0}}],
    }
    result = run(answer(body))
    assert result.outputs == [{SYMBOL: Target(-2, Decimal("350.05"))}, {SYMBOL: Target(0)}]


def test_failure_reply_is_returned_as_result() -> None:
    body: dict[str, JsonValue] = {"status": "timeout", "outputs": [], "index": 1, "detail": "느림"}
    assert run(answer(body)) == SandboxResult("timeout", [], 1, "느림")


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
        answer({**GOOD_BODY, "status": "weird"}),
        answer({**GOOD_BODY, "status": "invalid_signal"}),
        answer({**GOOD_BODY, "extra": 1}),
    ]
    for failing in cases:
        with pytest.raises(SandboxDown):
            run(failing)


def bad_output_bodies() -> list[dict[str, JsonValue]]:
    """출력이 이 일의 계약 밖이다. 실행기도 믿지 않고 다시 본다."""
    bad: list[JsonValue] = [
        "buy",
        ["buy"],
        {"B": {"position": 1, "price": None}},  # 선언하지 않은 종목
        {SYMBOL: {"position": 3, "price": None}},  # max_position 2를 넘는다
        {SYMBOL: {"position": True, "price": None}},
        {SYMBOL: {"position": 1, "price": "350.03"}},  # 틱(0.05)의 배수가 아니다
        {SYMBOL: {"position": 1, "price": 350}},
        {SYMBOL: {"position": 1, "price": "1e3"}},
        {SYMBOL: {"position": 1, "extra": None}},
    ]
    return [{**GOOD_BODY, "outputs": [output, None]} for output in bad]


def inconsistent_bodies() -> list[dict[str, JsonValue]]:
    """모양은 맞지만 일(봉 2개)과 맞지 않는 응답."""
    return [
        {**GOOD_BODY, "outputs": [None]},  # 출력이 시장 이벤트보다 적다
        {**GOOD_BODY, "index": 0},  # 성공인데 index가 있다
        {**GOOD_BODY, "detail": "x"},  # 성공인데 detail이 있다
        {**FAILED_BODY, "outputs": [None]},  # 실패인데 출력이 있다
        {**FAILED_BODY, "index": -1},
        {**FAILED_BODY, "index": 2},  # 봉 2개면 0, 1만 가능하다
        {**FAILED_BODY, "index": 10**9},
        {**FAILED_BODY, "index": True},
        {**FAILED_BODY, "detail": "x" * (MAX_DETAIL + 1)},
    ]


@pytest.mark.parametrize("body", bad_output_bodies() + inconsistent_bodies())
def test_inconsistent_reply_is_down(body: dict[str, JsonValue]) -> None:
    with pytest.raises(SandboxDown):
        run(answer(body))


def test_consistent_edge_replies_pass() -> None:
    assert run(answer({**FAILED_BODY, "detail": "x" * MAX_DETAIL})).index == 1
    no_index: dict[str, JsonValue] = {
        "status": "output",
        "outputs": [],
        "index": None,
        "detail": "",
    }
    assert run(answer(no_index)).status == "output"
    # 체결 이벤트가 없으면 출력은 봉 수와 같다.
    assert run(answer({**GOOD_BODY, "outputs": [None, None]})).outputs == [None, None]


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
    price = Decimal("1E+2")
    bar = Candle(START.astimezone(), price, Decimal("1.50"), Decimal("1"), Decimal("1"), 3)
    wire = event_to_wire(BarEvent(SYMBOL, "day", bar))
    assert (wire["open"], wire["high"], wire["interval"]) == ("100", "1.50", "day")
    assert wire["at"] == "2026-11-02T09:00:00+09:00"
    fill = FillEvent(SYMBOL, START, "sell", 2, Decimal("350.05"))
    assert event_to_wire(fill) == {
        "type": "fill",
        "symbol": SYMBOL,
        "at": "2026-11-02T09:00:00+09:00",
        "side": "sell",
        "qty": 2,
        "price": "350.05",
    }
    failed = SandboxResult("exception", [], 1, "x")
    assert result_from_json(result_to_json(failed), job()) == failed
    assert result_from_json(result_to_json(GOOD), job()) == GOOD


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

    assert asyncio.run(scenario()) == GOOD


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


SESSION_ID = "A" * 20 + "b_-" + "9" * 20
SESSION = SessionJob(
    code="code",
    params={"window": 5},
    streams={SYMBOL: frozenset({"minute"})},
    max_position=2,
    terms={SYMBOL: SymbolTerms(Decimal("0.05"), Decimal("250000"), Decimal("1500"))},
    expires_at=START + timedelta(days=7),
    model=b"\x00model",
)
HANDLE = SessionHandle(SESSION_ID, replace(SESSION, model=None))
BAR = BarEvent(SYMBOL, "minute", candle(0))

type Handler = Callable[[httpx2.Request], httpx2.Response]


def with_client[T](
    handler: Handler, use: Callable[[SandboxClient], Coroutine[object, object, T]]
) -> T:
    async def scenario() -> T:
        client = SandboxClient("http://executor:8100", TOKEN, httpx2.MockTransport(handler))
        try:
            return await use(client)
        finally:
            await client.aclose()

    return asyncio.run(scenario())


def open_with(handler: Handler) -> SessionHandle | SessionFailed:
    return with_client(handler, lambda client: client.open_session(SESSION))


def feed_with(handler: Handler) -> Output | SessionFailed:
    return with_client(handler, lambda client: client.feed(HANDLE, BAR))


def test_open_session_sends_the_wire_body_and_returns_a_handle() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"status": "ok", "session_id": SESSION_ID})

    handle = open_with(handler)
    assert handle == HANDLE
    [request] = seen
    assert (request.method, str(request.url)) == ("POST", "http://executor:8100/sessions")
    assert request.headers["x-sandbox-token"] == TOKEN
    assert json.loads(request.content) == session_to_json(SESSION)
    assert json.loads(request.content)["expires_at"] == "2026-11-09T09:00:00+09:00"
    # 식별자는 repr에 나오지 않는다. 모델 파일도 들고 있지 않는다.
    assert SESSION_ID not in repr(handle)
    assert isinstance(handle, SessionHandle)
    assert handle.job.model is None


def test_open_session_failure_is_returned() -> None:
    body: dict[str, JsonValue] = {"status": "invalid_code", "detail": "on_event 함수가 없습니다"}
    assert open_with(answer(body)) == SessionFailed("invalid_code", "on_event 함수가 없습니다")


@pytest.mark.parametrize(
    "body",
    [
        {"status": "ok", "session_id": "short"},
        {"status": "ok", "session_id": "a" * 42 + "/"},
        {"status": "ok", "session_id": "a" * 44},
        {"status": "ok", "session_id": 1},
        {"status": "ok", "session_id": SESSION_ID, "extra": 1},
        {"status": "ok"},
        {"status": "weird", "detail": ""},
        {"status": "exception"},
        {"status": "exception", "detail": 1},
        {"status": "exception", "detail": "x" * (MAX_DETAIL + 1)},
        {"status": "exception", "detail": "", "extra": 1},
        {"session_id": SESSION_ID},
    ],
)
def test_open_session_bad_reply_is_down(body: dict[str, JsonValue]) -> None:
    with pytest.raises(SandboxDown):
        open_with(answer(body))


def test_open_session_busy_and_down() -> None:
    with pytest.raises(SandboxBusy):
        open_with(lambda _: httpx2.Response(503, json={"status": "busy"}))
    for code in (400, 401, 413, 500):
        with pytest.raises(SandboxDown):
            open_with(lambda _, code=code: httpx2.Response(code, json={"status": "x"}))
    with pytest.raises(SandboxDown):
        open_with(lambda _: httpx2.Response(200, content=b"not json"))


def test_feed_sends_the_event_and_reads_the_output() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(
            200, json={"status": "ok", "output": {SYMBOL: {"position": -2, "price": "350.05"}}}
        )

    assert feed_with(handler) == {SYMBOL: Target(-2, Decimal("350.05"))}
    [request] = seen
    assert request.method == "POST"
    assert str(request.url) == f"http://executor:8100/sessions/{SESSION_ID}/events"
    assert request.headers["x-sandbox-token"] == TOKEN
    assert json.loads(request.content) == event_to_wire(BAR)


def test_feed_returns_no_change_and_failures() -> None:
    assert feed_with(answer({"status": "ok", "output": None})) is None
    body: dict[str, JsonValue] = {"status": "memory", "detail": "메모리 한도(512MB)를 넘었습니다"}
    assert feed_with(answer(body)) == SessionFailed("memory", "메모리 한도(512MB)를 넘었습니다")


def test_feed_on_a_gone_session_is_session_gone() -> None:
    with pytest.raises(SessionGone):
        feed_with(lambda _: httpx2.Response(404, json={"status": "gone"}))


@pytest.mark.parametrize(
    "output",
    [
        {"B": {"position": 1, "price": None}},  # 선언하지 않은 종목
        {SYMBOL: {"position": 3, "price": None}},  # max_position 2를 넘는다
        {SYMBOL: {"position": 1, "price": "350.03"}},  # 틱의 배수가 아니다
        {SYMBOL: {"position": 1.0}},
        "buy",
    ],
)
def test_feed_output_outside_the_contract_is_down(output: JsonValue) -> None:
    with pytest.raises(SandboxDown):
        feed_with(answer({"status": "ok", "output": output}))


@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(409, json={"status": "busy"}),
        httpx2.Response(500, json={"status": "error"}),
        httpx2.Response(200, json={"status": "ok"}),
        httpx2.Response(200, json={"status": "ok", "output": None, "extra": 1}),
        httpx2.Response(200, json={"status": "ok", "outputs": [None]}),
        httpx2.Response(200, json=[None]),
    ],
)
def test_feed_bad_reply_is_down(response: httpx2.Response) -> None:
    with pytest.raises(SandboxDown):
        feed_with(lambda _: response)


def test_close_deletes_and_treats_gone_as_closed() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        code = 200 if len(seen) == 1 else 404
        return httpx2.Response(code, json={"status": "closed" if code == 200 else "gone"})

    async def close_twice(client: SandboxClient) -> None:
        await client.close(HANDLE)
        await client.close(HANDLE)

    with_client(handler, close_twice)
    assert [(r.method, r.url.path) for r in seen] == [("DELETE", f"/sessions/{SESSION_ID}")] * 2
    assert all(r.headers["x-sandbox-token"] == TOKEN for r in seen)
    with pytest.raises(SandboxDown):
        with_client(lambda _: httpx2.Response(500), lambda client: client.close(HANDLE))


def test_token_is_not_in_session_errors() -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError(f"refused {request.headers['x-sandbox-token']}", request=request)

    calls: list[Callable[[SandboxClient], Coroutine[object, object, object]]] = [
        lambda client: client.open_session(SESSION),
        lambda client: client.feed(HANDLE, BAR),
        lambda client: client.close(HANDLE),
    ]
    for call in calls:
        with pytest.raises(SandboxDown) as caught:
            with_client(refuse, call)
        assert TOKEN not in str(caught.value)
        assert caught.value.__cause__ is None
