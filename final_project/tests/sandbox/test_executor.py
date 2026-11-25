"""실행기 HTTP API. 가짜 run으로 입구 검사를 보고, 마지막 하나만 실제 Docker로 잇는다."""

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from functools import partial

import httpx2
import pytest

from nh_mcp.domain import (
    MAX_MODEL_BYTES,
    BookEvent,
    MarketEvent,
    SandboxJob,
    SandboxResult,
    SymbolTerms,
    Target,
    TickEvent,
)
from nh_mcp.sandbox import executor
from nh_mcp.sandbox.container import ContainerStartError, run_in_container
from nh_mcp.sandbox.executor import MAX_BODY, MAX_CONCURRENT, MAX_EVENTS, MAX_JOB_BODY, make_app
from nh_mcp.sandbox.wire import MAX_MODEL_TEXT, TOKEN_HEADER, event_to_wire, job_to_json
from nh_mcp.services.backtest import MAX_CODE_BYTES, MAX_PARAMS_BYTES, MAX_SYMBOLS
from nh_mcp.strategies.examples import MEAN_REVERSION, MEAN_REVERSION_PARAMS

from .wire import SYMBOL, T0, TERMS, bars, job

TOKEN = "t" * 40
CODE = "def on_event(event, ctx):\n    return None\n"
OK = SandboxResult("ok", [None, {SYMBOL: Target(1)}], None, "")

type Body = dict[str, object]


class FakeRun:
    """받은 일을 기록하고 정해 둔 결과를 돌려준다. gate를 주면 풀릴 때까지 붙잡는다."""

    def __init__(self, reply: SandboxResult = OK, gate: asyncio.Event | None = None) -> None:
        self.reply = reply
        self.gate = gate
        self.calls: list[SandboxJob] = []

    async def __call__(self, work: SandboxJob) -> SandboxResult:
        self.calls.append(work)
        if self.gate is not None:
            await self.gate.wait()
        return self.reply


def body(**extra: object) -> Body:
    return {**job_to_json(job(CODE, bars(2))), **extra}


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


def assert_refused(payload: object) -> None:
    run = FakeRun()
    assert send(run, payload).status_code == 400
    assert run.calls == []


@pytest.mark.parametrize("token", [None, "", "wrong", "t" * 39, "t" * 41])
def test_missing_or_wrong_token_is_refused(token: str | None) -> None:
    run = FakeRun()
    assert send(run, body(), token).status_code == 401
    assert run.calls == []


def test_body_round_trips_into_the_same_job() -> None:
    run = FakeRun()
    work = job(CODE, bars(2), {"window": 5}, max_position=3)
    assert send(run, job_to_json(work)).status_code == 200
    assert run.calls == [work]


@pytest.mark.parametrize("field", ["image", "limits", "args", "model_path", "volumes", "bars"])
def test_unknown_field_is_refused(field: str) -> None:
    assert_refused(body(**{field: "x"}))


def test_model_round_trips_into_the_same_job() -> None:
    run = FakeRun()
    work = replace(job(CODE, bars(2)), model=b"\x00model\xff" * 1000)
    assert send(run, job_to_json(work)).status_code == 200
    assert run.calls == [work]
    assert run.calls[0].model == b"\x00model\xff" * 1000


def test_job_without_a_model_has_none() -> None:
    run = FakeRun()
    assert send(run, body(model=None)).status_code == 200
    assert run.calls[0].model is None


@pytest.mark.parametrize(
    "model",
    [
        "",  # 빈 파일
        "!!!!",  # base64 글자가 아님
        "QUJD\nRA==",  # 줄바꿈 섞임
        "가나다라",  # ASCII가 아님
        "QUJ",  # 길이가 맞지 않음
        12,
        True,
        ["QUJD"],
        base64.b64encode(bytes(MAX_MODEL_BYTES + 1)).decode(),  # 상한보다 1바이트 큼
    ],
    ids=["empty", "not-base64", "newline", "non-ascii", "padding", "int", "bool", "list", "big"],
)
def test_bad_model_is_refused(model: object) -> None:
    assert_refused(body(model=model))


@pytest.mark.parametrize(
    "missing", ["code", "params", "streams", "max_position", "terms", "events", "model"]
)
def test_missing_field_is_refused(missing: str) -> None:
    payload = body()
    del payload[missing]
    assert_refused(payload)


