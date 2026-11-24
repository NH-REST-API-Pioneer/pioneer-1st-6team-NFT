"""실행기 HTTP API. 가짜 run으로 입구 검사를 보고, 마지막 하나만 실제 Docker로 잇는다."""

import asyncio
import json
from collections.abc import AsyncIterator
from functools import partial

import httpx2
import pytest

from nh_mcp.domain import JsonValue
from nh_mcp.sandbox import executor
from nh_mcp.sandbox.container import ContainerStartError, run_in_container
from nh_mcp.sandbox.executor import MAX_BODY, MAX_CONCURRENT, make_app
from nh_mcp.sandbox.wire import TOKEN_HEADER, Reply, WireBar
from nh_mcp.strategies.examples import MEAN_REVERSION, MEAN_REVERSION_PARAMS

from .wire import bars

TOKEN = "t" * 40
GOOD: dict[str, object] = {
    "code": "def on_bar(bar, history, params):\n    return 'none'\n",
    "params": {},
}
OK = Reply("ok", ["none", "buy"], None, "")


class FakeRun:
    """받은 인자를 기록하고 정해 둔 Reply를 돌려준다. gate를 주면 풀릴 때까지 붙잡는다."""

    def __init__(self, reply: Reply = OK, gate: asyncio.Event | None = None) -> None:
        self.reply = reply
        self.gate = gate
        self.calls: list[tuple[str, dict[str, JsonValue], list[WireBar]]] = []

    async def __call__(self, code: str, params: dict[str, JsonValue], wire: list[WireBar]) -> Reply:
        self.calls.append((code, params, wire))
        if self.gate is not None:
            await self.gate.wait()
        return self.reply


def body(**extra: object) -> dict[str, object]:
    return {**GOOD, "bars": bars(2), **extra}


async def post(
    run: executor.RunFn,
    content: bytes | AsyncIterator[bytes],
    token: str | None = TOKEN,
) -> httpx2.Response:
    headers = {} if token is None else {TOKEN_HEADER: token}
    transport = httpx2.ASGITransport(app=make_app(TOKEN, run))
    async with httpx2.AsyncClient(transport=transport, base_url="http://executor") as http:
        return await http.post("/run", content=content, headers=headers)


def send(run: executor.RunFn, payload: object, token: str | None = TOKEN) -> httpx2.Response:
    return asyncio.run(post(run, json.dumps(payload).encode(), token))


@pytest.mark.parametrize("token", [None, "", "wrong", "t" * 39, "t" * 41])
def test_missing_or_wrong_token_is_refused(token: str | None) -> None:
    run = FakeRun()
    assert send(run, body(), token).status_code == 401
    assert run.calls == []


@pytest.mark.parametrize("field", ["image", "limits", "args"])
def test_unknown_field_is_refused(field: str) -> None:
    run = FakeRun()
    assert send(run, body(**{field: "x"})).status_code == 400
    assert run.calls == []


@pytest.mark.parametrize("missing", ["code", "params", "bars"])
def test_missing_field_is_refused(missing: str) -> None:
    run = FakeRun()
    payload = body()
    del payload[missing]
    assert send(run, payload).status_code == 400
    assert run.calls == []


def bad_bar_cases() -> list[object]:
    good = bars(1)[0]
    return [
        bars(2001),
        [{**good, "extra": "x"}],
        [{k: v for k, v in good.items() if k != "low"}],
        [{**good, "open": 350}],
        [{**good, "close": 350.5}],
        [{**good, "at": 1}],
        [{**good, "volume": "10"}],
        [{**good, "volume": True}],
        ["not a bar"],
        "not a list",
    ]


@pytest.mark.parametrize("wire", bad_bar_cases())
def test_bad_bars_are_refused(wire: object) -> None:
    run = FakeRun()
    assert send(run, {**GOOD, "bars": wire}).status_code == 400
    assert run.calls == []


def test_volume_may_be_null_and_2000_bars_pass() -> None:
    run = FakeRun()
    wire = bars(2000)
    wire[0] = {**wire[0], "volume": None}
    assert send(run, {**GOOD, "bars": wire}).status_code == 200
    assert run.calls[0][2] == wire


@pytest.mark.parametrize(
    "payload",
    [
        {**GOOD, "code": 1, "bars": []},
        {**GOOD, "params": [], "bars": []},
        {**GOOD, "params": "x", "bars": []},
        [1, 2],
        "text",
    ],
)
def test_bad_code_params_or_shape_is_refused(payload: object) -> None:
    run = FakeRun()
    assert send(run, payload).status_code == 400
    assert run.calls == []


def test_unparseable_body_is_refused() -> None:
    run = FakeRun()
    assert asyncio.run(post(run, b"{not json")).status_code == 400
    assert run.calls == []


def test_oversized_body_is_refused() -> None:
    run = FakeRun()
    too_big = b"x" * (MAX_BODY + 1)
    assert asyncio.run(post(run, too_big)).status_code == 413
    assert run.calls == []


