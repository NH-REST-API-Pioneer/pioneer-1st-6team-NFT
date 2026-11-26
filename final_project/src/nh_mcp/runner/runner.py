"""실행기 전체: 켜기, 끄기, 키 삭제, 서버 시작·종료.

켜기(`start`, W5의 `start_strategy`가 소진한 승인 내용을 넘긴다):
1. 승인 내용을 읽고 코드·모델 해시가 승인받은 것과 같은지 본다.
2. 종목마다 틱·승수·거래 시간. 모르면 켜지 않는다.
3. 계좌를 다 읽고, 실행할 종목에 보유·미체결이 없어야 한다. 전략의 `ctx.positions`는 0에서 시작해
   NH와 맞아야 하기 때문이다. 계좌를 모르면 켜지 않는다.
4. 체결·호가 흐름이면 구독을 먼저 연다(데우는 동안 온 것은 구독에 쌓인다).
5. 데울 데이터: 봉 실행은 NH 봉 가운데 지금까지 끝난 것의 마지막 두 거래일, 틱 실행은 오늘 저장한
   체결·호가(구독을 연 시각 전, 가장 최근 WARMUP_TICKS개)와 지난 두 거래일 일봉. 넣는 순서는
   백테스트와 같다.
6. `starting`으로 남기고 세션을 연다 → 데울 이벤트를 넣는다(출력은 버리고 체결도 없다) → `running`.
   세션이나 출력이 잘못되면 `stopped(sandbox_failed|invalid_output)`이고 켜기를 거절한다.

서버가 뜰 때(`boot`) `starting`·`running`인 실행은 모두 `paused(server_restart)`가 되고 남은 세션을
닫는다. 저절로 다시 켜지 않는다. 내려갈 때(`shutdown`)는 작업만 멈추고 상태는 그대로 둔다.
"""

import contextlib
import hashlib
import logging
import secrets
import sqlite3
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Literal

from nh_mcp.domain import (
    BarEvent,
    Candle,
    JsonValue,
    MarketEvent,
    SessionFailed,
    SessionJob,
    Streams,
    UserId,
)
from nh_mcp.runner import machine
from nh_mcp.runner.run import Deps, RunTask, Started, Stop, feed_one
from nh_mcp.runner.timing import Timer, minute_floor, today
from nh_mcp.sandbox.client import SandboxBusy, SandboxDown, SessionGone, SessionHandle
from nh_mcp.services.backtest import code_hash, feed_order
from nh_mcp.services.queries import BrokerRejected, KeyMissing, Unavailable, UnknownSymbol
from nh_mcp.services.runs import (
    Approved,
    Standing,
    StartRefused,
    SymbolInfo,
    parse_approval,
    with_days,
)
from nh_mcp.store import strategy_runs
from nh_mcp.store.strategy_runs import RunEvent, RunRow, StopReason, StrategyRun
from nh_mcp.store.ticks import Sub
from nh_mcp.strategies.backtest import decision_time

log = logging.getLogger(__name__)

# 데우는 데 쓰는 거래일 수(사람 결정 2026-11-25: 최근 이틀).
WARMUP_DAYS = 2
# 두 거래일을 찾으려고 거슬러 받는 기간(연휴를 넘는다).
WARMUP_LOOKBACK = timedelta(days=10)
WARMUP_MINUTES = 2000
WARMUP_DAY_BARS = 20
# 틱 실행 데우기의 체결·호가 수. 한 이벤트 왕복 약 1~2ms면 20~40초이고, 그동안 온 실시간은 구독에
# 쌓인다(구독 상한 1000개, 체결·호가 합쳐 초당 10개 남짓이면 100초쯤).
WARMUP_TICKS = 20_000

NOT_APPROVED = "승인 내용을 읽을 수 없습니다. preview_strategy부터 다시 해 주세요."
NOT_THE_CODE = "승인받은 코드와 다릅니다(해시가 다름). preview_strategy부터 다시 해 주세요."
NOT_THE_MODEL = "승인받은 모델 파일과 다릅니다(해시가 다름). preview_strategy부터 다시 해 주세요."


def _refuse(rule: str, message: str) -> StartRefused:
    return StartRefused(rule, message)


def _tick_subs(streams: Streams) -> frozenset[Sub]:
    subs: set[Sub] = set()
    for symbol, names in streams.items():
        if "tick" in names:
            subs.add(Sub(symbol, "tick"))
        if "book" in names:
            subs.add(Sub(symbol, "book"))
    return frozenset(subs)


