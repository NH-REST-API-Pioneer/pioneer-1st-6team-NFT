"""자동 실행 도구: start_strategy·stop_strategy·get_strategy_status와 "자동 실행" 묶음, 켜기 한도.

실제 HTTP MCP 서버에 도구를 붙이고, 그 뒤는 실행기 테스트의 가짜(상태 있는 가짜 NH, 대본대로 답하는
세션, 멈춰 둔 시계)로 돈다. 주문은 진짜 OrderService를 지나 가짜 NH로만 나간다(실제 주문 없음).
시계는 2026-12-01(화) 10:00 KST. 한도는 하루 손실 5,000,000원, 최대 3계약, 1회 2계약.
"""

import asyncio
import hashlib
from collections.abc import AsyncGenerator, Iterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import httpx2
import pytest
from conftest import Serve
from fakes import FakeMaster, FakeSandbox, auto_run_on, mcp_token
from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from mcp_types import Tool
from runner.rig import (
    CODE,
    SYMBOL,
    T0,
    FakeSessions,
    Rig,
    Strategy,
    approval,
    flat_bar,
    ignore,
    scope,
)
from runner.scripts import MINUTE, after_minute, by_minute

from nh_mcp.auth.access import AccessPolicy, make_gate
from nh_mcp.domain import (
    JsonValue,
    ModelHash,
    SessionFailed,
    SessionJob,
    Streams,
    Target,
    UserId,
)
from nh_mcp.guard.risk import EXPIRY_WARN_DAYS
from nh_mcp.markets.domestic import DomesticMarket
from nh_mcp.markets.domestic_master import DomesticMaster
from nh_mcp.markets.domestic_products import DomesticProducts
from nh_mcp.models.store import ModelStore
from nh_mcp.nh import limits as nh_limits
from nh_mcp.ops.rate_limit import LIMITS, RateLimiter
from nh_mcp.runner.runner import Runner
from nh_mcp.sandbox.client import SandboxBusy, SandboxClient, SessionHandle
from nh_mcp.server import build_server
from nh_mcp.services.backtest import BacktestService, Sources, code_hash, run_key
from nh_mcp.services.queries import QueryService
from nh_mcp.services.strategy import (
    MAX_LISTED_EVENTS,
    MAX_LISTED_RUNS,
    MAX_RUN_USERS,
    MAX_RUNS_PER_USER,
    StrategyService,
)
from nh_mcp.store import confirm_tokens, features, strategy_runs, users
from nh_mcp.store.backtests import SUCCESS, NewRun, record
from nh_mcp.store.confirm_tokens import NewToken
from nh_mcp.store.strategy_runs import Change, RunEvent, RunState, StrategyRun
from nh_mcp.tools.backtest import register_backtest_tools
from nh_mcp.tools.context import ToolContext
from nh_mcp.tools.strategy import register_strategy_tools

KEYS_URL = "http://localhost:8000/keys"
TOKENS = {
    "a-token": mcp_token("g-a", "a@example.com"),
    "b-token": mcp_token("g-b", "b@example.com"),
}
AUTO_RUN_TOOLS = {"preview_strategy", "start_strategy", "stop_strategy", "get_strategy_status"}
PRICE = Decimal("780.05")
EXPIRES = (T0 + timedelta(days=3)).isoformat(timespec="seconds")
NAME = "코스피200 F 202612"


