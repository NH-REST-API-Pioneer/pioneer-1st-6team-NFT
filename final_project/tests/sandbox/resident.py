"""띄워 두는 컨테이너(세션)를 백테스트와 같은 모양으로 돌리는 테스트 도우미."""

import asyncio
import subprocess
from datetime import datetime, timedelta

from nh_mcp.domain import (
    KST,
    Output,
    SandboxJob,
    SandboxResult,
    SessionFailed,
    SessionJob,
    StrategyEvent,
)
from nh_mcp.sandbox.container import NAME_PREFIX, RESIDENT_PREFIX, ResidentSession


def session_job(work: SandboxJob) -> SessionJob:
    """일에서 이벤트를 빼고 지금부터 하루 뒤 만료를 더한다(실행기는 지난 만료를 받지 않는다)."""
    return SessionJob(
        work.code,
        work.params,
        work.streams,
        work.max_position,
        work.terms,
        datetime.now(KST) + timedelta(days=1),
        work.model,
    )


async def feed_all(work: SandboxJob, image: str, events: list[StrategyEvent]) -> SandboxResult:
    """세션을 열고 events를 하나씩 넣는다. 실패하면 그 순번(열기 실패면 None)과 사유, 출력은 비운다.

    체결 계산은 하지 않는다(세션에는 없다). 체결 이벤트가 필요하면 events에 넣는다.
    """
    opened = await ResidentSession.open(session_job(work), image)
    if isinstance(opened, SessionFailed):
        return SandboxResult(opened.reason, [], None, opened.detail)
    outputs: list[Output] = []
    try:
        for index, event in enumerate(events):
            output = await opened.feed(event)
            if isinstance(output, SessionFailed):
                return SandboxResult(output.reason, [], index, output.detail)
            outputs.append(output)
    finally:
        await opened.close()
    return SandboxResult("ok", outputs, None, "")


def run_resident(work: SandboxJob, image: str) -> SandboxResult:
    """일의 시장 이벤트를 세션에 그대로 넣는다(체결 이벤트 없음)."""
    return asyncio.run(feed_all(work, image, [*work.events]))


def leftovers(prefix: str = RESIDENT_PREFIX) -> str:
    """이름이 prefix로 시작하는 컨테이너(멈춘 것 포함)의 id."""
    listing = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name=^{prefix}", "-q"],
        capture_output=True,
        text=True,
        check=True,
    )
    return listing.stdout.strip()


def any_leftovers() -> str:
    return (leftovers(NAME_PREFIX) + " " + leftovers(RESIDENT_PREFIX)).strip()
