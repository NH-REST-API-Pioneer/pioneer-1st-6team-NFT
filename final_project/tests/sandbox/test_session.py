"""띄워 두는 컨테이너(M4b 세션). 실제 Docker로 돈다.

탈출 테스트는 test_escape.py가 백테스트 컨테이너와 띄워 두는 컨테이너 둘 다에 돈다.
"""

import asyncio
import json
import subprocess
import time
from collections.abc import AsyncGenerator, Iterator, Mapping
from contextlib import asynccontextmanager
from datetime import timedelta
from decimal import Decimal
from functools import partial

import httpx2
import pytest

from nh_mcp.domain import (
    FillEvent,
    MarketEvent,
    Output,
    SandboxJob,
    SessionFailed,
    StrategyEvent,
    SymbolTerms,
    Target,
)
from nh_mcp.sandbox.client import SandboxClient, SessionGone, SessionHandle
from nh_mcp.sandbox.container import (
    EVENT_SECONDS,
    ContainerStartError,
    ResidentSession,
    SessionClosedError,
    remove_leftovers,
    run_in_container,
)
from nh_mcp.sandbox.executor import make_app
from nh_mcp.strategies.backtest import Simulator, decision_time
from nh_mcp.strategies.examples import MEAN_REVERSION, MEAN_REVERSION_PARAMS

from .resident import feed_all, leftovers, session_job
from .test_container import FOLLOWS_FILLS, SEEDED_CHOICE, closes
from .wire import SYMBOL, T0, bars, job

pytestmark = pytest.mark.docker

TOKEN = "s" * 40
NONE = "def on_event(event, ctx):\n    return None\n"


@pytest.fixture(autouse=True)
def no_resident_left_behind() -> Iterator[None]:
    yield
    assert leftovers() == ""


def test_live_container_has_the_resident_limits(sandbox_image: str) -> None:
    """띄워 둔 컨테이너의 실제 설정(docker inspect). 누적 CPU 한도만 없다."""

    async def scenario() -> tuple[dict[str, object], str]:
        session = await ResidentSession.open(session_job(job(NONE)), sandbox_image)
        assert isinstance(session, ResidentSession), session
        try:
            inspect = await asyncio.to_thread(
                subprocess.run,
                ["docker", "inspect", "-f", "{{json .HostConfig}}|{{.Config.User}}", session.name],
                capture_output=True,
                text=True,
                check=True,
            )
        finally:
            await session.close()
        host, _, user = inspect.stdout.strip().partition("|")
        return json.loads(host), user

    host, user = asyncio.run(scenario())
    assert user == "65534:65534"
    assert host["NetworkMode"] == "none"
    assert host["ReadonlyRootfs"] is True
    assert host["Tmpfs"] == {"/tmp": "size=66m,noexec,nosuid"}
    assert host["CapDrop"] == ["ALL"]
    assert host["SecurityOpt"] == ["no-new-privileges"]
    assert (host["Memory"], host["MemorySwap"]) == (512 * 1024**2, 512 * 1024**2)
    assert (host["PidsLimit"], host["NanoCpus"]) == (32, 1_000_000_000)
    assert host["LogConfig"] == {"Type": "none", "Config": {}}
    assert host["Ulimits"] == [
        {"Name": "fsize", "Hard": 52428800, "Soft": 52428800},
        {"Name": "nofile", "Hard": 64, "Soft": 64},
    ]


# 프로세스가 보는 CPU 시간 한도. 무한이면 목표 1, 60초면 -1.
CPU_RLIMIT = """
import resource

def on_event(event, ctx):
    soft, hard = resource.getrlimit(resource.RLIMIT_CPU)
    if (soft, hard) == (resource.RLIM_INFINITY, resource.RLIM_INFINITY):
        return {"A": {"position": 1}}
    if (soft, hard) == (60, 60):
        return {"A": {"position": -1}}
    return None
"""


def test_resident_strategy_has_no_cpu_time_limit_but_backtests_do(sandbox_image: str) -> None:
    """누적 CPU 한도가 없다는 것을 전략 프로세스 쪽에서 본다(커널이 거는 RLIMIT_CPU).

    60초가 넘는 CPU를 실제로 태우는 테스트는 1분이 넘게 걸려 두지 않는다. CPU를 거의 쓰지 않고
    쉬는 세션은 CPU 한도가 있어도 죽지 않으므로 70초 쉬는 테스트는 이것을 보여 주지 못한다.
    """
    work = job(CPU_RLIMIT, bars(1))
    backtest = asyncio.run(run_in_container(work, sandbox_image, wall_seconds=8.0))
    resident = asyncio.run(feed_all(work, sandbox_image, [*work.events]))
    assert backtest.outputs == [{SYMBOL: Target(-1)}], backtest
    assert resident.outputs == [{SYMBOL: Target(1)}], resident


# 지금까지 받은 봉 수로 목표를 정한다. 같은 프로세스가 상태를 들고 있어야 0, 1, 0이 된다.
COUNTS = """
def on_event(event, ctx):
    return {"A": {"position": len(ctx.history("A", "minute")) % 2}}
"""