@pytest.fixture(autouse=True)
def unlimited_nh(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """NH 호출 제한기는 실제 시계로 잠들어 멈춘 시계와 맞지 않는다(tests/runner/conftest.py)."""
    monkeypatch.setattr(nh_limits, "PER_SECOND", dict.fromkeys(nh_limits.PER_SECOND, 1_000_000))
    yield


@dataclass
class Window:
    """승인 창에서 사용자가 한 일을 흉내 내고, 창에 뜬 문구를 기록한다."""

    action: Literal["accept", "decline", "cancel"]
    value: bool = True
    messages: list[str] = field(default_factory=list[str])

    async def __call__(self, message: str, *_: object) -> ElicitResult:
        self.messages.append(message)
        content = {"value": self.value} if self.action == "accept" else None
        return ElicitResult(action=self.action, content=content)


class Bench:
    """실행기 가짜 위에 자동 실행 도구(와 백테스트 도구)를 붙인 서버. 두 사용자 모두 묶음을 켰다."""

    def __init__(
        self, tmp_path: Path, strategy: Strategy = ignore, sessions: FakeSessions | None = None
    ) -> None:
        self.rig = rig = Rig(tmp_path, strategy, sessions=sessions)
        self.sessions = rig.sessions if sessions is None else sessions
        self.conn, timer = rig.conn, rig.timer
        rig.exchange.market_price = lambda _symbol, _side: PRICE
        master = DomesticMaster(timer, httpx2.MockTransport(FakeMaster()))
        domestic = DomesticMarket(rig.pool, master, timer)
        self.models = ModelStore(tmp_path / "models", self.conn, timer)
        self.strategies = StrategyService(self.conn, timer, domestic, self.models, rig.runner)
        allowed = frozenset({"a@example.com", "b@example.com"})
        gate = make_gate(AccessPolicy(False, allowed), self.conn, timer)
        self.mcp = build_server(StaticTokenVerifier(TOKENS), gate, clock=timer)
        ctx = ToolContext(self.conn, timer, KEYS_URL)
        limiter = RateLimiter(timer, LIMITS)
        register_strategy_tools(self.mcp, ctx, self.strategies, limiter)
        sources = Sources(
            QueryService(domestic),
            DomesticProducts(rig.pool, timer),
            self.models,
            lambda: rig.recorder.store,
        )
        sandbox = SandboxClient(
            "http://executor.test", "t" * 40, transport=httpx2.MockTransport(FakeSandbox())
        )
        backtests = BacktestService(self.conn, timer, sources, sandbox)
        register_backtest_tools(self.mcp, ctx, backtests, limiter, "http://localhost:8000/models")
        auto_run_on(self.conn, rig.user, rig.other)

    def backtested(
        self, user: UserId, code: str = CODE, model: ModelHash | None = None, symbol: str = SYMBOL
    ) -> None:
        streams: Streams = {symbol: frozenset({"minute"})}
        key = run_key(code, {}, streams, model)
        period = (date(2026, 11, 2), date(2026, 11, 30))
        run = NewRun(user, key, code, "{}", period, None, None, 5, SUCCESS, None)
        record(self.conn, run, T0)

    def live(
        self,
        user: UserId,
        run_id: str,
        symbol: str,
        state: RunState = "starting",
        at: datetime = T0,
    ) -> None:
        """다른 곳에서 켠 실행 한 줄(실행기 작업은 없다)."""
        streams: Streams = {symbol: frozenset({"minute"})}
        run = StrategyRun(run_id, user, "domestic", streams, code_hash(CODE), {}, None, scope(), at)
        strategy_runs.insert(self.conn, run)
        if state != "starting":
            reason = "server_restart" if state == "paused" else None
            strategy_runs.set_state(self.conn, user, run_id, Change("starting", state, reason), T0)

    def token_used(self, token: str) -> bool:
        row = confirm_tokens.find(self.conn, token)
        assert row is not None
        return row.used_at is not None

    def runs(self, user: UserId | None = None) -> list[strategy_runs.RunRow]:
        return strategy_runs.list_for(self.conn, user or self.rig.user)

    @asynccontextmanager
    async def clients(
        self, serve: Serve, *tokens: str, window: Window | None = None
    ) -> AsyncGenerator[list["Caller"]]:
        """서버 하나에 사용자마다 연결 하나. 끝나면 실행기 작업을 멈춘다."""
        async with serve(self.mcp) as url, AsyncExitStack() as stack:
            callers = [
                Caller(
                    await stack.enter_async_context(
                        Client(url, auth=token, elicitation_handler=window)
                    )
                )
                for token in tokens
            ]
            yield callers
        await self.rig.runner.shutdown()

    @asynccontextmanager
    async def client(
        self, serve: Serve, token: str = "a-token", window: Window | None = None
    ) -> AsyncGenerator["Caller"]:
        async with self.clients(serve, token, window=window) as (caller,):
            yield caller


class Caller:
    def __init__(self, client: Client[Any]) -> None:
        self.client = client

    async def __call__(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        content = (await self.client.call_tool(tool, arguments)).structured_content
        assert content is not None
        return content

    async def names(self) -> set[str]:
        return {t.name for t in await self.client.list_tools()}

    async def tools(self) -> dict[str, Tool]:
        return {t.name: t for t in await self.client.list_tools()}


def preview_args(mode: str = "auto", model: str | None = None) -> dict[str, Any]:
    strategy: dict[str, Any] = {"code": CODE, "params": {}, "streams": {SYMBOL: ["minute"]}}
    if model is not None:
        strategy["model"] = model
    scope = {
        "quantity": 1,
        "max_position": 1,
        "daily_orders": 10,
        "daily_loss": "1000000",
        "expires_at": EXPIRES,
        "on_loss": "hold",
        "mode": mode,
    }
    return {"strategy": strategy, "scope": scope}


async def preview_and_start(
    call: Caller, mode: str = "auto", model: str | None = None
) -> tuple[str, dict[str, Any]]:
    previewed = await call("preview_strategy", preview_args(mode, model))
    assert previewed["ok"] is True, previewed
    token: str = previewed["data"]["confirm_token"]
    return token, await call("start_strategy", {"confirm_token": token})


def started(serve: Serve, bench: Bench, window: Window | None = None) -> dict[str, Any]:
    async def scenario() -> dict[str, Any]:
        async with bench.client(serve, window=window) as call:
            _, result = await preview_and_start(call)
            return result

    return asyncio.run(scenario())


# ---- 등록·묶음 -----------------------------------------------------------------


def test_start_strategy_takes_the_confirm_token_and_nothing_else(
    serve: Serve, tmp_path: Path
) -> None:
    bench = Bench(tmp_path)

    async def scenario() -> dict[str, Tool]:
        async with bench.client(serve) as call:
            return await call.tools()

    tools = asyncio.run(scenario())

    schema = tools["start_strategy"].input_schema
    assert set(schema["properties"]) == {"confirm_token"}
    assert schema["required"] == ["confirm_token"]
    hints: dict[str, tuple[bool | None, bool | None]] = {}
    for name in AUTO_RUN_TOOLS:
        annotations = tools[name].annotations
        assert annotations is not None, name
        hints[name] = (annotations.read_only_hint, annotations.destructive_hint)
    assert hints == {
        "preview_strategy": (False, False),
        "start_strategy": (False, True),
        "stop_strategy": (False, True),
        "get_strategy_status": (True, None),
    }
    # 설명의 숫자는 서버가 실제로 쓰는 값이다.
    status = tools["get_strategy_status"].description or ""
    assert f"{MAX_LISTED_RUNS}개" in status and f"{MAX_LISTED_EVENTS}개" in status
    assert f"동시 {MAX_RUNS_PER_USER}개" in (tools["start_strategy"].description or "")


def test_with_the_bundle_off_the_four_tools_are_hidden_from_that_user_only(
    serve: Serve, tmp_path: Path
) -> None:
    bench = Bench(tmp_path)
    features.turn_off(bench.conn, bench.rig.other, "auto_run")

    async def scenario() -> tuple[set[str], set[str]]:
        async with bench.clients(serve, "b-token", "a-token") as (off, on):
            return await off.names(), await on.names()

    hidden, shown = asyncio.run(scenario())

    assert not hidden & AUTO_RUN_TOOLS
    assert "backtest_strategy" in hidden
    assert shown >= AUTO_RUN_TOOLS


def test_with_the_bundle_off_preview_and_start_are_refused_with_the_switch_address(
    serve: Serve, tmp_path: Path
) -> None:
    bench = Bench(tmp_path)
    bench.backtested(bench.rig.user)

    async def scenario() -> tuple[str, dict[str, Any], dict[str, Any]]:
        async with bench.client(serve) as call:
            previewed = await call("preview_strategy", preview_args())
            features.turn_off(bench.conn, bench.rig.user, "auto_run")
            token = previewed["data"]["confirm_token"]
            return (
                token,
                await call("preview_strategy", preview_args()),
                await call("start_strategy", {"confirm_token": token}),
            )

    token, previewed, start = asyncio.run(scenario())

    for refused in (previewed, start):
        assert refused["ok"] is False
        assert (refused["error"]["code"], refused["error"]["rule"]) == (
            "RISK_BLOCKED",
            "auto_run_off",
        )
        assert KEYS_URL in refused["error"]["message"]
    assert bench.runs() == [] and bench.sessions.opened == []
    assert not bench.token_used(token)


def test_with_the_bundle_off_stop_and_status_still_answer(serve: Serve, tmp_path: Path) -> None:
    """묶음을 끄면 전략이 모두 꺼져 둘은 남은 기록을 보거나 끄는 일뿐이다. 끄기는 막지 않는다."""
    bench = Bench(tmp_path)
    bench.live(bench.rig.user, "paused-1", SYMBOL, "paused")
    features.turn_off(bench.conn, bench.rig.user, "auto_run")

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        async with bench.client(serve) as call:
            return (
                await call("get_strategy_status", {}),
                await call("stop_strategy", {"run_id": "paused-1"}),
            )

    status, stopped = asyncio.run(scenario())

    assert [r["run_id"] for r in status["data"]["runs"]] == ["paused-1"]
    assert (stopped["data"]["state"], stopped["data"]["reason"]) == ("stopped", "user")


def test_turning_the_bundle_off_stops_running_runs_and_hides_the_tools(
    serve: Serve, tmp_path: Path
) -> None:
    bench = Bench(tmp_path)
    bench.backtested(bench.rig.user)

    async def scenario() -> tuple[int, set[str]]:
        async with bench.client(serve) as call:
            _, result = await preview_and_start(call)
            assert result["ok"] is True, result
            count = await bench.strategies.set_auto_run(bench.rig.user, False)
            return count, await call.names()

    count, names = asyncio.run(scenario())

    assert count == 1
    [row] = bench.runs()
    assert (row.state, row.reason) == ("stopped", "user")
    assert bench.rig.runner.active(bench.rig.user) == []
    assert not names & AUTO_RUN_TOOLS


def test_turning_the_bundle_off_while_starting_stops_the_new_run(
    serve: Serve, tmp_path: Path
) -> None:
    class SwitchedOff(FakeSessions):
        """세션을 여는 사이 사용자가 등록 화면에서 자동 실행을 끈다."""

        def __init__(self) -> None:
            super().__init__()
            self.bench: Bench | None = None

        async def open_session(self, job: SessionJob) -> SessionHandle | SessionFailed:
            assert self.bench is not None
            features.turn_off(self.bench.conn, self.bench.rig.user, "auto_run")
            return await super().open_session(job)

    sessions = SwitchedOff()
    bench = Bench(tmp_path, sessions=sessions)
    sessions.bench = bench
    bench.backtested(bench.rig.user)

    result = started(serve, bench)

    assert (result["error"]["code"], result["error"]["rule"]) == ("RISK_BLOCKED", "auto_run_off")
    [row] = bench.runs()
    assert (row.state, row.reason) == ("stopped", "user")
    assert len(sessions.closed) == 1


# ---- 켜기 -------------------------------------------------------------------


def test_started_run_answers_with_its_id_scope_and_how_to_follow_it(
    serve: Serve, tmp_path: Path
) -> None:
    bench = Bench(tmp_path)
    bench.backtested(bench.rig.user)

    result = started(serve, bench, Window("accept"))

    assert result["ok"] is True, result
    data = result["data"]
    [row] = bench.runs()
    assert data["run_id"] == row.run.run_id
    assert data["state"] == "running"
    assert data["streams"] == {SYMBOL: ["minute"]}
    assert data["mode"] == "auto"
    assert data["scope"]["expires_at"] == EXPIRES
    assert "get_strategy_status" in data["note"]
    # 승인한 코드가 그대로 세션에 들어갔다.
    [job] = bench.sessions.opened
    assert job.code == CODE


def test_the_window_shows_the_stored_scope_and_the_three_accepted_warnings(
    serve: Serve, tmp_path: Path
) -> None:
    bench = Bench(tmp_path)
    bench.backtested(bench.rig.user)
    window = Window("accept")

    started(serve, bench, window)

    (message,) = window.messages
    for part in (
        f"{NAME}({SYMBOL}) 1분봉",
        "자동 모드",
        "1회 1계약 · 최대 1계약 · 하루 10회 · 일일 손실 1,000,000원",
        "2026-12-04 10:00까지",
        f"코드 {code_hash(CODE)[:12]} · 모델 없음",
        "시장가",
        "15:35~15:45",
        f"만기까지 {EXPIRY_WARN_DAYS}일 이하",
        "승인할까요?",
    ):
        assert part in message, part


def test_suggest_mode_window_has_no_order_warnings(serve: Serve, tmp_path: Path) -> None:
    bench = Bench(tmp_path)
    bench.backtested(bench.rig.user)
    window = Window("accept")

    async def scenario() -> None:
        async with bench.client(serve, window=window) as call:
            await preview_and_start(call, mode="suggest")

    asyncio.run(scenario())

    (message,) = window.messages
    assert "제안 모드" in message and "주문하지 않고" in message
    assert "시장가" not in message


@pytest.mark.parametrize("window", [Window("decline"), Window("cancel"), Window("accept", False)])
def test_declined_starts_nothing_and_uses_up_the_token(
    serve: Serve, tmp_path: Path, window: Window
) -> None:
    bench = Bench(tmp_path)
    bench.backtested(bench.rig.user)

    async def scenario() -> tuple[str, dict[str, Any], dict[str, Any]]:
        async with bench.client(serve, window=window) as call:
            token, first = await preview_and_start(call)
            return token, first, await call("start_strategy", {"confirm_token": token})

    token, first, again = asyncio.run(scenario())

    assert first["error"]["code"] == "CONFIRM_EXPIRED"
    assert "승인하지 않아" in first["error"]["message"]
    assert again["error"]["code"] == "CONFIRM_EXPIRED"
    assert len(window.messages) == 1
    assert bench.token_used(token)
    assert bench.runs() == [] and bench.sessions.opened == []


def test_a_client_without_a_window_still_needs_the_token_and_starts_once(
    serve: Serve, tmp_path: Path
) -> None:
    bench = Bench(tmp_path)
    bench.backtested(bench.rig.user)

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        async with bench.client(serve) as call:
            token, first = await preview_and_start(call)
            return first, await call("start_strategy", {"confirm_token": token})

    first, again = asyncio.run(scenario())

    assert first["ok"] is True, first
    assert again["error"]["code"] == "CONFIRM_EXPIRED"
    assert len(bench.runs()) == 1


def test_expired_other_users_made_up_and_order_tokens_are_refused(
    serve: Serve, tmp_path: Path
) -> None:
    bench = Bench(tmp_path)
    bench.backtested(bench.rig.user)
    order_token, _ = confirm_tokens.issue(
        bench.conn, NewToken(bench.rig.user, "order", "{}"), T0, timedelta(minutes=2)
    )

    async def scenario() -> list[dict[str, Any]]:
        async with bench.clients(serve, "a-token", "b-token") as (a, b):
            previewed = await a("preview_strategy", preview_args())
            token = previewed["data"]["confirm_token"]
            answers = [
                await b("start_strategy", {"confirm_token": token}),
                await a("start_strategy", {"confirm_token": "made-up"}),
                await a("start_strategy", {"confirm_token": order_token}),
            ]
            await bench.rig.timer.advance(121)
            answers.append(await a("start_strategy", {"confirm_token": token}))
            return answers

    answers = asyncio.run(scenario())

    for answer in answers:
        assert answer["error"]["code"] == "CONFIRM_EXPIRED", answer
        assert "preview_strategy" in answer["error"]["message"]
    assert bench.runs() == [] and bench.runs(bench.rig.other) == []


def test_the_code_comes_from_my_own_successful_backtest_only(serve: Serve, tmp_path: Path) -> None:
    """토큰의 코드 해시를 다른 사용자만 백테스트했다면 켜지 않는다(내 기록이 생기면 켠다)."""
    bench = Bench(tmp_path)
    bench.backtested(bench.rig.other)
    payload = approval(MINUTE, scope())
    new = NewToken(bench.rig.user, "strategy", payload)
    token, _ = confirm_tokens.issue(bench.conn, new, T0, timedelta(minutes=2))

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        async with bench.client(serve) as call:
            refused = await call("start_strategy", {"confirm_token": token})
            bench.backtested(bench.rig.user)
            return refused, await call("start_strategy", {"confirm_token": token})

    refused, accepted = asyncio.run(scenario())

    assert (refused["error"]["code"], refused["error"]["rule"]) == (
        "RISK_BLOCKED",
        "not_backtested",
    )
    assert accepted["ok"] is True, accepted
    [row] = bench.runs()
    assert row.state == "running"
    assert bench.runs(bench.rig.other) == []


def test_a_model_file_removed_after_preview_is_refused_before_using_the_token(
    serve: Serve, tmp_path: Path
) -> None:
    bench = Bench(tmp_path)
    content = b"trained-model"
    hashed = ModelHash(hashlib.sha256(content).hexdigest())

    async def upload() -> None:
        draft = bench.models.draft(bench.rig.user)
        draft.write(content)
        assert await bench.models.commit(bench.rig.user, draft, "m.pkl") == "uploaded"

    asyncio.run(upload())
    bench.backtested(bench.rig.user, model=hashed)
    window = Window("accept")

    async def scenario() -> tuple[str, dict[str, Any]]:
        async with bench.client(serve, window=window) as call:
            previewed = await call("preview_strategy", preview_args(model=hashed))
            token = previewed["data"]["confirm_token"]
            assert bench.models.delete(bench.rig.user, hashed)
            return token, await call("start_strategy", {"confirm_token": token})

    token, refused = asyncio.run(scenario())

    assert refused["error"]["rule"] == "model_missing"
    assert f"모델 {hashed[:12]}" in window.messages[0]
    assert not bench.token_used(token)
    assert bench.runs() == []


# ---- 한도 -------------------------------------------------------------------


def refused_start(serve: Serve, bench: Bench) -> tuple[str, dict[str, Any], Window]:
    window = Window("accept")

    async def scenario() -> tuple[str, dict[str, Any]]:
        async with bench.client(serve, window=window) as call:
            return await preview_and_start(call)

    token, result = asyncio.run(scenario())
    return token, result, window


def test_a_sixth_live_run_is_refused_before_the_window_and_keeps_the_token(
    serve: Serve, tmp_path: Path
) -> None:
    bench = Bench(tmp_path)
    bench.backtested(bench.rig.user)
    for i in range(MAX_RUNS_PER_USER):
        bench.live(bench.rig.user, f"r{i}", f"A{i}6C000", "running" if i % 2 else "starting")

    token, result, window = refused_start(serve, bench)

    assert (result["error"]["code"], result["error"]["rule"]) == ("RISK_BLOCKED", "too_many_runs")
    assert "stop_strategy" in result["error"]["message"]
    assert window.messages == []
    assert not bench.token_used(token)
    assert bench.sessions.opened == []


def test_paused_and_stopped_runs_do_not_count(serve: Serve, tmp_path: Path) -> None:
    bench = Bench(tmp_path)
    bench.backtested(bench.rig.user)
    for i in range(MAX_RUNS_PER_USER):
        bench.live(bench.rig.user, f"r{i}", SYMBOL if i == 0 else f"A{i}6C000", "paused")
    bench.live(bench.rig.user, "done", SYMBOL, "stopped")

    result = started(serve, bench)

    assert result["ok"] is True, result


def test_one_live_run_per_symbol(serve: Serve, tmp_path: Path) -> None:
    bench = Bench(tmp_path)
    bench.backtested(bench.rig.user)
    bench.live(bench.rig.user, "first", SYMBOL, "running")
    # 다른 사용자가 같은 종목을 돌리는 것은 상관없다.
    bench.live(bench.rig.other, "theirs", SYMBOL, "running")

    token, result, window = refused_start(serve, bench)

    assert result["error"]["rule"] == "symbol_in_use"
    assert "first" in result["error"]["message"] and "theirs" not in result["error"]["message"]
    assert window.messages == [] and not bench.token_used(token)


def test_the_101st_user_is_told_to_try_again_later(serve: Serve, tmp_path: Path) -> None:
    bench = Bench(tmp_path)
    bench.backtested(bench.rig.user)
    for i in range(MAX_RUN_USERS):
        other = users.get_or_create(bench.conn, f"g-{i}", f"u{i}@example.com", T0)
        bench.live(other, f"u{i}", SYMBOL, "running")

    token, result, _ = refused_start(serve, bench)

    error = result["error"]
    assert (error["code"], error["rule"], error["retryable"]) == (
        "TEMPORARILY_UNAVAILABLE",
        "server_full",
        True,
    )
    assert not bench.token_used(token)


def test_a_full_executor_is_retryable_and_says_the_token_is_used(
    serve: Serve, tmp_path: Path
) -> None:
    class Full(FakeSessions):
        async def open_session(self, job: SessionJob) -> SessionHandle | SessionFailed:
            raise SandboxBusy("세션 40개")

    bench = Bench(tmp_path, sessions=Full())
    bench.backtested(bench.rig.user)

    token, result, _ = refused_start(serve, bench)

    error = result["error"]
    assert (error["code"], error["rule"], error["retryable"]) == (
        "TEMPORARILY_UNAVAILABLE",
        "sandbox_busy",
        True,
    )
    assert "preview_strategy부터" in error["message"]
    assert bench.token_used(token)
    [row] = bench.runs()
    assert (row.state, row.reason) == ("stopped", "sandbox_failed")


# ---- 끄기·상태 ---------------------------------------------------------------


def test_stop_ends_my_running_run_right_away(serve: Serve, tmp_path: Path) -> None:
    bench = Bench(tmp_path)
    bench.backtested(bench.rig.user)

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        async with bench.client(serve) as call:
            _, result = await preview_and_start(call)
            run_id = result["data"]["run_id"]
            return (
                await call("stop_strategy", {"run_id": run_id}),
                await call("stop_strategy", {"run_id": run_id}),
            )

    stopped, again = asyncio.run(scenario())

    assert (stopped["data"]["state"], stopped["data"]["reason"]) == ("stopped", "user")
    assert "get_account" in stopped["data"]["note"]
    assert again["data"] == stopped["data"]
    assert bench.rig.runner.active(bench.rig.user) == []
    assert len(bench.sessions.closed) == 1


def test_another_users_run_is_the_same_answer_as_no_run(serve: Serve, tmp_path: Path) -> None:
    bench = Bench(tmp_path)
    bench.live(bench.rig.user, "mine", SYMBOL, "paused")

    async def scenario() -> list[dict[str, Any]]:
        async with bench.client(serve, "b-token") as b:
            return [
                await b("stop_strategy", {"run_id": "mine"}),
                await b("stop_strategy", {"run_id": "nothing"}),
                await b("get_strategy_status", {"run_id": "mine"}),
                await b("get_strategy_status", {"run_id": "nothing"}),
                await b("get_strategy_status", {}),
            ]

    *refused, listed = asyncio.run(scenario())

    assert {(r["error"]["code"], r["error"]["message"]) for r in refused} == {
        ("INVALID_INPUT", refused[0]["error"]["message"])
    }
    assert listed["data"]["runs"] == []
    [row] = bench.runs()
    assert row.state == "paused"


def test_stop_without_a_run_id_stops_nothing(serve: Serve, tmp_path: Path) -> None:
    bench = Bench(tmp_path)
    bench.live(bench.rig.user, "mine", SYMBOL, "paused")

    async def scenario() -> list[dict[str, Any]]:
        async with bench.client(serve) as call:
            return [await call("stop_strategy", {}), await call("stop_strategy", {"run_id": " "})]

    for answer in asyncio.run(scenario()):
        assert answer["error"]["code"] == "INVALID_INPUT"
        assert "run_id" in answer["error"]["message"]
    [row] = bench.runs()
    assert row.state == "paused"


def test_a_server_restart_pause_is_explained_and_stopping_it_makes_it_stopped(
    serve: Serve, tmp_path: Path
) -> None:
    bench = Bench(tmp_path)
    bench.backtested(bench.rig.user)

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        async with bench.client(serve) as call:
            _, result = await preview_and_start(call)
            run_id = result["data"]["run_id"]
            # 서버가 내려갔다 다시 뜬다.
            await bench.rig.runner.shutdown()
            await Runner(bench.rig.deps).boot()
            return (
                await call("get_strategy_status", {"run_id": run_id}),
                await call("stop_strategy", {"run_id": run_id}),
            )

    status, stopped = asyncio.run(scenario())

    data = status["data"]
    assert (data["state"], data["reason"]) == ("paused", "server_restart")
    for part in ("get_account", "get_orders", "preview_strategy", "start_strategy"):
        assert part in data["explain"], part
    assert data["positions"] is None and data["pnl_today"] is None
    assert (stopped["data"]["state"], stopped["data"]["reason"]) == ("stopped", "user")


def test_status_lists_my_latest_twenty_only(serve: Serve, tmp_path: Path) -> None:
    bench = Bench(tmp_path)
    for i in range(MAX_LISTED_RUNS + 5):
        earlier = T0 - timedelta(hours=30 - i)
        bench.live(bench.rig.user, f"r{i:02d}", SYMBOL, "stopped", at=earlier)
    bench.live(bench.rig.other, "theirs", SYMBOL, "running")

    async def scenario() -> dict[str, Any]:
        async with bench.client(serve) as call:
            return await call("get_strategy_status", {})

    runs = asyncio.run(scenario())["data"]["runs"]

    assert len(runs) == MAX_LISTED_RUNS
    assert runs[0]["run_id"] == f"r{MAX_LISTED_RUNS + 4:02d}"
    assert "theirs" not in {r["run_id"] for r in runs}


def test_status_of_one_run_keeps_the_last_fifty_events_oldest_first(
    serve: Serve, tmp_path: Path
) -> None:
    bench = Bench(tmp_path)
    bench.live(bench.rig.user, "mine", SYMBOL, "stopped")
    for i in range(MAX_LISTED_EVENTS + 10):
        event = RunEvent(T0 + timedelta(seconds=i), "output", None, {"i": i})
        strategy_runs.log(bench.conn, bench.rig.user, "mine", event)

    async def scenario() -> dict[str, Any]:
        async with bench.client(serve) as call:
            return await call("get_strategy_status", {"run_id": "mine"})

    events = asyncio.run(scenario())["data"]["events"]

    assert len(events) == MAX_LISTED_EVENTS
    assert [e["detail"]["i"] for e in events] == list(range(10, MAX_LISTED_EVENTS + 10))


def test_suggest_mode_status_shows_the_latest_suggestion_and_how_to_act(
    serve: Serve, tmp_path: Path
) -> None:
    bench = Bench(tmp_path, by_minute({0: Target(1), 1: Target(-1, Decimal("781.00"))}))
    bench.backtested(bench.rig.user)
    for i in range(2):
        bench.rig.exchange.publish(
            SYMBOL, flat_bar(T0 + timedelta(minutes=i), "780.00"), timedelta(seconds=2)
        )

    async def scenario() -> dict[str, Any]:
        async with bench.client(serve) as call:
            _, result = await preview_and_start(call, mode="suggest")
            await bench.rig.timer.run_until(after_minute(1))
            return await call("get_strategy_status", {"run_id": result["data"]["run_id"]})

    data = asyncio.run(scenario())["data"]

    [suggestion] = data["suggestions"]
    assert suggestion["symbol"] == SYMBOL
    assert suggestion["target"] == {"position": -1, "price": "781.00"}
    assert "preview_order" in data["suggestion_note"]
    assert bench.rig.exchange.order_requests() == []
    assert data["orders_today"] == 0


# ---- 처음부터 끝까지 ----------------------------------------------------------


def test_backtest_preview_start_order_status_and_stop(serve: Serve, tmp_path: Path) -> None:
    """백테스트 → 미리보기 → 켜기(승인 창) → 1분봉 → 자동 주문·체결 → 상태 → 끄기."""
    bench = Bench(tmp_path, by_minute({0: Target(1)}))
    exchange = bench.rig.exchange
    yesterday = T0 - timedelta(days=1)
    for i in range(5):
        exchange.publish(SYMBOL, flat_bar(yesterday + timedelta(minutes=i), "779.00"), timedelta(0))
    exchange.publish(SYMBOL, flat_bar(T0, "780.00"), timedelta(seconds=2))
    backtest: dict[str, Any] = {
        "strategy": {"code": CODE, "params": {}},
        "data": {"streams": {SYMBOL: ["minute"]}, "start": "2026-11-30", "end": "2026-11-30"},
    }
    window = Window("accept")

    async def scenario() -> dict[str, dict[str, Any]]:
        async with bench.client(serve, window=window) as call:
            out = {"backtest": await call("backtest_strategy", backtest)}
            _, out["start"] = await preview_and_start(call)
            run_id = out["start"]["data"]["run_id"]
            await bench.rig.timer.run_until(after_minute(0) + timedelta(seconds=5))
            out["status"] = await call("get_strategy_status", {"run_id": run_id})
            out["stop"] = await call("stop_strategy", {"run_id": run_id})
            out["listed"] = await call("get_strategy_status", {})
            return out

    out = asyncio.run(scenario())

    assert out["backtest"]["ok"] is True, out["backtest"]
    assert out["start"]["ok"] is True, out["start"]
    assert len(window.messages) == 1
    [placed] = exchange.placed
    assert (placed.symbol, placed.side, placed.kind, placed.quantity) == (
        SYMBOL,
        "buy",
        "market",
        1,
    )
    status = out["status"]["data"]
    assert status["state"] == "running"
    assert status["orders_today"] == 1
    assert status["positions"] == {SYMBOL: 1}
    assert status["pnl_today"] is not None
    kinds: list[JsonValue] = [e["kind"] for e in status["events"]]
    assert {"output", "order", "fill"} <= set(kinds)
    order = next(e for e in status["events"] if e["kind"] == "order")
    assert order["detail"]["outcome"] == "accepted"
    assert (out["stop"]["data"]["state"], out["stop"]["data"]["reason"]) == ("stopped", "user")
    [listed] = out["listed"]["data"]["runs"]
    assert (listed["state"], listed["orders_today"], listed["positions"]) == ("stopped", 1, None)