def bad_bar_cases() -> list[object]:
    bar = event_to_wire(bars(1)[0])
    tick: Body = {
        "type": "tick",
        "symbol": SYMBOL,
        "at": bar["at"],
        "price": "350",
        "qty": 1,
        "bid": None,
        "ask": None,
    }
    return [
        {**bar, "extra": "x"},
        {k: v for k, v in bar.items() if k != "low"},
        {**bar, "open": 350},
        {**bar, "close": 350.5},
        {**bar, "close": "3.5e2"},
        {**bar, "high": " 350"},
        {**bar, "low": "NaN"},
        {**bar, "at": 1},
        {**bar, "at": "2026-11-02T09:00:00"},  # 시간대 없음
        {**bar, "at": "2026-11-02T00:00:00+00:00"},  # 같은 시각이지만 KST가 아니다
        {**bar, "at": "2026-11-02T09:00:00.500000+09:00"},
        {**bar, "volume": "10"},
        {**bar, "volume": True},
        {**bar, "interval": "week"},
        {**bar, "interval": "day"},  # 선언하지 않은 흐름
        {**bar, "symbol": "B"},  # 선언하지 않은 종목
        {**bar, "symbol": 1},
        {**bar, "type": "fill"},  # 체결 이벤트는 실행기가 만든다
        tick,  # 선언하지 않은 흐름
        "not an event",
    ]


@pytest.mark.parametrize("event", bad_bar_cases())
def test_bad_bar_events_are_refused(event: object) -> None:
    assert_refused(body(events=[event]))


def test_events_must_be_a_list_of_at_most_max_events() -> None:
    assert_refused(body(events="not a list"))
    assert_refused(job_to_json(job(CODE, bars(MAX_EVENTS + 1))))


def test_volume_may_be_null_and_max_events_pass() -> None:
    run = FakeRun()
    payload = job_to_json(job(CODE, bars(MAX_EVENTS)))
    events = payload["events"]
    assert isinstance(events, list)
    first = events[0]
    assert isinstance(first, dict)
    events[0] = {**first, "volume": None}
    assert send(run, payload).status_code == 200
    [work] = run.calls
    assert len(work.events) == MAX_EVENTS
    assert [event_to_wire(e) for e in work.events] == events


