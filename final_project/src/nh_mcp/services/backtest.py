"""백테스트 유스케이스: 봉과 체결 조건을 모아 격리 실행기에 보내고, 신호로 성과를 계산해 남긴다.

전략 코드는 이 프로세스에서 실행하거나 파싱하지 않는다. 해시하고 저장하고 실행기로 넘기기만 한다.
순서: 입력 크기 → 종목(국내 선물만) → 사용자별 자리 → 봉 → 봉 수 → 틱 → 실행기 → 성과 → 기록.
봉을 틱보다 먼저 받는다. 틱 표는 키가 없을 때 None을 돌려주므로, 먼저 보면 키 없음이
"틱 모름"으로 보이기 때문이다(봉 조회가 KeyMissing을 낸다).
"""

import asyncio
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from nh_mcp.domain import (
    KST,
    Candle,
    Clock,
    JsonValue,
    SandboxFailure,
    SandboxJob,
    SandboxResult,
    UserId,
)
from nh_mcp.markets.domestic import Interval
from nh_mcp.markets.domestic_products import DomesticProducts
from nh_mcp.sandbox.client import SandboxBusy, SandboxClient, SandboxDown
from nh_mcp.services.queries import QueryService
from nh_mcp.store import backtests
from nh_mcp.store.backtests import NewRun, RunKey
from nh_mcp.strategies.backtest import MIN_BARS, FillTerms, OpenPosition, Report, simulate

MAX_BARS = 2000
MAX_CODE_BYTES = 65_536
MAX_PARAMS_BYTES = 4096
# 한 사용자가 동시에 돌릴 수 있는 백테스트 수. 실행기 전체 한도(3개)는 실행기가 본다.
PER_USER = 2
MAX_QUANTITY = 100
# 봉이 2개 미만이라 실행기를 부르지 않은 기록의 상태.
DATA_SHORT = "data_short"
# 첫 봉이 요청 시작일보다 이만큼 넘게 늦어야 경고한다. 주말·연휴로 늦는 것은 경고하지 않는다.
LATE_START_GRACE = timedelta(days=5)

NOT_FUTURE = (
    "백테스트는 국내 선물 종목만 됩니다. search_symbol 결과에서 kind가 future인 종목을 고르세요."
)
NO_TICK = "이 종목의 틱 크기를 확인하지 못해 백테스트할 수 없습니다."

# 자원 제한 사유 → 사용자 말. 값은 docs/safety.md의 격리 제한과 같다.
_LIMIT_HIT: dict[SandboxFailure, str] = {
    "cpu": "전략 코드가 CPU 시간 제한(60초)을 넘었습니다.",
    "memory": "전략 코드가 메모리 제한(512MB)을 넘었습니다.",
    "processes": "전략 코드가 프로세스·스레드 수 제한을 넘었습니다.",
    "output": "전략 코드가 출력 제한(1MB)을 넘었습니다.",
    "timeout": "전략 실행이 시간 제한(70초)을 넘었습니다.",
}


class StrategyInvalid(Exception):
    """입력이나 코드·신호 형식이 맞지 않거나, 데이터·종목 정보가 모자라 돌릴 수 없다."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class StrategyFailed(Exception):
    """코드가 예외를 냈거나 자원 제한을 넘었다. 결과는 없다."""

    def __init__(self, reason: SandboxFailure, message: str) -> None:
        super().__init__(message)
        self.reason: SandboxFailure = reason
        self.message = message


class RunnerUnavailable(Exception):
    """실행기가 바쁘거나 답하지 않거나, 이 사용자가 이미 동시 한도만큼 돌리고 있다."""


@dataclass(frozen=True)
class BacktestRequest:
    symbol: str
    code: str
    params: dict[str, JsonValue]
    interval: Interval
    period: tuple[date, date]
    quantity: int
    fee_per_fill: Decimal


@dataclass(frozen=True)
class BacktestOutcome:
    report: Report
    code_hash: str
    bars: int
    first_bar_at: datetime
    last_bar_at: datetime
    truncated: bool
    warnings: list[str]


def code_hash(code: str) -> str:
    """코드 UTF-8 바이트의 SHA-256. 공백 하나도 정규화하지 않는다."""
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _canonical(value: JsonValue) -> str:
    """키를 정렬하고 공백 없이 쓴 JSON. 파라미터·설정·결과를 같은 모양으로 저장한다."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def params_key(params: dict[str, JsonValue]) -> str:
    """파라미터를 비교·저장하는 문자열. `preview_strategy`도 같은 함수로 비교한다."""
    return _canonical(params)