def test_oversized_body_without_content_length_is_refused() -> None:
    run = FakeRun()

    async def chunks() -> AsyncIterator[bytes]:
        # content-length 없이 조금씩 보낸다. 읽은 양으로 막아야 한다.
        for _ in range(MAX_BODY // 65536 + 2):
            yield b"x" * 65536

    assert asyncio.run(post(run, chunks())).status_code == 413
    assert run.calls == []


def test_fourth_concurrent_run_is_busy() -> None:
    async def scenario() -> None:
        gate = asyncio.Event()
        run = FakeRun(gate=gate)
        payload = json.dumps(body()).encode()
        transport = httpx2.ASGITransport(app=make_app(TOKEN, run))
        async with httpx2.AsyncClient(transport=transport, base_url="http://executor") as http:

            async def call() -> httpx2.Response:
                return await http.post("/run", content=payload, headers={TOKEN_HEADER: TOKEN})

            held = [asyncio.create_task(call()) for _ in range(MAX_CONCURRENT)]
            while len(run.calls) < MAX_CONCURRENT:
                await asyncio.sleep(0)
            busy = await call()
            assert busy.status_code == 503
            assert busy.json() == {"status": "busy"}
            gate.set()
            assert [r.status_code for r in await asyncio.gather(*held)] == [200] * MAX_CONCURRENT
            assert (await call()).status_code == 200

    asyncio.run(scenario())


def test_slot_is_released_when_run_fails() -> None:
    class Boom(FakeRun):
        async def __call__(
            self, code: str, params: dict[str, JsonValue], wire: list[WireBar]
        ) -> Reply:
            raise RuntimeError("docker 없음")

    async def scenario() -> None:
        payload = json.dumps(body()).encode()
        transport = httpx2.ASGITransport(app=make_app(TOKEN, Boom()))
        async with httpx2.AsyncClient(transport=transport, base_url="http://executor") as http:
            for _ in range(MAX_CONCURRENT + 2):
                resp = await http.post("/run", content=payload, headers={TOKEN_HEADER: TOKEN})
                assert resp.status_code == 500
                assert "docker" not in resp.text

    asyncio.run(scenario())


def test_start_error_is_a_500_not_a_strategy_reply() -> None:
    """컨테이너를 못 띄우면 전략 사유로 답하지 않는다. MCP 서버는 500을 실행기 장애로 본다."""

    class NoImage(FakeRun):
        async def __call__(
            self, code: str, params: dict[str, JsonValue], wire: list[WireBar]
        ) -> Reply:
            raise ContainerStartError("전략 컨테이너 상태를 읽지 못했습니다")

    resp = send(NoImage(), body())
    assert resp.status_code == 500
    assert resp.json() == {"status": "error"}


def test_reply_is_passed_through() -> None:
    reply = Reply("exception", [], 1, "ZeroDivisionError: x")
    run = FakeRun(reply)
    resp = send(run, body(params={"window": 5}))
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "exception",
        "signals": [],
        "bar_index": 1,
        "detail": "ZeroDivisionError: x",
    }
    code, params, wire = run.calls[0]
    assert code == GOOD["code"]
    assert params == {"window": 5}
    assert wire == bars(2)


def test_only_post_run_exists() -> None:
    async def scenario() -> list[int]:
        transport = httpx2.ASGITransport(app=make_app(TOKEN, FakeRun()))
        async with httpx2.AsyncClient(transport=transport, base_url="http://executor") as http:
            return [
                (await http.get("/run")).status_code,
                (await http.post("/other", content=b"{}")).status_code,
            ]

    assert asyncio.run(scenario()) == [405, 404]


def test_main_refuses_a_short_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANDBOX_TOKEN", "short")
    monkeypatch.setenv("SANDBOX_IMAGE", "img")
    with pytest.raises(SystemExit) as stop:
        executor.main()
    assert "SANDBOX_TOKEN" in str(stop.value)
    assert "short" not in str(stop.value).replace("SANDBOX_TOKEN", "")


def test_main_refuses_missing_image(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANDBOX_TOKEN", TOKEN)
    monkeypatch.delenv("SANDBOX_IMAGE", raising=False)
    with pytest.raises(SystemExit) as stop:
        executor.main()
    assert "SANDBOX_IMAGE" in str(stop.value)


def test_main_removes_leftover_containers_before_serving(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []

    async def sweep() -> int:
        order.append("sweep")
        return 0

    def serve(*_: object, **__: object) -> None:
        order.append("serve")

    monkeypatch.setenv("SANDBOX_TOKEN", TOKEN)
    monkeypatch.setenv("SANDBOX_IMAGE", "img")
    monkeypatch.setattr(executor, "remove_leftovers", sweep)
    monkeypatch.setattr(executor.uvicorn, "run", serve)
    executor.main()
    assert order == ["sweep", "serve"]


@pytest.mark.docker
def test_end_to_end_with_real_docker(sandbox_image: str) -> None:
    run = partial(run_in_container, image=sandbox_image)
    payload = {"code": MEAN_REVERSION, "params": MEAN_REVERSION_PARAMS, "bars": bars(30)}
    transport = httpx2.ASGITransport(app=make_app(TOKEN, run))

    async def scenario() -> httpx2.Response:
        async with httpx2.AsyncClient(transport=transport, base_url="http://executor") as http:
            return await http.post(
                "/run", content=json.dumps(payload).encode(), headers={TOKEN_HEADER: TOKEN}
            )

    resp = asyncio.run(scenario())
    assert resp.status_code == 200
    out = resp.json()
    assert out["status"] == "ok"
    assert len(out["signals"]) == 30