class Runner:
    def __init__(self, deps: Deps) -> None:
        self._deps = deps
        self._conn: sqlite3.Connection = deps.conn
        self._timer: Timer = deps.timer
        self._tasks: dict[str, RunTask] = {}

    # ---- 서버 수명 ------------------------------------------------------------

    @asynccontextmanager
    async def running(self) -> AsyncGenerator["Runner"]:
        await self.boot()
        try:
            yield self
        finally:
            await self.shutdown()

    async def boot(self) -> None:
        """돌던 실행을 일시정지로 바꾸고 남은 세션을 닫는다. 아무것도 다시 켜지 않는다."""
        now = self._timer.now()
        for row in strategy_runs.to_clean_up(self._conn):
            run = row.run
            if row.state in ("starting", "running"):
                machine.advance(self._conn, run, "paused", "server_restart", now)
                self._log(run, {"state": "paused", "reason": "server_restart"})
            await self._close_session(row)

    async def shutdown(self) -> None:
        """작업만 멈춘다. 상태는 그대로 두고 다음 `boot`가 일시정지로 바꾼다."""
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            await task.cancel()

    # ---- 도구가 부르는 것 ------------------------------------------------------

    async def start(self, user_id: UserId, approval: str, code: str, model: bytes | None) -> str:
        """승인받은 전략을 켜고 실행 식별자를 돌려준다. 켜지 않으면 StartRefused."""
        approved = self._approved(approval, code, model)
        now = self._timer.now()
        if now >= approved.scope.expires_at:
            raise _refuse("expired", "승인이 이미 만료됐습니다.")
        infos = await self._infos(user_id, approved.streams)
        await self._flat(user_id, approved.streams)
        run_id = secrets.token_hex(8)
        subs = _tick_subs(approved.streams)
        live = await self._deps.feeds.open(user_id, run_id, subs) if subs else None
        try:
            warm, last = await self._warm(user_id, approved.streams, now)
            run = StrategyRun(
                run_id,
                user_id,
                "domestic",
                approved.streams,
                approved.code_hash,
                approved.params,
                approved.model_hash,
                approved.scope,
                now,
            )
            handle = await self._begin(run, code, model, infos, warm)
        except BaseException:
            if live is not None:
                await self._deps.feeds.close(live)
            raise
        task = RunTask(self._deps, Started(run, handle, infos, live, last), self._forget)
        self._tasks[run_id] = task
        task.start()
        return run_id

    async def stop(self, user_id: UserId, run_id: str) -> bool:
        """사용자가 끈다. 돌던 실행은 멈출 때까지 기다리고, 일시정지면 멈춤으로 바꾼다."""
        task = self._tasks.get(run_id)
        if task is not None and task.user_id == user_id:
            await task.halt("user")
            return True
        row = strategy_runs.get(self._conn, user_id, run_id)
        if row is None or row.state != "paused":
            return False
        machine.advance(self._conn, row.run, "stopped", "user", self._timer.now())
        self._log(row.run, {"reason": "user"})
        await self._close_session(row)
        return True

    async def key_removed(self, user_id: UserId) -> None:
        """키를 지운다. 그 사용자의 돌던 실행은 멈추고(걸린 주문을 아직 키로 취소), 일시정지는
        멈춤으로 바꾼다. 키를 지우기 전에 불러야 한다."""
        for task in [t for t in self._tasks.values() if t.user_id == user_id]:
            await task.halt("key_removed")
        for row in strategy_runs.list_for(self._conn, user_id):
            if row.state == "paused":
                run = row.run
                machine.advance(self._conn, run, "stopped", "key_removed", self._timer.now())
                self._log(run, {"reason": "key_removed"})

    def active(self, user_id: UserId) -> list[str]:
        """이 사용자의 돌고 있는 실행."""
        return sorted(run_id for run_id, t in self._tasks.items() if t.user_id == user_id)

    def standing(self, user_id: UserId, run_id: str) -> Standing | None:
        """이 사용자의 돌고 있는 실행이면 체결로 센 포지션과 오늘 손익, 아니면 None."""
        task = self._tasks.get(run_id)
        return task.standing() if task is not None and task.user_id == user_id else None

    # ---- 켜기 -----------------------------------------------------------------

    def _approved(self, approval: str, code: str, model: bytes | None) -> Approved:
        approved = parse_approval(approval)
        if approved is None:
            raise _refuse("invalid_approval", NOT_APPROVED)
        if code_hash(code) != approved.code_hash:
            raise _refuse("not_approved_code", NOT_THE_CODE)
        hashed = None if model is None else hashlib.sha256(model).hexdigest()
        if hashed != approved.model_hash:
            raise _refuse("not_approved_model", NOT_THE_MODEL)
        return approved

    async def _infos(self, user_id: UserId, streams: Streams) -> dict[str, SymbolInfo]:
        infos: dict[str, SymbolInfo] = {}
        for symbol in sorted(streams):
            try:
                info = await self._deps.market.symbol(user_id, symbol)
            except UnknownSymbol:
                raise _refuse("unknown_symbol", f"{symbol}: 종목 마스터에 없습니다.") from None
            except Unavailable:
                raise _refuse("unavailable", "종목 정보를 받지 못했습니다.") from None
            if info is None:
                raise _refuse("unknown_terms", f"{symbol}의 틱 크기·거래 시간을 알 수 없습니다.")
            infos[symbol] = info
        return infos

    async def _flat(self, user_id: UserId, streams: Streams) -> None:
        """실행할 종목에 보유·미체결이 없어야 한다. 계좌를 다 읽지 못하면 켜지 않는다."""
        try:
            account = await self._deps.market.account(user_id)
        except KeyMissing:
            raise _refuse("key_missing", "국내 선물 키가 없습니다.") from None
        except (Unavailable, BrokerRejected):
            raise _refuse("account_unknown", "계좌를 읽지 못해 켜지 않습니다.") from None
        if not account.complete:
            raise _refuse("account_unknown", "계좌를 다 읽지 못해 켜지 않습니다.")
        held = sorted({p.symbol for p in account.positions if p.symbol in streams and p.quantity})
        waiting = sorted(
            {o.symbol for o in account.open_orders if o.symbol in streams and o.remaining_quantity}
        )
        if held or waiting:
            names = ", ".join(sorted({*held, *waiting}))
            raise _refuse(
                "account_not_flat",
                f"실행할 종목에 보유나 미체결 주문이 있습니다({names}). 전략은 포지션 0에서 "
                "시작해야 "
                "NH와 맞습니다. 정리한 뒤 다시 켜 주세요.",
            )

    async def _warm(
        self, user_id: UserId, streams: Streams, now: datetime
    ) -> tuple[list[MarketEvent], dict[str, datetime]]:
        """데울 이벤트(넣는 순서)와 종목마다 이미 넣은 마지막 1분봉의 시작 시각."""
        try:
            if any(names & {"tick", "book"} for names in streams.values()):
                return await self._warm_ticks(user_id, streams, now), {}
            return await self._warm_bars(user_id, streams, now)
        except KeyMissing:
            raise _refuse("key_missing", "국내 선물 키가 없습니다.") from None
        except (Unavailable, BrokerRejected, UnknownSymbol):
            raise _refuse("unavailable", "지난 데이터를 받지 못해 켜지 않습니다.") from None

    async def _bars(
        self, user_id: UserId, symbol: str, interval: Literal["minute", "day"], now: datetime
    ) -> list[BarEvent]:
        """지금까지 끝난 봉(판단 시각이 지금 이전)."""
        period = (today(now - WARMUP_LOOKBACK), today(now))
        limit = WARMUP_MINUTES if interval == "minute" else WARMUP_DAY_BARS
        candles: list[Candle] = await self._deps.market.bars(
            user_id, symbol, interval, period, limit
        )
        events = [BarEvent(symbol, interval, c) for c in candles]
        return [e for e in events if decision_time(e) <= now]

    async def _warm_bars(
        self, user_id: UserId, streams: Streams, now: datetime
    ) -> tuple[list[MarketEvent], dict[str, datetime]]:
        fetched: list[BarEvent] = []
        for symbol, names in sorted(streams.items()):
            for interval in ("day", "minute"):
                if interval in names:
                    fetched += await self._bars(user_id, symbol, interval, now)
        dates = sorted({today(e.bar.at) for e in fetched})[-WARMUP_DAYS:]
        events = sorted((e for e in fetched if today(e.bar.at) in dates), key=feed_order)
        # 아직 넣지 않은 첫 1분봉: 넣은 것 다음, 넣은 것이 없으면 지금 진행 중인 분부터.
        floor = minute_floor(now) - timedelta(minutes=1)
        last = {
            symbol: max(
                (e.bar.at for e in events if e.symbol == symbol and e.interval == "minute"),
                default=floor,
            )
            for symbol, names in streams.items()
            if "minute" in names
        }
        return list[MarketEvent](events), last

    async def _warm_ticks(
        self, user_id: UserId, streams: Streams, now: datetime
    ) -> list[MarketEvent]:
        days: dict[str, list[Candle]] = {}
        for symbol, names in sorted(streams.items()):
            if "day" in names:
                bars = [
                    e
                    for e in await self._bars(user_id, symbol, "day", now)
                    if today(e.bar.at) < today(now)
                ]
                kept = sorted({today(e.bar.at) for e in bars})[-WARMUP_DAYS:]
                days[symbol] = [e.bar for e in bars if today(e.bar.at) in kept]
        before = now.replace(microsecond=0)
        recorded = await self._deps.feeds.recorded(
            user_id, _tick_subs(streams), today(now), before, WARMUP_TICKS
        )
        return with_days(recorded, days)

    async def _begin(
        self,
        run: StrategyRun,
        code: str,
        model: bytes | None,
        infos: Mapping[str, SymbolInfo],
        warm: list[MarketEvent],
    ) -> SessionHandle:
        """`starting`으로 남기고 세션을 열어 데운 뒤 `running`으로. 실패하면 멈추고 StartRefused."""
        strategy_runs.insert(self._conn, run)
        job = SessionJob(
            code=code,
            params=run.params,
            streams=run.streams,
            max_position=run.scope.max_position,
            terms={s: info.terms for s, info in infos.items()},
            expires_at=run.scope.expires_at,
            model=model,
        )
        handle: SessionHandle | None = None
        try:
            opened = await self._deps.sessions.open_session(job)
            if isinstance(opened, SessionFailed):
                raise Stop(
                    "invalid_output" if opened.reason == "invalid_output" else "sandbox_failed",
                    f"{opened.reason}: 전략 코드가 낸 내용: {opened.detail}",
                )
            handle = opened
            strategy_runs.set_session(self._conn, run.user_id, run.run_id, handle.session_id)
            for event in warm:
                await feed_one(self._deps.sessions, handle, event)
            machine.advance(self._conn, run, "running", None, self._timer.now())
        except SandboxBusy:
            await self._abort(run, handle, "sandbox_failed", "실행기 자리가 없습니다.")
            raise _refuse(
                "sandbox_busy", "서버의 전략 실행 자리가 모두 찼습니다. 잠시 뒤 다시 켜 주세요."
            ) from None
        except SandboxDown:
            await self._abort(run, handle, "sandbox_failed", "실행기에 닿지 못했습니다.")
            raise _refuse("sandbox_unavailable", "전략 실행기에 닿지 못했습니다.") from None
        except Stop as stop:
            await self._abort(run, handle, stop.reason, stop.detail)
            raise _refuse(
                stop.reason, f"전략을 켜다 멈췄습니다({stop.reason}). {stop.detail}"
            ) from None
        except BaseException:
            await self._abort(run, handle, "sandbox_failed", "켜는 중에 멈췄습니다.")
            raise
        return handle

    async def _abort(
        self, run: StrategyRun, handle: SessionHandle | None, reason: StopReason, detail: str
    ) -> None:
        with contextlib.suppress(machine.IllegalTransition):
            machine.advance(self._conn, run, "stopped", reason, self._timer.now())
        self._log(run, {"reason": reason, "detail": detail[:500]})
        if handle is not None:
            try:
                await self._deps.sessions.close(handle)
                strategy_runs.set_session(self._conn, run.user_id, run.run_id, None)
            except (SandboxDown, SessionGone):
                log.warning("전략 %s의 세션을 닫지 못했습니다.", run.run_id)

    # ---- 그 밖 ---------------------------------------------------------------

    async def _close_session(self, row: RunRow) -> None:
        """기록에 남은 세션을 닫는다. 실행기가 이미 지웠으면 닫힌 것이다."""
        if row.session_id is None:
            return
        run = row.run
        # 닫기에는 세션 식별자만 쓴다. 출력 검사용 계약은 비워 둔다.
        placeholder = SessionJob(
            "", run.params, run.streams, run.scope.max_position, {}, run.scope.expires_at
        )
        try:
            await self._deps.sessions.close(SessionHandle(row.session_id, placeholder))
        except (SandboxDown, SessionGone):
            log.warning(
                "전략 %s의 남은 세션을 닫지 못했습니다. 다음 시작 때 다시 닫습니다.", run.run_id
            )
            return
        strategy_runs.set_session(self._conn, run.user_id, run.run_id, None)

    def _log(self, run: StrategyRun, detail: dict[str, JsonValue]) -> None:
        event = RunEvent(self._timer.now(), "stop", None, detail)
        strategy_runs.log(self._conn, run.user_id, run.run_id, event)

    def _forget(self, run_id: str) -> None:
        self._tasks.pop(run_id, None)
