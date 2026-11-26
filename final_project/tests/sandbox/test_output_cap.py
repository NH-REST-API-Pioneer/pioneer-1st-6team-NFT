"""표준 출력 상한(사람 결정 2026-11-25).

줄 하나 64KiB, 합계 max(1MiB, 256바이트 * 넣은 이벤트 줄 수).
"""

import asyncio
import json
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from nh_mcp.domain import BarEvent, Candle, MarketEvent, SandboxJob, SandboxResult, SymbolTerms
from nh_mcp.sandbox import container
from nh_mcp.sandbox.container import MAX_LINE, MAX_STDERR, MAX_STDOUT, run_in_container

from .wire import T0, bars, job

# 실제 근월물 코드 길이(8자)의 두 종목.
NEAR, FAR = "A756C000", "A756D000"
TERMS = {
    symbol: SymbolTerms(Decimal("0.05"), Decimal("250000"), Decimal("0")) for symbol in (NEAR, FAR)
}
# 이벤트마다 두 종목 목표를 낸다. 지금 포지션(0)이라 체결은 나지 않는다.
BOTH = f"""
def on_event(event, ctx):
    return {{"{NEAR}": {{"position": 0}}, "{FAR}": {{"position": 0}}}}
"""


def pair_bars(n: int) -> list[MarketEvent]:
    """두 종목의 1분봉을 번갈아 n개."""
    out: list[MarketEvent] = []
    for i in range(n):
        price = Decimal(350 + i % 7)
        candle = Candle(T0 + timedelta(minutes=i // 2), price, price, price, price, 10)
        out.append(BarEvent(NEAR if i % 2 == 0 else FAR, "minute", candle))
    return out


@pytest.mark.docker
def test_two_symbol_target_on_every_event_runs_25000_events(sandbox_image: str) -> None:
    events = pair_bars(25_000)
    line = json.dumps({"output": {NEAR: {"position": 0}, FAR: {"position": 0}}}) + "\n"
    # 옛 상한(합계 1MiB)이면 약 1.5만 이벤트에서 output으로 끊겼다.
    assert len(events) * len(line) > MAX_STDOUT
    work = SandboxJob(
        code=BOTH,
        params={},
        streams={NEAR: frozenset({"minute"}), FAR: frozenset({"minute"})},
        max_position=1,
        terms=TERMS,
        events=events,
    )
    reply = asyncio.run(run_in_container(work, sandbox_image))
    assert reply.status == "ok", reply
    assert len(reply.outputs) == 25_000


@pytest.mark.docker
def test_line_over_64kib_is_output(sandbox_image: str) -> None:
    # 계약에 맞든 아니든 한 줄이 64KiB를 넘으면 그 줄을 다 읽지 않고 끊는다.
    code = 'def on_event(event, ctx):\n    return {"A": {"position": 0, "price": "1" * 70_000}}\n'
    reply = asyncio.run(run_in_container(job(code), sandbox_image, wall_seconds=8.0))
    assert reply == SandboxResult("output", [], 0, "출력 한 줄이 64KiB를 넘었습니다")


# 프로토콜 출구(기술자 3~9 중 하나)에 계약에 맞는 60KB 줄을 하나 더 쓴다. 줄 하나는 한도 안이지만
# 실행기는 이벤트마다 한 줄씩 읽어 가므로 읽은 양이 이벤트 두 개에 약 60KB씩 쌓여 1MiB를 넘는다.
PADDED_FLOOD = """
import os

LINE = b'{"output": null' + b" " * 60_000 + b'}\\n'

def on_event(event, ctx):
    for fd in range(3, 10):
        try:
            os.write(fd, LINE)
        except OSError:
            pass
    return None
"""


@pytest.mark.docker
def test_extra_lines_inside_the_line_cap_fail_past_the_total_cap(sandbox_image: str) -> None:
    reply = asyncio.run(run_in_container(job(PADDED_FLOOD, bars(60)), sandbox_image, 20.0))
    detail = f"표준 출력이 한도({MAX_STDOUT}바이트)를 넘었습니다"
    assert reply == SandboxResult("output", [], 34, detail)


# 컨테이너 대신 띄우는 가짜 하네스. 이벤트마다 argv[1]바이트(줄바꿈 포함) 줄로 답한다.
SIZED_ANSWER = """
import sys

size = int(sys.argv[1])
line = b'{"output": null' + b" " * (size - 17) + b'}\\n'
sys.stdin.readline()
sys.stdout.buffer.write(b'{"ready": true}\\n')
sys.stdout.flush()
while sys.stdin.readline():
    sys.stdout.buffer.write(line)
    sys.stdout.flush()
"""


@pytest.mark.parametrize(
    ("size", "events", "status"),
    [
        # 합계 1.2MB: 옛 상한(1MiB)은 넘지만 이벤트 6000개 * 256바이트 = 1.536MB 안이다.
        (200, 6_000, "ok"),
        # 합계 1.5MB: 이벤트 5000개 * 256바이트 = 1.28MB를 넘는다.
        (300, 5_000, "output"),
        # 줄 하나가 한도를 넘는다.
        (MAX_LINE + 2, 1, "output"),
    ],
)
def test_total_cap_grows_with_the_events_fed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size: int, events: int, status: str
) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(SIZED_ANSWER, encoding="utf-8")

    def probe_argv(image: str, name: str) -> list[str]:
        return [sys.executable, "-I", str(probe), str(size)]

    monkeypatch.setattr(container, "docker_argv", probe_argv)
    assert size * events > MAX_STDOUT or size > MAX_LINE
    reply = asyncio.run(run_in_container(job("", bars(events)), "unused", wall_seconds=60.0))
    assert reply.status == status, reply
    if status == "ok":
        assert reply == SandboxResult("ok", [None] * events, None, "")


# 이벤트마다 print로 params["size"]바이트를 쓰는 전략. print는 표준 에러로 간다.
NOISY = """
import sys

def on_event(event, ctx):
    sys.stderr.write("x" * int(ctx.params["size"]))
    sys.stderr.flush()
    return None
"""


@pytest.mark.parametrize(
    ("size", "events", "status"),
    [
        # 합계 1.2MB: 고정 1MiB는 넘지만 이벤트 6000개 * 256바이트 = 1.536MB 안이다.
        (200, 6_000, "ok"),
        # 합계 1.5MB: 이벤트 5000개 * 256바이트 = 1.28MB를 넘는다.
        (300, 5_000, "output"),
    ],
)
def test_stderr_cap_grows_with_the_events_fed(
    sandbox_image: str, size: int, events: int, status: str
) -> None:
    # 띄워 둔 컨테이너는 며칠 돈다. 이벤트마다 print하는 전략이 고정 1MiB에 죽지 않아야 한다.
    assert size * events > MAX_STDERR
    run = job(NOISY, bars(events), params={"size": size})
    reply = asyncio.run(run_in_container(run, sandbox_image, wall_seconds=60.0))
    assert reply.status == status, reply
