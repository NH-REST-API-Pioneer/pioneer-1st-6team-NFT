"""실제 컨테이너(실행기 앱 + 띄워 둔 컨테이너)로 도는 실행기.

같은 코드·같은 봉·같은 체결이면 실행기가 세션에 넣은 이벤트와 받은 출력이 백테스트와 줄마다 같다.
가짜 NH는 백테스트의 체결 가정(시장가는 그 분 봉 시가 ± 1틱, 지정가는 넘어선 봉에서 지정가)으로
체결하고, 봉은 마감 2~15초 뒤에 REST에 올라온다.
"""

import asyncio
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from functools import partial
from importlib import import_module
from pathlib import Path

import httpx2
import pytest

from nh_mcp.domain import (
    BarEvent,
    Candle,
    Clock,
    FillEvent,
    JsonValue,
    MarketEvent,
    Output,
    SandboxJob,
    SessionFailed,
    SessionJob,
    StrategyEvent,
    SymbolTerms,
)
from nh_mcp.sandbox.client import SandboxClient, SessionHandle
from nh_mcp.sandbox.container import ResidentSession, run_in_container
from nh_mcp.sandbox.executor import make_app
from nh_mcp.strategies.backtest import Simulator, decision_time
from nh_mcp.strategies.examples import MEAN_REVERSION, MEAN_REVERSION_PARAMS, SCALE_IN_LIMIT

from .rig import SYMBOL, T0, Rig, approval, candle, held, scope
from .scripts import MINUTE

pytestmark = pytest.mark.docker
sandbox_image = import_module("sandbox.conftest").sandbox_image
leftovers = import_module("sandbox.resident").leftovers

TOKEN = "r" * 40
TERMS = {SYMBOL: SymbolTerms(Decimal("0.05"), Decimal("250000"), Decimal(0))}
DELAYS = [2, 15, 7, 11, 3, 14, 5, 9]


@pytest.fixture(autouse=True)
def no_strategy_container_left_behind() -> Iterator[None]:
    yield
    assert leftovers() == ""


class Recorded:
    """실제 실행기 클라이언트를 감싸 넣은 이벤트와 받은 출력을 적는다. inflight는 도는 호출 수."""

    def __init__(self) -> None:
        self.inner: SandboxClient | None = None
        self.fed: list[tuple[StrategyEvent, Output]] = []
        self.inflight = 0

    def _client(self) -> SandboxClient:
        assert self.inner is not None
        return self.inner

    async def open_session(self, job: SessionJob) -> SessionHandle | SessionFailed:
        self.inflight += 1
        try:
            return await self._client().open_session(job)
        finally:
            self.inflight -= 1

    async def feed(self, handle: SessionHandle, event: StrategyEvent) -> Output | SessionFailed:
        self.inflight += 1
        try:
            output = await self._client().feed(handle, event)
        finally:
            self.inflight -= 1
        if not isinstance(output, SessionFailed):
            self.fed.append((event, output))
        return output

    async def close(self, handle: SessionHandle) -> None:
        self.inflight += 1
        try:
            await self._client().close(handle)
        finally:
            self.inflight -= 1


@asynccontextmanager
async def executor(image: str, clock: Clock) -> AsyncGenerator[SandboxClient]:
    """실제 실행기 앱(수명 주기 포함). 세션 만료는 테스트의 멈춰 둔 시계로 잰다."""
    app = make_app(
        TOKEN,
        partial(run_in_container, image=image),
        partial(ResidentSession.open, image=image),
        clock=clock,
    )
    client = SandboxClient("http://executor", TOKEN, httpx2.ASGITransport(app=app))
    async with app.router.lifespan_context(app):
        try:
            yield client
        finally:
            await client.aclose()


def bars_of(candles: list[Candle]) -> list[MarketEvent]:
    return [BarEvent(SYMBOL, "minute", c) for c in candles]


@dataclass(frozen=True)
class Case:
    """같은 코드·파라미터·봉으로 백테스트와 실행기를 돌린다. cap은 최대 포지션."""

    code: str
    params: dict[str, JsonValue]
    candles: list[Candle]
    cap: int
    fills: list[tuple[str, int, Decimal]]


def backtested(image: str, case: Case) -> list[tuple[StrategyEvent, Output]]:
    """백테스트 한 번: 넣은 이벤트(체결 계산이 만든 체결 포함)와 줄마다의 출력."""
    events = bars_of(case.candles)
    job = SandboxJob(case.code, case.params, MINUTE, case.cap, TERMS, events)
    result = asyncio.run(run_in_container(job, image, wall_seconds=30.0))
    assert result.status == "ok", result
    sim = Simulator(TERMS)
    answers = iter(result.outputs)
    pairs: list[tuple[StrategyEvent, Output]] = []
    for event in events:
        for fed in [*sim.on_market(event), event]:
            output = next(answers)
            pairs.append((fed, output))
            sim.on_output(output, decision_time(fed))
    assert next(answers, "end") == "end"
    return pairs