def test_session_keeps_state_and_answers_after_idling(sandbox_image: str) -> None:
    async def scenario() -> list[Output | SessionFailed]:
        session = await ResidentSession.open(session_job(job(COUNTS)), sandbox_image)
        assert isinstance(session, ResidentSession), session
        try:
            first = await session.feed(bars(1)[0])
            # 이벤트 마감(2초)보다 길게 쉰다. 쉬는 시간은 마감에 들지 않는다.
            await asyncio.sleep(EVENT_SECONDS + 1)
            rest = [await session.feed(event) for event in bars(3)[1:]]
        finally:
            await session.close()
        return [first, *rest]

    assert asyncio.run(scenario()) == [
        {SYMBOL: Target(0)},
        {SYMBOL: Target(1)},
        {SYMBOL: Target(0)},
    ]


def fed_by_backtest(
    events: list[MarketEvent], outputs: list[Output], terms: Mapping[str, SymbolTerms]
) -> list[StrategyEvent]:
    """백테스트가 넣은 순서(만든 체결 이벤트 포함)를 같은 체결 계산으로 다시 만든다."""
    sim = Simulator(terms)
    answers = iter(outputs)
    fed: list[StrategyEvent] = []
    for event in events:
        for item in [*sim.on_market(event), event]:
            fed.append(item)
            sim.on_output(next(answers), decision_time(item))
    assert next(answers, "end") == "end"
    return fed


@pytest.mark.parametrize(
    ("work", "fills"),
    [
        pytest.param(
            job(
                MEAN_REVERSION,
                closes(["99", "101"] * 10 + ["97", "98", "100", "100"]),
                MEAN_REVERSION_PARAMS,
            ),
            2,
            id="mean-reversion",
        ),
        pytest.param(job(FOLLOWS_FILLS, bars(4), max_position=2), 2, id="follows-fills"),
        pytest.param(job(SEEDED_CHOICE, bars(12)), None, id="seeded-random"),
    ],
)
def test_session_gives_the_backtest_outputs_for_the_same_events_and_fills(
    sandbox_image: str, work: SandboxJob, fills: int | None
) -> None:
    """백테스트가 넣은 것(시장 이벤트와 체결 계산이 만든 체결 이벤트)을 그 순서 그대로 세션에 넣으면
    출력이 줄마다 같다. 세션에는 체결 계산이 없으니 체결은 FillEvent로 직접 넣는다.
    """
    backtest = asyncio.run(run_in_container(work, sandbox_image, wall_seconds=20.0))
    assert backtest.status == "ok", backtest
    fed = fed_by_backtest([*work.events], backtest.outputs, work.terms)
    made = [e for e in fed if isinstance(e, FillEvent)]
    if fills is not None:
        assert len(made) == fills
    resident = asyncio.run(feed_all(work, sandbox_image, fed))
    assert resident.status == "ok", resident
    assert resident.outputs == backtest.outputs


def test_infinite_loop_kills_the_session_in_two_seconds(sandbox_image: str) -> None:
    code = (
        "def on_event(event, ctx):\n"
        "    if ctx.history('A', 'minute'):\n"
        "        while True:\n"
        "            pass\n"
        "    return None\n"
    )

    async def scenario() -> tuple[Output | SessionFailed, float, str]:
        session = await ResidentSession.open(session_job(job(code)), sandbox_image)
        assert isinstance(session, ResidentSession), session
        assert await session.feed(bars(1)[0]) is None
        started = time.monotonic()
        stuck = await session.feed(bars(2)[1])
        took = time.monotonic() - started
        gone = await asyncio.to_thread(leftovers)
        with pytest.raises(SessionClosedError):
            await session.feed(bars(3)[2])
        return stuck, took, gone

    stuck, took, gone = asyncio.run(scenario())
    assert stuck == SessionFailed("timeout", "2초 안에 답하지 않았습니다")
    assert EVENT_SECONDS <= took < EVENT_SECONDS + 3
    assert gone == ""


def test_bad_code_fails_to_open_and_leaves_no_container(sandbox_image: str) -> None:
    opened = asyncio.run(
        ResidentSession.open(session_job(job("def on_event(event):\n    pass\n")), sandbox_image)
    )
    assert isinstance(opened, SessionFailed)
    assert opened.reason == "invalid_code"


def test_output_outside_the_contract_ends_the_session(sandbox_image: str) -> None:
    code = 'def on_event(event, ctx):\n    return {"A": {"position": 2}}\n'
    reply = asyncio.run(feed_all(job(code), sandbox_image, [*bars(2)]))
    assert (reply.status, reply.index, reply.detail) == (
        "invalid_output",
        0,
        '{"A": {"position": 2}}',
    )


SLEEPS = "import time\n\ndef on_event(event, ctx):\n    time.sleep(1.5)\n    return None\n"