def check_strategy(code: str, params: dict[str, JsonValue]) -> str | None:
    """코드와 파라미터의 크기가 맞으면 None, 아니면 알려 줄 문장."""
    if not code.strip():
        return "strategy.code에 on_bar(bar, history, params) 함수를 담은 코드를 넣어 주세요."
    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        return f"strategy.code는 UTF-8로 {MAX_CODE_BYTES:,}바이트까지입니다."
    try:
        size = len(params_key(params).encode("utf-8"))
    except RecursionError:
        size = MAX_PARAMS_BYTES + 1  # 너무 깊게 중첩돼 직렬화할 수 없다.
    if size > MAX_PARAMS_BYTES:
        return f"strategy.params는 JSON으로 {MAX_PARAMS_BYTES:,}바이트까지입니다."
    return None


def check_settings(quantity: int, fee_per_fill: Decimal) -> str | None:
    """체결 조건이 맞으면 None. simulate는 값을 검사하지 않으므로 여기서 막는다."""
    if type(quantity) is not int or not 1 <= quantity <= MAX_QUANTITY:
        return f"settings.quantity는 1 이상 {MAX_QUANTITY} 이하의 정수(계약 수)여야 합니다."
    if not fee_per_fill.is_finite() or fee_per_fill < 0:
        return 'settings.fee_per_fill은 0 이상의 금액을 숫자만으로 써 주세요. 예: "1500"'
    return None


def _stamp(at: datetime) -> str:
    return at.astimezone(KST).isoformat(timespec="seconds")


def _position(p: OpenPosition | None) -> dict[str, JsonValue] | None:
    if p is None:
        return None
    return {
        "side": p.side,
        "quantity": p.quantity,
        "entry_at": _stamp(p.entry_at),
        "entry_price": str(p.entry_price),
        "mark_price": str(p.mark_price),
        "unrealized_pnl": str(p.unrealized_pnl),
        "entry_fee": str(p.entry_fee),
    }


def report_data(report: Report) -> dict[str, JsonValue]:
    """성과 지표. 금액은 문자열이다. 도구 응답과 저장하는 결과 JSON이 같이 쓴다."""
    return {
        "trades": len(report.trades),
        "fills": report.fills,
        "win_rate": None if report.win_rate is None else str(report.win_rate),
        "net_pnl": str(report.net_pnl),
        "max_drawdown": str(report.max_drawdown),
        "fees": str(report.fees),
        "open_position": _position(report.open_position),
        "last_signal_ignored": report.last_signal_ignored,
    }


def _bar_note(result: SandboxResult) -> str:
    return "" if result.bar_index is None else f" ({result.bar_index + 1}번째 봉에서)"


def _strategy_said(detail: str) -> str:
    """전략 코드가 낸 내용을 그렇다고 표시해 붙인다. 없으면 붙이지 않는다."""
    return f" 전략 코드가 낸 내용: {detail}" if detail else ""


def _failure(result: SandboxResult) -> Exception:
    """실패한 실행 결과를 서비스 예외로. detail은 전략 코드가 낸 것이라 그렇다고 적는다.

    invalid_code·invalid_signal의 detail도 전략 코드가 만들 수 있다(코드 예외 문구, 돌려준 값,
    프로토콜 출구에 쓴 거짓 줄). 서버가 쓴 문장에는 표시를 붙이지 않는다.
    """
    note = _bar_note(result)
    match result.status:
        case "ok":
            raise ValueError("성공한 결과는 실패로 바꿀 수 없다")
        case "invalid_code":
            return StrategyInvalid(
                "전략 코드 형식이 맞지 않습니다. on_bar(bar, history, params) 함수가 있어야 "
                "합니다." + _strategy_said(result.detail)
            )
        case "invalid_signal":
            return StrategyInvalid(
                f'전략 코드가 "buy"·"sell"·"exit"·"none" 밖의 값을 돌려줬습니다{note}.'
                + _strategy_said(result.detail)
            )
        case "exception":
            return StrategyFailed("exception", f"전략 코드가 낸 오류: {result.detail}{note}")
        case "cpu" | "memory" | "processes" | "output" | "timeout" as reason:
            return StrategyFailed(reason, _LIMIT_HIT[reason] + note)


@dataclass(frozen=True)
class _Run:
    """실행 한 건의 기록 재료."""

    user_id: UserId
    request: BacktestRequest
    key: RunKey
    bars: list[Candle]


