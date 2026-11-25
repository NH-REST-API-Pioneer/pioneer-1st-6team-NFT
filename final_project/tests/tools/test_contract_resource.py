"""계약 리소스 strategy://contract: 로그인한 사용자만 읽고, 숫자는 모두 그 값을 쓰는 곳의
상수와 같다. 전략 도구의 설명은 이 리소스를 가리킨다. MCP 클라이언트로 실제 HTTP 서버를 부른다."""

import asyncio
import itertools
from pathlib import Path
from typing import Any

from conftest import Serve
from fakes import World
from fastmcp import Client

from nh_mcp.domain import MAX_MODEL_BYTES
from nh_mcp.models.store import MAX_FILES
from nh_mcp.ops.rate_limit import BACKTEST_BUCKET, LIMITS, WINDOW
from nh_mcp.realtime.recorder import MAX_SUBSCRIPTIONS
from nh_mcp.sandbox.container import (
    FIXED_ARGS,
    MAX_LINE,
    MAX_STDOUT,
    STDOUT_PER_EVENT,
    WALL_SECONDS,
)
from nh_mcp.sandbox.wire import MAX_LEVELS
from nh_mcp.services.backtest import (
    MAX_BARS,
    MAX_CODE_BYTES,
    MAX_EVENTS,
    MAX_PARAMS_BYTES,
    MAX_POSITION,
    MAX_SYMBOLS,
    MAX_TICK_EVENTS,
    PER_USER,
)
from nh_mcp.store.ticks import RETENTION
from nh_mcp.tools.contract import CONTRACT_URI, LIBRARIES, PYTHON

HARNESS = Path(__file__).resolve().parents[2] / "src" / "nh_mcp" / "sandbox" / "harness.py"
# 이 리소스를 가리켜야 하는 도구. preview_strategy는 주문 스위치를 켠 서버에만 있다.
STRATEGY_TOOLS = {
    "backtest_strategy",
    "preview_strategy",
    "set_tick_recording",
    "get_strategy_data",
}


def read_contract(serve: Serve, token: str) -> tuple[list[Any], list[Any] | None]:
    async def scenario() -> tuple[list[Any], list[Any] | None]:
        async with serve(World().mcp) as url, Client(url, auth=token) as client:
            listed = await client.list_resources()
            try:
                return listed, await client.read_resource(CONTRACT_URI)
            except Exception:
                return listed, None

    return asyncio.run(scenario())


def contract(serve: Serve) -> str:
    _, contents = read_contract(serve, "admin-token")
    assert contents is not None
    [content] = contents
    return content.text


def fixed(flag: str, prefix: str = "") -> str:
    """docker 고정 인자에서 flag 뒤 값 가운데 prefix로 시작하는 것."""
    pairs = itertools.pairwise(FIXED_ARGS)
    [value] = [v.removeprefix(prefix) for f, v in pairs if f == flag and v.startswith(prefix)]
    return value


def test_contract_is_listed_and_readable_with_login(serve: Serve) -> None:
    listed, contents = read_contract(serve, "admin-token")

    [resource] = [r for r in listed if str(r.uri) == CONTRACT_URI]
    assert resource.mime_type == "text/markdown"
    assert contents is not None
    [content] = contents
    assert content.text.startswith("# 전략 계약과 남는 제약")


def test_contract_needs_an_allowed_login(serve: Serve) -> None:
    listed, contents = read_contract(serve, "outsider-token")

    assert [str(r.uri) for r in listed] == []
    assert contents is None


def test_contract_numbers_come_from_the_limits_that_enforce_them(serve: Serve) -> None:
    text = contract(serve)
    mib = 1024 * 1024
    expected = [
        f"종목 {MAX_SYMBOLS}개까지",
        f"(최대 {MAX_LEVELS}단계)",
        f"settings.max_position(1~{MAX_POSITION})",
        f"{RETENTION.days}일 보관",
        f"APP Key당 {MAX_SUBSCRIPTIONS}건",
        f"흐름마다 {MAX_BARS:,}봉, 합계 {MAX_EVENTS:,}개",
        f"일봉을 합쳐 {MAX_TICK_EVENTS:,}개",
        f"코드 {MAX_CODE_BYTES // 1024}KB",
        f"JSON {MAX_PARAMS_BYTES // 1024}KB",
        f"CPU {fixed('--ulimit', 'cpu=').split(':')[0]}초",
        f"벽시계 {WALL_SECONDS:g}초",
        f"메모리 {fixed('--memory').removesuffix('m')}MB",
        f"프로세스·스레드 {fixed('--pids-limit')}개",
        f"한 줄 {MAX_LINE // 1024}KB",
        f"이벤트당 {STDOUT_PER_EVENT}바이트(최소 {MAX_STDOUT // mib}MB)",
        f"/tmp {fixed('--tmpfs', '/tmp:size=').split(',')[0].removesuffix('m')}MB뿐",
        f"동시 백테스트 {PER_USER}개, {WINDOW.seconds // 60}분에 {LIMITS[BACKTEST_BUCKET]}번",
        f"파일당 {MAX_MODEL_BYTES // mib}MB, 사용자당 {MAX_FILES}개",
        f"PYTHONHASHSEED={fixed('-e', 'PYTHONHASHSEED=')}",
        f"Python {PYTHON}",
    ]
    assert [e for e in expected if e not in text] == []
    for name, version in LIBRARIES:
        assert f"{name} {version.removesuffix('+cpu')}" in text, name
    # 하네스가 거는 시드도 리소스가 알리는 값과 같다.
    seed = fixed("-e", "PYTHONHASHSEED=")
    harness = HARNESS.read_text(encoding="utf-8")
    assert f"시드 {seed}" in text
    for call in ("random.seed", "numpy.random.seed", "module.manual_seed"):
        assert f"{call}({seed})" in harness, call


def test_contract_lists_every_example_and_refusal(serve: Serve) -> None:
    text = contract(serve)
    for example in ("mean-reversion", "pair-spread", "scale-in-limit", "tick-momentum"):
        assert example in text
    for code in ("INVALID_INPUT", "STRATEGY_FAILED", "TEMPORARILY_UNAVAILABLE"):
        assert f"- {code}" in text
    # 서비스가 STRATEGY_FAILED의 error.rule로 내는 사유 전부.
    assert (
        "STRATEGY_FAILED(error.rule): exception, cpu, memory, processes, output, timeout." in text
    )


def test_contract_stays_short_enough_to_read_before_every_strategy(serve: Serve) -> None:
    assert len(contract(serve).encode()) <= 7 * 1024


def test_strategy_tools_point_to_the_contract(serve: Serve) -> None:
    async def scenario() -> dict[str, str]:
        world = World(orders_enabled=True)
        async with serve(world.mcp) as url, Client(url, auth="admin-token") as client:
            return {t.name: t.description or "" for t in await client.list_tools()}

    described = asyncio.run(scenario())

    assert set(described) >= STRATEGY_TOOLS
    assert [n for n in sorted(STRATEGY_TOOLS) if CONTRACT_URI not in described[n]] == []