def test_close_while_feeding_removes_the_container_at_once(sandbox_image: str) -> None:
    async def scenario() -> tuple[float, str, BaseException | Output | SessionFailed]:
        session = await ResidentSession.open(session_job(job(SLEEPS)), sandbox_image)
        assert isinstance(session, ResidentSession), session
        feeding = asyncio.create_task(session.feed(bars(1)[0]))
        await asyncio.sleep(0.3)
        started = time.monotonic()
        await session.close()
        took = time.monotonic() - started
        gone = await asyncio.to_thread(leftovers)
        [outcome] = await asyncio.gather(feeding, return_exceptions=True)
        return took, gone, outcome

    took, gone, outcome = asyncio.run(scenario())
    assert gone == ""
    assert took < 1.2
    assert isinstance(outcome, SessionClosedError)


def test_cancelled_feed_removes_the_container(sandbox_image: str) -> None:
    async def scenario() -> str:
        session = await ResidentSession.open(session_job(job(SLEEPS)), sandbox_image)
        assert isinstance(session, ResidentSession), session
        feeding = asyncio.create_task(session.feed(bars(1)[0]))
        await asyncio.sleep(0.3)
        feeding.cancel()
        with pytest.raises(asyncio.CancelledError):
            await feeding
        with pytest.raises(SessionClosedError):
            await session.feed(bars(1)[0])
        return await asyncio.to_thread(leftovers)

    assert asyncio.run(scenario()) == ""


def test_missing_image_is_a_start_error() -> None:
    with pytest.raises(ContainerStartError):
        asyncio.run(ResidentSession.open(session_job(job(NONE)), "nh-mcp-sandbox:missing-tag"))


# 체결은 포지션을 그대로 목표로, 셋째 봉에서 예외를 낸다.
FILLS_THEN_RAISES = """
def on_event(event, ctx):
    if event["type"] == "fill":
        return {"A": {"position": ctx.positions["A"]}}
    if len(ctx.history("A", "minute")) == 2:
        raise ValueError("셋째 봉")
    return {"A": {"position": 1}}
"""


@asynccontextmanager
async def executor_client(image: str) -> AsyncGenerator[SandboxClient]:
    """실제 실행기 앱(수명 주기 포함)에 붙은 클라이언트. 끝나면 실행기가 남은 세션을 모두 닫는다."""
    app = make_app(
        TOKEN, partial(run_in_container, image=image), partial(ResidentSession.open, image=image)
    )
    client = SandboxClient("http://executor", TOKEN, httpx2.ASGITransport(app=app))
    async with app.router.lifespan_context(app):
        try:
            yield client
        finally:
            await client.aclose()


def test_client_executor_and_container_end_to_end(sandbox_image: str) -> None:
    """MCP 서버 쪽 클라이언트 → 실행기 API → 띄워 둔 컨테이너. 실패하면 그 뒤로는 SessionGone."""
    fill = FillEvent(SYMBOL, T0 + timedelta(minutes=1), "buy", 1, Decimal("351.05"))

    async def scenario() -> list[object]:
        seen: list[object] = []
        async with executor_client(sandbox_image) as client:
            bad = await client.open_session(session_job(job("on_event = 1\n")))
            seen.append(bad)
            handle = await client.open_session(session_job(job(FILLS_THEN_RAISES)))
            assert isinstance(handle, SessionHandle), handle
            for event in (bars(1)[0], fill, bars(2)[1], bars(3)[2]):
                seen.append(await client.feed(handle, event))
            with pytest.raises(SessionGone):
                await client.feed(handle, bars(4)[3])
            closed = await client.open_session(session_job(job(NONE)))
            assert isinstance(closed, SessionHandle), closed
            await client.close(closed)
            with pytest.raises(SessionGone):
                await client.feed(closed, bars(1)[0])
            # 이미 닫은 세션을 또 닫아도 된다.
            await client.close(closed)
        return seen

    seen = asyncio.run(scenario())
    assert seen[0] == SessionFailed("invalid_code", "on_event 함수가 없습니다")
    assert seen[1:4] == [{SYMBOL: Target(1)}, {SYMBOL: Target(1)}, {SYMBOL: Target(1)}]
    assert isinstance(seen[4], SessionFailed)
    assert seen[4].reason == "exception"
    assert "ValueError" in seen[4].detail


def test_executor_restart_leaves_no_session(sandbox_image: str) -> None:
    """실행기가 다시 시작하면(남은 컨테이너를 지우고 새 앱) 세션은 없고 MCP 쪽은 404를 받는다."""

    async def scenario() -> tuple[str, str]:
        # 옛 실행기의 수명 주기가 끝날 때 그쪽 프로세스도 거둔다(실제로는 실행기와 함께 사라진다).
        async with executor_client(sandbox_image) as before:
            handle = await before.open_session(session_job(job(NONE)))
            assert isinstance(handle, SessionHandle), handle
            running = await asyncio.to_thread(leftovers)
            assert await remove_leftovers() >= 1
            async with executor_client(sandbox_image) as after:
                with pytest.raises(SessionGone):
                    await after.feed(handle, bars(1)[0])
            gone = await asyncio.to_thread(leftovers)
        return running, gone

    running, gone = asyncio.run(scenario())
    assert running != ""
    assert gone == ""