def test_a_full_size_job_fits_the_body_cap() -> None:
    """한도까지 채운 일(이벤트 MAX_EVENTS개, 종목 5개, 코드·params 최대)이 본문 상한 안에 든다.

    가장 큰 이벤트인 10단계 호가로 채운다(코드 8자, 가격 784.55, 잔량 네 자리). 모의 서버의 실제
    호가는 5단계다. 클라이언트는 공백 없이 보내지만 여기서는 공백을 넣어 더 크게 잰다.
    """
    symbols = [f"A01{n}C000" for n in range(MAX_SYMBOLS)]
    price = Decimal("784.55")
    levels = tuple((price - Decimal(k) / 20, 1234) for k in range(10))
    events: list[MarketEvent] = [
        BookEvent(symbols[i % MAX_SYMBOLS], T0 + timedelta(seconds=i), levels, levels)
        for i in range(MAX_EVENTS)
    ]
    work = SandboxJob(
        code="x = 1\n" * (MAX_CODE_BYTES // 6),
        params={"p": "a" * (MAX_PARAMS_BYTES - 16)},
        streams={s: frozenset({"tick", "book"}) for s in symbols},
        max_position=100,
        terms={
            s: SymbolTerms(Decimal("0.05"), Decimal("250000"), Decimal("1500")) for s in symbols
        },
        events=events,
    )
    payload = json.dumps(job_to_json(work)).encode()
    run = FakeRun()

    assert len(payload) < MAX_JOB_BODY
    assert asyncio.run(post(run, payload)).status_code == 200
    assert len(run.calls[0].events) == MAX_EVENTS

    # 같은 일에 가장 큰 모델 파일을 더해도 본문 상한 안에 든다.
    with_model = json.dumps(job_to_json(replace(work, model=bytes(MAX_MODEL_BYTES)))).encode()
    assert len(with_model) < MAX_BODY
    assert asyncio.run(post(run, with_model)).status_code == 200
    assert run.calls[1].model == bytes(MAX_MODEL_BYTES)


def test_the_raised_cap_is_only_room_for_the_model() -> None:
    """모델 파일 없이 예전 상한을 넘는 본문은 받지 않는다(늘린 상한은 모델 자리다)."""
    run = FakeRun()
    padded = body(code="x" * MAX_JOB_BODY)
    assert asyncio.run(post(run, json.dumps(padded).encode())).status_code == 413
    # 모델이 있어도 모델을 뺀 부분이 상한을 넘으면 같다.
    with_model = body(code="x" * MAX_JOB_BODY, model=base64.b64encode(b"m" * 3000).decode())
    assert asyncio.run(post(run, json.dumps(with_model).encode())).status_code == 413
    assert run.calls == []


def test_body_cap_is_the_job_cap_plus_the_largest_model_in_base64() -> None:
    assert MAX_MODEL_TEXT == len(base64.b64encode(bytes(MAX_MODEL_BYTES))) == 69_905_068
    assert MAX_BODY == MAX_JOB_BODY + MAX_MODEL_TEXT == 120_236_716


def tick_book_job() -> SandboxJob:
    price = Decimal("350.05")
    tick = TickEvent(SYMBOL, T0, price, 2, None, Decimal("350.10"))
    levels = tuple((price - Decimal(i) / 20, 3 + i) for i in range(10))
    return SandboxJob(
        code=CODE,
        params={},
        streams={SYMBOL: frozenset({"tick", "book"})},
        max_position=3,
        terms=TERMS,
        events=[tick, BookEvent(SYMBOL, T0, levels, ())],
    )


def test_tick_and_book_events_pass_unchanged() -> None:
    run = FakeRun()
    work = tick_book_job()
    assert send(run, job_to_json(work)).status_code == 200
    assert run.calls == [work]


def bad_tick_book_cases() -> list[object]:
    tick, book = (event_to_wire(e) for e in tick_book_job().events)
    return [
        {**tick, "qty": True},
        {**tick, "bid": 350},
        {**tick, "price": None},
        {**book, "bids": [["350", 1]] * 11},
        {**book, "bids": [["350"]]},
        {**book, "bids": [["350", 1, 2]]},
        {**book, "asks": [["350", "1"]]},
        {**book, "asks": "350"},
    ]


@pytest.mark.parametrize("event", bad_tick_book_cases())
def test_bad_tick_and_book_events_are_refused(event: object) -> None:
    assert_refused({**job_to_json(tick_book_job()), "events": [event]})


TERMS_WIRE: Body = {"tick": "0.05", "multiplier": "250000", "fee_per_fill": "0"}


@pytest.mark.parametrize(
    "change",
    [
        {"code": 1},
        {"params": []},
        {"params": "x"},
        {"max_position": True},
        {"max_position": -1},
        {"max_position": "1"},
        {"streams": {}},
        {"streams": []},
        {"streams": {SYMBOL: []}},
        {"streams": {SYMBOL: ["minute", "minute"]}},
        {"streams": {SYMBOL: ["week"]}},
        {"streams": {SYMBOL: "minute"}},
        {"streams": {SYMBOL: ["minute"], "B": ["minute"]}},  # B의 체결 조건이 없다
        {"terms": {}},
        {"terms": {SYMBOL: TERMS_WIRE, "B": TERMS_WIRE}},  # 흐름이 없는 종목
        {"terms": {SYMBOL: {**TERMS_WIRE, "extra": "1"}}},
        {"terms": {SYMBOL: {**TERMS_WIRE, "tick": "0"}}},
        {"terms": {SYMBOL: {**TERMS_WIRE, "tick": 0.05}}},
        {"terms": {SYMBOL: {**TERMS_WIRE, "multiplier": "-1"}}},
        {"terms": {SYMBOL: {**TERMS_WIRE, "fee_per_fill": "-1"}}},
    ],
)
def test_bad_job_fields_are_refused(change: Body) -> None:
    assert_refused(body(**change))


@pytest.mark.parametrize("payload", [[1, 2], "text", None])
def test_bad_shape_is_refused(payload: object) -> None:
    assert_refused(payload)


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
        async def __call__(self, work: SandboxJob) -> SandboxResult:
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
        async def __call__(self, work: SandboxJob) -> SandboxResult:
            raise ContainerStartError("전략 컨테이너 상태를 읽지 못했습니다")

    resp = send(NoImage(), body())
    assert resp.status_code == 500
    assert resp.json() == {"status": "error"}


def test_reply_is_passed_through() -> None:
    failed = FakeRun(SandboxResult("exception", [], 1, "ZeroDivisionError: x"))
    resp = send(failed, body())
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "exception",
        "outputs": [],
        "index": 1,
        "detail": "ZeroDivisionError: x",
    }
    limit = {SYMBOL: Target(-1, Decimal("350.05"))}
    ok = FakeRun(SandboxResult("ok", [None, limit, {SYMBOL: Target(0)}], None, ""))
    assert send(ok, body()).json() == {
        "status": "ok",
        "outputs": [
            None,
            {SYMBOL: {"position": -1, "price": "350.05"}},
            {SYMBOL: {"position": 0, "price": None}},
        ],
        "index": None,
        "detail": "",
    }


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
    payload = job_to_json(job(MEAN_REVERSION, bars(30), MEAN_REVERSION_PARAMS))
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
    assert len(out["outputs"]) == 30
