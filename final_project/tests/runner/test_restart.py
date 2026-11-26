"""서버를 다시 띄우면 돌던 실행은 모두 일시정지(server_restart)가 되고, 남은 세션은 닫히고, 저절로
다시 켜지지 않는다."""

import asyncio
from pathlib import Path

from nh_mcp.domain import Target
from nh_mcp.runner.runner import Runner
from nh_mcp.store import strategy_runs

from .rig import Rig
from .scripts import TICK, after_minute, by_minute, publish_minutes, start


def test_restart_pauses_running_runs_closes_sessions_and_resumes_nothing(tmp_path: Path) -> None:
    rig = Rig(tmp_path, by_minute({i: Target(1) for i in range(10)}))
    publish_minutes(rig, ["780.00"] * 10)

    async def scenario() -> tuple[list[str], list[str | None], Runner]:
        bars = await start(rig)
        ticks = await start(rig, TICK)
        sessions = [rig.row(r).session_id for r in (bars, ticks)]
        # 서버가 내려간다: 작업만 멈추고 상태는 그대로.
        await rig.runner.shutdown()
        assert {rig.row(r).state for r in (bars, ticks)} == {"running"}
        # 다시 뜬다.
        again = Runner(rig.deps)
        await again.boot()
        await rig.timer.run_until(after_minute(5))
        return [bars, ticks], sessions, again

    runs, sessions, again = asyncio.run(scenario())

    for run_id in runs:
        row = rig.row(run_id)
        assert (row.state, row.reason, row.session_id) == ("paused", "server_restart", None)
    assert None not in sessions
    assert sorted(rig.sessions.closed) == sorted(s for s in sessions if s is not None)
    assert again.active(rig.user) == []
    # 일시정지된 실행은 이벤트를 받지 않고 주문도 내지 않는다.
    assert rig.sessions.market_events() == []
    assert rig.exchange.order_requests() == []


def test_a_paused_run_can_only_be_stopped(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    async def scenario() -> tuple[str, bool]:
        run_id = await start(rig)
        await rig.runner.shutdown()
        again = Runner(rig.deps)
        await again.boot()
        return run_id, await again.stop(rig.user, run_id)

    run_id, done = asyncio.run(scenario())

    assert done is True
    row = rig.row(run_id)
    assert (row.state, row.reason) == ("stopped", "user")
    kinds = [e.kind for e in strategy_runs.events(rig.conn, rig.user, run_id)]
    assert kinds.count("stop") == 2  # 일시정지, 멈춤