class BacktestService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        clock: Clock,
        queries: QueryService,
        products: DomesticProducts,
        sandbox: SandboxClient,
    ) -> None:
        self._conn = conn
        self._clock = clock
        self._queries = queries
        self._products = products
        self._sandbox = sandbox
        self._running: dict[UserId, int] = {}
        # 호출이 취소돼도 도는 실행. 끝날 때까지 참조를 붙들어 가비지 수집을 막는다.
        self._tasks: set[asyncio.Task[Report]] = set()

    def running(self, user_id: UserId) -> int:
        """이 사용자가 지금 돌리고 있는 백테스트 수."""
        return self._running.get(user_id, 0)

    async def run(self, user_id: UserId, request: BacktestRequest) -> BacktestOutcome:
        problem = check_strategy(request.code, request.params) or check_settings(
            request.quantity, request.fee_per_fill
        )
        if problem is not None:
            raise StrategyInvalid(problem)
        contract = await self._queries.contract(request.symbol)
        if contract.kind != "future":
            raise StrategyInvalid(NOT_FUTURE)
        self._take(user_id)
        handed_off = False
        try:
            bars = await self._queries.candles(
                user_id, request.symbol, request.interval, request.period, MAX_BARS
            )
            key = RunKey(
                "domestic",
                request.symbol,
                request.interval,
                code_hash(request.code),
                params_key(request.params),
            )
            run = _Run(user_id, request, key, bars)
            if len(bars) < MIN_BARS:
                self._save(run, DATA_SHORT, None)
                raise StrategyInvalid(
                    f"기간 안 봉이 {len(bars)}개라 데이터가 모자랍니다. "
                    f"{MIN_BARS}개 이상 있게 기간을 넓혀 주세요."
                )
            rules = await self._products.rules_for(user_id, contract)
            if rules is None:
                raise StrategyInvalid(NO_TICK)
            terms = FillTerms(
                rules.tick_size, contract.multiplier, request.quantity, request.fee_per_fill
            )
            # 실행기 호출부터 기록까지는 따로 띄운다. 도구 호출이 취소돼도 끝까지 돌고 남긴다.
            task = asyncio.create_task(self._execute(run, terms))
            handed_off = True
        finally:
            if not handed_off:
                self._release(user_id)
        self._tasks.add(task)
        task.add_done_callback(self._settle)
        report = await asyncio.shield(task)
        return self._outcome(run, report)

    def _take(self, user_id: UserId) -> None:
        if self.running(user_id) >= PER_USER:
            raise RunnerUnavailable()
        self._running[user_id] = self.running(user_id) + 1

    def _release(self, user_id: UserId) -> None:
        left = self.running(user_id) - 1
        if left > 0:
            self._running[user_id] = left
        else:
            self._running.pop(user_id, None)

    def _settle(self, task: asyncio.Task[Report]) -> None:
        """끝난 실행을 놓아준다. 아무도 기다리지 않던 실행의 예외도 여기서 거둔다."""
        self._tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def _execute(self, run: _Run, terms: FillTerms) -> Report:
        try:
            job = SandboxJob(run.request.code, run.request.params, run.bars)
            try:
                result = await self._sandbox.run(job)
            except (SandboxBusy, SandboxDown):
                # 코드가 돌지 않았으므로 기록하지 않는다.
                raise RunnerUnavailable() from None
            if result.status != "ok":
                self._save(run, result.status, None)
                raise _failure(result)
            report = simulate(run.bars, result.signals, terms)
            self._save(run, backtests.SUCCESS, _canonical(report_data(report)))
            return report
        finally:
            self._release(run.user_id)

    def _save(self, run: _Run, status: str, result: str | None) -> None:
        request = run.request
        settings: dict[str, JsonValue] = {
            "quantity": request.quantity,
            "fee_per_fill": str(request.fee_per_fill),
        }
        backtests.record(
            self._conn,
            NewRun(
                user_id=run.user_id,
                key=run.key,
                code=request.code,
                settings=_canonical(settings),
                requested=request.period,
                first_bar_at=run.bars[0].at if run.bars else None,
                last_bar_at=run.bars[-1].at if run.bars else None,
                bar_count=len(run.bars),
                status=status,
                result=result,
            ),
            self._clock(),
        )

    def _outcome(self, run: _Run, report: Report) -> BacktestOutcome:
        first, last = run.bars[0].at, run.bars[-1].at
        truncated = len(run.bars) == MAX_BARS
        warnings: list[str] = []
        if truncated:
            warnings.append(f"기간 안 봉이 많아 가장 최근 {MAX_BARS}개만 썼습니다.")
        elif first.astimezone(KST).date() > run.request.period[0] + LATE_START_GRACE:
            day = first.astimezone(KST).date().isoformat()
            warnings.append(f"요청한 시작일보다 늦은 날부터 데이터가 있습니다(첫 봉 {day}).")
        return BacktestOutcome(
            report=report,
            code_hash=run.key.code_hash,
            bars=len(run.bars),
            first_bar_at=first,
            last_bar_at=last,
            truncated=truncated,
            warnings=warnings,
        )