def live(tmp_path: Path, image: str, case: Case) -> tuple[Rig, Recorded]:
    """같은 코드를 실행기로 켜고, 봉을 2~15초 늦게 올리며 마지막 봉 뒤까지 돌린다."""
    recorded = Recorded()
    rig = Rig(tmp_path, sessions=recorded, busy=lambda: recorded.inflight > 0)
    candles, code = case.candles, case.code
    rig.exchange.tape = {SYMBOL: candles}
    for i, c in enumerate(candles):
        rig.exchange.publish(SYMBOL, c, timedelta(seconds=DELAYS[i % len(DELAYS)]))
    cap = case.cap
    run_scope = scope(quantity=min(cap, 2), max_position=cap, daily_loss=Decimal("100000000"))

    async def scenario() -> None:
        async with executor(image, rig.timer.now) as client:
            recorded.inner = client
            run_id = await rig.runner.start(
                rig.user, approval(MINUTE, run_scope, code, case.params), code, None
            )
            await rig.timer.run_until(candles[-1].at + timedelta(minutes=1, seconds=40))
            assert rig.row(run_id).state == "running"
            assert await rig.runner.stop(rig.user, run_id)
            await rig.runner.shutdown()

    asyncio.run(scenario())
    return rig, recorded


def fills(pairs: list[tuple[StrategyEvent, Output]]) -> list[tuple[str, int, Decimal]]:
    return [(e.side, e.qty, e.price) for e, _ in pairs if isinstance(e, FillEvent)]


def flat(i: int, price: str) -> Candle:
    return candle(T0 + timedelta(minutes=i), price, price, price, price)


# 평균 100인 봉 20개 뒤 97에서 사고(다음 봉 시가 98 + 1틱 = 98.05에 체결), 100에서 정리한다(99.95).
MEAN_BARS = [flat(i, p) for i, p in enumerate(["99", "101"] * 10 + ["97", "98", "100", "100"])]
# window 3, 한 단계 1%: 98.85에 1계약, 96.95에 1계약을 더 사고, 98.85에 2계약을 판다(지정가).
SCALE_BARS = [
    candle(T0 + timedelta(minutes=i), o, h, low, c)
    for i, (o, h, low, c) in enumerate(
        [
            ("100", "100", "100", "100"),
            ("100", "100", "100", "100"),
            ("100", "100", "100", "100"),
            ("100", "100", "98.9", "98.9"),
            ("98.9", "99", "98.8", "97"),
            ("97", "97.1", "96.9", "96.5"),
            ("96.5", "98.9", "96.5", "98.8"),
            ("98.8", "99", "98.7", "99"),
            ("99", "99", "98.85", "99"),
        ]
    )
]
SCALE_PARAMS: dict[str, JsonValue] = {"window": 3, "step": "0.01", "max_steps": 3, "tick": "0.05"}


CASES = [
    pytest.param(
        Case(
            MEAN_REVERSION,
            MEAN_REVERSION_PARAMS,
            MEAN_BARS,
            1,
            [("buy", 1, Decimal("98.05")), ("sell", 1, Decimal("99.95"))],
        ),
        id="mean-reversion",
    ),
    pytest.param(
        Case(
            SCALE_IN_LIMIT,
            SCALE_PARAMS,
            SCALE_BARS,
            3,
            [
                ("buy", 1, Decimal("98.85")),
                ("buy", 1, Decimal("96.95")),
                ("sell", 2, Decimal("98.85")),
            ],
        ),
        id="scale-in-limit",
    ),
]


@pytest.mark.parametrize("case", CASES)
def test_the_runner_feeds_the_session_what_the_backtest_feeds_and_gets_the_same_outputs(
    tmp_path: Path, sandbox_image: str, case: Case
) -> None:
    expected = backtested(sandbox_image, case)
    rig, recorded = live(tmp_path, sandbox_image, case)

    assert fills(expected) == case.fills
    # 시장 이벤트는 그대로, 체결은 같은 자리(그 체결을 낳은 봉 다음 봉 앞)에 같은 수량·가격으로.
    shape = [(type(e).__name__, e.symbol) for e, _ in recorded.fed]
    assert shape == [(type(e).__name__, e.symbol) for e, _ in expected]
    assert [e for e, _ in recorded.fed if isinstance(e, BarEvent)] == bars_of(case.candles)
    assert fills(recorded.fed) == case.fills
    # 출력이 줄마다 같다.
    assert [o for _, o in recorded.fed] == [o for _, o in expected]
    # NH로 나간 주문은 체결 수와 같고, 끝에 포지션은 0이다.
    assert len(rig.exchange.order_requests()) == len(case.fills)
    assert held(rig) == 0


ASKS_TOO_MUCH = 'def on_event(event, ctx):\n    return {"A016C000": {"position": 3}}\n'


def test_an_output_beyond_the_approved_max_position_never_reaches_nh(
    tmp_path: Path, sandbox_image: str
) -> None:
    recorded = Recorded()
    rig = Rig(tmp_path, sessions=recorded, busy=lambda: recorded.inflight > 0)
    rig.exchange.publish(SYMBOL, flat(0, "780"), timedelta(seconds=2))

    async def scenario() -> str:
        async with executor(sandbox_image, rig.timer.now) as client:
            recorded.inner = client
            run_id = await rig.runner.start(
                rig.user,
                approval(MINUTE, scope(max_position=1), ASKS_TOO_MUCH),
                ASKS_TOO_MUCH,
                None,
            )
            await rig.timer.run_until(T0 + timedelta(minutes=1, seconds=30))
            await rig.runner.shutdown()
            return run_id

    run_id = asyncio.run(scenario())

    row = rig.row(run_id)
    assert (row.state, row.reason, row.session_id) == ("stopped", "invalid_output", None)
    assert rig.exchange.order_requests() == []
