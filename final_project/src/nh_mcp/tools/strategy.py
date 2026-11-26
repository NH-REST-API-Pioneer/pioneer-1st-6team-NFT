"""자동 실행 도구: preview_strategy, start_strategy, stop_strategy, get_strategy_status.

도구는 입력 검사와 응답 포장만 한다. 한도 비교, 백테스트 확인, 확인 토큰, 켜기·끄기는
`services/strategy.py`가 한다. 속도 제한은 서비스를 부르기 전에 본다.
`ORDERS_ENABLED=false`면 이 도구들은 등록되지 않는다(주문 도구와 같다).

네 도구는 "자동 실행" 묶음이다(`docs/tools.md`). 사용자가 등록 화면에서 켜지 않았으면 도구 목록에서
뺀다(`AutoRunGate`). 숨기기만으로 끝내지 않고 preview_strategy·start_strategy는 서비스가 다시 보고
거절한다. stop_strategy·get_strategy_status는 숨기기만 하고 부르면 답한다. 묶음을 끄면 전략이 모두
꺼지므로 둘은 남은 기록을 보거나 끄는 일뿐이고, 위험을 줄이는 끄기는 막지 않는다.
"""

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import TypedDict

import mcp_types
from fastmcp import Context, FastMCP
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import Tool

from nh_mcp import envelope
from nh_mcp.auth.access import NotLoggedIn
from nh_mcp.domain import KST, JsonValue, OnLoss, StrategyMode, StrategyScope
from nh_mcp.ops.rate_limit import ORDER_BUCKET, PREVIEW_BUCKET, RateLimiter
from nh_mcp.services.backtest import streams_data
from nh_mcp.services.orders import RiskBlocked
from nh_mcp.services.runs import StartRefused
from nh_mcp.services.strategy import (
    AUTO_RUN_OFF,
    MAX_DAILY_ORDERS,
    SERVER_FULL,
    RunDetail,
    RunMissing,
    RunStarting,
    RunView,
    StartDeclined,
    StrategyRequest,
    StrategyService,
    StrategyTokenInvalid,
)
from nh_mcp.store.strategy_runs import RunEvent, RunRow, StopReason
from nh_mcp.tools.approval import ask
from nh_mcp.tools.backtest import AMOUNT, StrategyCode, parse_strategy, parse_streams
from nh_mcp.tools.context import DOMESTIC_ONLY, ToolContext, Whole, whole
from nh_mcp.tools.order import MAX_QUANTITY

# "자동 실행" 묶음의 도구에 붙이는 태그.
AUTO_RUN_TAG = "auto_run"

_ON_LOSS: dict[str, OnLoss] = {"liquidate": "liquidate", "hold": "hold"}
_MODES: dict[str, StrategyMode] = {"auto": "auto", "suggest": "suggest"}

# 켜기를 거절한 사유(rule) → (오류 코드, 다시 해 볼 만한가). 없는 사유는 RISK_BLOCKED.
_REFUSALS: dict[str, tuple[envelope.ErrorCode, bool]] = {
    "sandbox_busy": ("TEMPORARILY_UNAVAILABLE", True),
    "sandbox_unavailable": ("TEMPORARILY_UNAVAILABLE", True),
    "unavailable": ("TEMPORARILY_UNAVAILABLE", True),
    SERVER_FULL: ("TEMPORARILY_UNAVAILABLE", True),
    "key_missing": ("KEY_NOT_REGISTERED", False),
    "sandbox_failed": ("STRATEGY_FAILED", False),
    "invalid_output": ("STRATEGY_FAILED", False),
}
TOKEN_INVALID = (
    "확인 토큰이 없거나 만료됐거나 이미 썼습니다. preview_strategy부터 다시 하세요. 토큰을 만들어 "
    "내지 않습니다."
)
DECLINED = "사용자가 승인하지 않아 전략을 켜지 않았습니다. 이 확인 토큰은 더 쓸 수 없습니다."
NO_RUN = "그 run_id의 전략이 없습니다. get_strategy_status로 내 전략의 run_id를 확인하세요."
STILL_STARTING = "켜는 중이라 아직 끌 수 없습니다. 몇 초 뒤 stop_strategy를 다시 불러 주세요."
NEED_RUN_ID = (
    "끌 전략의 run_id를 넣어 주세요(get_strategy_status로 찾습니다). 한 번에 모두 끄는 일은 "
    "없습니다."
)
NO_NOTICE = (
    "서버는 신호·주문·체결·멈춤을 먼저 알리지 못합니다. 사용자가 물으면 get_strategy_status로 "
    "확인합니다."
)
SUGGESTION_NOTE = (
    "제안은 전략이 낸 목표 포지션입니다(롱 +, 숏 -, 0은 정리, price가 없으면 시장가). 제안 모드는 "
    "주문하지 않습니다. 사용자에게 보여 주고 내자고 하면 get_account로 지금 포지션을 보고 차이만큼 "
    "preview_order → place_order로 냅니다."
)
AFTER_STOP = (
    "멈춘 전략의 보유 포지션은 그대로 남을 수 있습니다. get_account로 확인하세요. 다시 켜려면 "
    "preview_strategy → start_strategy로 다시 승인받습니다."
)

# 문구가 늘 같은 예외 → (오류 코드, 문구, 다시 해 볼 만한가).
_FIXED: dict[type[Exception], tuple[envelope.ErrorCode, str, bool]] = {
    StrategyTokenInvalid: ("CONFIRM_EXPIRED", TOKEN_INVALID, False),
    StartDeclined: ("CONFIRM_EXPIRED", DECLINED, False),
    RunMissing: ("INVALID_INPUT", NO_RUN, False),
    RunStarting: ("TEMPORARILY_UNAVAILABLE", STILL_STARTING, True),
}

_STATES = {
    "starting": "켜는 중입니다(지난 데이터를 넣어 전략 상태를 채우는 중). 잠시 뒤 다시 보세요.",
    "running": "서버에서 돌고 있습니다.",
}
_REASONS: dict[StopReason, str] = {
    "user": "사용자가 껐습니다(stop_strategy 또는 등록 화면의 자동 실행 끔).",
    "expired": "승인의 만료 시각이 지나 멈췄습니다.",
    "daily_orders": "하루 주문 횟수를 다 써서 멈췄습니다.",
    "daily_loss": "이 전략의 오늘 손익이 손실 한도에 닿아 멈췄습니다(liquidate면 정리 주문을 냄).",
    "stale_feed": "실시간 시세가 끊겨 멈췄습니다(오래된 가격으로 판단하지 않습니다).",
    "rejections": "NH가 주문을 연달아 거부해 멈췄습니다(events의 order 기록에 msg_cd).",
    "sandbox_failed": "전략 코드나 격리 환경이 실패해 멈췄습니다(events의 stop 기록에 사유).",
    "invalid_output": "전략 코드가 계약에 맞지 않는 출력을 내 멈췄습니다(events의 stop 기록).",
    "key_removed": "NH 키를 지워 멈췄습니다.",
    "unknown_order": (
        "결과를 모르는 주문이 생겨 멈췄습니다. 다시 주문하지 말고 get_orders로 확인하세요."
    ),
    "server_restart": (
        "서버가 다시 시작돼 일시정지했습니다. 저절로 다시 켜지지 않습니다. "
        "get_account·get_orders로 그 종목의 보유와 미체결을 NH와 맞춰 보고(다시 켜려면 그 종목이 "
        "비어 있어야 합니다), preview_strategy → start_strategy로 다시 승인받아 켭니다. 이 실행은 "
        "stop_strategy로 끕니다."
    ),
}


class StrategyDraft(StrategyCode):
    """백테스트한 전략 코드와 파라미터, 모델 파일, 흐름(backtest_strategy의 data.streams와 같은
    값)."""

    streams: dict[str, list[str]]


class ScopeDraft(TypedDict):
    """승인 범위. daily_loss는 숫자만 쓴 문자열, expires_at은 시간대가 있는 ISO 8601."""

    quantity: Whole
    max_position: Whole
    daily_orders: Whole
    daily_loss: str
    expires_at: str
    on_loss: str
    mode: str


def _expiry(raw: str) -> datetime | str:
    """시간대가 있는 시각, 또는 잘못됐다고 알려 줄 문장."""
    try:
        parsed = datetime.fromisoformat(raw.strip())
    except ValueError:
        parsed = None
    if parsed is None or parsed.utcoffset() is None:
        return (
            'scope.expires_at은 시간대가 있는 시각으로 써 주세요. 예: "2026-12-03T15:45:00+09:00"'
        )
    return parsed


def _counts(draft: ScopeDraft) -> tuple[int, int, int] | str:
    """(1회 수량, 최대 보유, 하루 주문 수), 또는 잘못됐다고 알려 줄 문장."""
    quantity = whole(draft["quantity"], 1, MAX_QUANTITY)
    if quantity is None:
        return f"scope.quantity는 1 이상 {MAX_QUANTITY} 이하의 정수(계약 수)여야 합니다."
    position = whole(draft["max_position"], 1, MAX_QUANTITY)
    if position is None:
        return f"scope.max_position은 1 이상 {MAX_QUANTITY} 이하의 정수(계약 수)여야 합니다."
    orders = whole(draft["daily_orders"], 1, MAX_DAILY_ORDERS)
    if orders is None:
        return f"scope.daily_orders는 1 이상 {MAX_DAILY_ORDERS} 이하의 정수여야 합니다."
    return quantity, position, orders


def _scope(draft: ScopeDraft) -> StrategyScope | str:
    """승인 범위, 또는 입력이 잘못됐다고 알려 줄 문장."""
    counts = _counts(draft)
    if isinstance(counts, str):
        return counts
    loss = draft["daily_loss"].strip()
    if AMOUNT.fullmatch(loss) is None or Decimal(loss) <= 0:
        return 'scope.daily_loss는 0보다 큰 금액을 숫자만으로 써 주세요. 예: "300000"'
    expires = _expiry(draft["expires_at"])
    if isinstance(expires, str):
        return expires
    on_loss, mode = _ON_LOSS.get(draft["on_loss"]), _MODES.get(draft["mode"])
    if on_loss is None:
        return 'scope.on_loss는 "liquidate"(손실 한도에서 정리) 또는 "hold"(유지)입니다.'
    if mode is None:
        return 'scope.mode는 "auto"(목표대로 주문) 또는 "suggest"(제안만)입니다.'
    return StrategyScope(*counts, Decimal(loss), expires, on_loss, mode)


def _request(strategy: StrategyDraft, scope: ScopeDraft) -> StrategyRequest | str:
    """서비스에 넘길 요청, 또는 입력이 잘못됐다고 알려 줄 문장."""
    parsed = parse_strategy(strategy)
    if isinstance(parsed, str):
        return parsed
    streams = parse_streams(strategy["streams"])
    if isinstance(streams, str):
        return streams
    approved = _scope(scope)
    if isinstance(approved, str):
        return approved
    code, params, model = parsed
    return StrategyRequest(code, params, streams, approved, model)


# ---- 응답 포장 ---------------------------------------------------------------


def _kst(at: datetime | None) -> str | None:
    return None if at is None else at.astimezone(KST).isoformat(timespec="seconds")


def _scope_data(scope: StrategyScope) -> dict[str, JsonValue]:
    return {
        "quantity": scope.quantity,
        "max_position": scope.max_position,
        "daily_orders": scope.daily_orders,
        "daily_loss": format(scope.daily_loss, "f"),
        "expires_at": _kst(scope.expires_at),
        "on_loss": scope.on_loss,
        "mode": scope.mode,
    }


def _explain(row: RunRow) -> str:
    if row.reason is not None:
        return _REASONS[row.reason]
    return _STATES.get(row.state, "")


def _run_data(view: RunView) -> dict[str, JsonValue]:
    row, run, standing = view.row, view.row.run, view.standing
    return {
        "run_id": run.run_id,
        "state": row.state,
        "reason": row.reason,
        "explain": _explain(row),
        "streams": streams_data(run.streams),
        "mode": run.scope.mode,
        "started_at": _kst(run.started_at),
        "stopped_at": _kst(row.stopped_at),
        "orders_today": view.orders_today,
        "positions": None if standing is None else dict[str, JsonValue](standing.positions),
        "pnl_today": None
        if standing is None or standing.pnl is None
        else format(standing.pnl, "f"),
    }


def _event_data(event: RunEvent) -> dict[str, JsonValue]:
    return {
        "at": _kst(event.at),
        "kind": event.kind,
        "symbol": event.symbol,
        "detail": event.detail,
    }


def _suggestion_data(event: RunEvent) -> dict[str, JsonValue]:
    target = event.detail.get(event.symbol or "")
    return {"at": _kst(event.at), "symbol": event.symbol, "target": target}


def _detail_data(detail: RunDetail) -> dict[str, JsonValue]:
    run = detail.view.row.run
    extra: dict[str, JsonValue] = {
        "scope": _scope_data(run.scope),
        "code_hash": run.code_hash,
        "model_hash": run.model_hash,
        "events": [_event_data(e) for e in detail.events],
    }
    data = _run_data(detail.view) | extra
    if run.scope.mode == "suggest":
        data["suggestions"] = [_suggestion_data(e) for e in detail.suggestions]
        data["suggestion_note"] = SUGGESTION_NOTE
    return data


def _failed(ctx: ToolContext, exc: Exception) -> envelope.Response:
    """켜기·끄기·상태의 서비스 예외를 오류 응답으로. 나머지는 공통 변환(`ToolContext.failed`)."""
    meta = ctx.meta()
    fixed = _FIXED.get(type(exc))
    if fixed is not None:
        code, message, retryable = fixed
        return envelope.error(code, message, meta, retryable=retryable)
    match exc:
        case RiskBlocked(rule=rule, message=message) if rule == AUTO_RUN_OFF:
            return envelope.error(
                "RISK_BLOCKED", f"{message} 등록 화면: {ctx.keys_url}", meta, rule=rule
            )
        case StartRefused(rule=rule, message=message):
            code, retryable = _REFUSALS.get(rule, ("RISK_BLOCKED", False))
            if code == "KEY_NOT_REGISTERED":
                message = f"{message} 등록 화면: {ctx.keys_url}"
            return envelope.error(code, message, meta, retryable=retryable, rule=rule)
        case _:
            return ctx.failed(exc)


# ---- 묶음 숨기기 ---------------------------------------------------------------


class AutoRunGate(Middleware):
    """ "자동 실행"을 켜지 않은 사용자의 도구 목록에서 그 묶음의 도구를 뺀다.

    설정은 요청마다 DB에서 읽는다(등록 화면에서 바꾸면 다음 목록부터 바뀐다). 로그인한 사용자를
    모르면 뺀다.
    """

    def __init__(self, ctx: ToolContext, strategies: StrategyService) -> None:
        self._ctx = ctx
        self._strategies = strategies

    async def on_list_tools(
        self,
        context: MiddlewareContext[mcp_types.ListToolsRequest],
        call_next: CallNext[mcp_types.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        tools = await call_next(context)
        if self._enabled():
            return tools
        return [tool for tool in tools if AUTO_RUN_TAG not in tool.tags]

    def _enabled(self) -> bool:
        try:
            user_id = self._ctx.user_id()
        except NotLoggedIn:
            return False
        return self._strategies.auto_run(user_id)


def register_strategy_tools(
    mcp: FastMCP, ctx: ToolContext, strategies: StrategyService, limiter: RateLimiter
) -> None:
    mcp.add_middleware(AutoRunGate(ctx, strategies))

    @mcp.tool(tags={AUTO_RUN_TAG}, annotations={"readOnlyHint": False, "destructiveHint": False})
    async def preview_strategy(
        strategy: StrategyDraft, scope: ScopeDraft, market: str = "domestic"
    ) -> envelope.Response:
        """전략을 켜도 되는 범위인지 내 위험 한도와 비교하고, 통과하면 확인 토큰을 발급한다.

        전략은 켜지지 않고 주문도 나가지 않는다. 범위와 제약은 strategy://contract.
        쓸 때: backtest_strategy 결과를 사용자에게 보여 주고 실행 범위를 정할 때.
        쓰지 않을 때: 코드를 시험하려면 backtest_strategy, 주문 한 건은 preview_order.
        먼저 같은 code·params·streams·model로 backtest_strategy를 성공해야 한다. 코드가 한 글자만
        달라도, 종목이나 흐름, 모델 파일이 하나라도 달라도 안 된다(error.rule이 not_backtested).
        종목·흐름을 적는 순서는 상관없다.
        strategy: {"code", "params", "streams", "model"(선택)}. backtest_strategy의
        strategy.code·params·model과 data.streams를 똑같이 쓴다. 그 뒤에 사용자가 모델 파일을
        지웠으면 INVALID_INPUT이다. streams: {종목코드: ["minute"(1분봉), "day"(일봉),
        "tick"(체결), "book"(호가) 가운데 하나 이상]}, 종목코드는 search_symbol 결과의 국내 선물.
        "minute"과 "tick"·"book"은 같이 쓸 수 없다(backtest_strategy와 같은 규칙).
        market은 "domestic"만 된다.
        scope: 사용자가 정한 실행 범위. 내 한도(set_risk_limits)를 넘을 수 없다.
        quantity: 1회 주문 계약 수. max_position: 최대 보유 계약 수(quantity 이상).
        daily_orders: 하루 최대 주문 횟수(1~100). daily_loss: 이 전략의 하루 손실 한도, 원 단위
        숫자만 쓴 문자열(예: "300000"). expires_at: 승인이 끝나는 시각, 시간대가 있는 ISO 8601이고
        지금부터 7일 이내(예: "2026-12-03T15:45:00+09:00").
        on_loss: 손실 한도에 닿으면 "liquidate"(포지션 정리) 또는 "hold"(유지).
        mode: "auto"(목표대로 주문) 또는 "suggest"(제안만, 사용자가 확인).
        다음: 응답의 summary를 사용자에게 그대로 보여 주고 승인을 받은 뒤
        start_strategy(confirm_token). 토큰은 2분 동안 한 번만 쓸 수 있다.
        막히면 error.code가 LIMITS_NOT_SET(한도 없음), RISK_BLOCKED(error.rule이
        auto_run_off(사용자가 등록 화면에서 자동 실행을 켜야 함), scope_exceeds_limits,
        not_backtested), INVALID_INPUT(입력 형식)이다.
        """
        user_id = ctx.user_id()
        if not limiter.allow(user_id, PREVIEW_BUCKET):
            return ctx.too_many()
        if market != "domestic":
            return ctx.invalid(DOMESTIC_ONLY)
        request = _request(strategy, scope)
        if isinstance(request, str):
            return ctx.invalid(request)

        try:
            result = await strategies.preview(user_id, request)
        except Exception as exc:
            return _failed(ctx, exc)
        data: dict[str, JsonValue] = {
            "confirm_token": result.confirm_token,
            "code_hash": result.code_hash,
            "summary": result.summary,
            "expires_at": _kst(result.expires_at),
        }
        return envelope.ok(data, ctx.meta())

    @mcp.tool(tags={AUTO_RUN_TAG}, annotations={"readOnlyHint": False, "destructiveHint": True})
    async def start_strategy(
        context: Context, confirm_token: str
    ) -> envelope.Response | mcp_types.InputRequiredResult:
        """preview_strategy를 통과한 전략을 서버에서 켠다. 켜면 대화를 닫아도 서버에서 돈다.

        preview_strategy의 confirm_token만 받는다. 종목, 코드, 범위는 토큰에 저장된 것만 쓴다.
        쓸 때: preview_strategy의 summary를 사용자에게 보여 주고 사용자가 그대로 켜자고 했을 때.
        쓰지 않을 때: 범위나 코드를 바꾸려면 preview_strategy를 다시 부른다. 토큰을 만들어 내지
        않는다. 주문 한 건은 preview_order.
        지원하는 클라이언트에서는 서버가 사용자 화면에 승인 창을 띄운다(종목, 범위, 그리고 자동
        모드가 주문마다 묻지 않고 받아들이는 경고: 시장가, 종가 단일가, 만기 임박). 거절하면 켜지
        않고 그 토큰은 더 쓸 수 없다.
        자동 모드는 승인 범위 안에서 주문하고, 제안 모드는 목표만 기록한다. 서버는 먼저 알리지
        못하므로 결과는 사용자가 물을 때 get_strategy_status로 본다.
        켜지 못하면 error.rule: auto_run_off(사용자가 등록 화면에서 자동 실행을 켜야 함),
        too_many_runs(한 사람 동시 5개), symbol_in_use(종목마다 전략 하나), account_not_flat(그
        종목에 보유·미체결이 있음, 정리한 뒤 다시), not_backtested, model_missing.
        error.code가 TEMPORARILY_UNAVAILABLE(server_full, sandbox_busy 등)이면 잠시 뒤 다시 한다.
        CONFIRM_EXPIRED면 preview_strategy부터 다시 한다.
        다음: 응답의 run_id를 사용자에게 알려 주고, 상태는 get_strategy_status(run_id), 끄기는
        stop_strategy(run_id).
        """
        user_id = ctx.user_id()
        if not limiter.allow(user_id, ORDER_BUCKET):
            return ctx.too_many()
        try:
            pending = strategies.pending_start(user_id, confirm_token)
        except Exception as exc:
            return _failed(ctx, exc)
        approval = await ask(context, "strategy", pending.message, pending.state)
        if isinstance(approval, mcp_types.InputRequiredResult):
            return approval
        try:
            row = await strategies.start(user_id, confirm_token, approval)
        except Exception as exc:
            return _failed(ctx, exc)
        run = row.run
        data: dict[str, JsonValue] = {
            "run_id": run.run_id,
            "state": row.state,
            "streams": streams_data(run.streams),
            "mode": run.scope.mode,
            "scope": _scope_data(run.scope),
            "note": NO_NOTICE,
        }
        return envelope.ok(data, ctx.meta())

    @mcp.tool(tags={AUTO_RUN_TAG}, annotations={"readOnlyHint": False, "destructiveHint": True})
    async def stop_strategy(run_id: str | None = None) -> envelope.Response:
        """켜 둔 전략 하나를 확인 없이 바로 끈다. 위험을 줄이는 일이라 승인 창이 없다.

        쓸 때: 사용자가 전략을 끄거나 멈추자고 할 때. 일시정지(paused)된 전략도 끄면
        멈춤(stopped)이 된다.
        쓰지 않을 때: 포지션 정리는 따로 한다. 끄면 이 전략의 걸린 주문은 취소하지만 보유 포지션은
        그대로 둔다. 정리하려면 get_account로 보고 preview_order → place_order.
        run_id는 start_strategy나 get_strategy_status 결과의 값만 쓴다. 빼면 아무것도 끄지 않는다
        (한 번에 모두 끄는 일은 없다). 이미 멈춘 전략은 지금 상태를 그대로 돌려준다.
        error.code가 TEMPORARILY_UNAVAILABLE이면 켜는 중이니 몇 초 뒤 다시 부른다.
        다음: get_account로 남은 포지션을 확인해 사용자에게 알린다.
        """
        user_id = ctx.user_id()
        chosen = (run_id or "").strip()
        if not chosen:
            return ctx.invalid(NEED_RUN_ID)
        try:
            row = await strategies.stop(user_id, chosen)
        except Exception as exc:
            return _failed(ctx, exc)
        data: dict[str, JsonValue] = {
            "run_id": row.run.run_id,
            "state": row.state,
            "reason": row.reason,
            "stopped_at": _kst(row.stopped_at),
            "note": AFTER_STOP,
        }
        return envelope.ok(data, ctx.meta())

    @mcp.tool(tags={AUTO_RUN_TAG}, annotations={"readOnlyHint": True})
    async def get_strategy_status(run_id: str | None = None) -> envelope.Response:
        """내 자동 실행 전략의 상태, 오늘 주문 수·손익·포지션, 최근 기록, 제안을 본다. 읽기만 한다.

        쓸 때: 사용자가 "전략 어떻게 됐어?"라고 물을 때. 서버는 신호·주문·체결·멈춤을 먼저 알리지
        못한다.
        쓰지 않을 때: 계좌 전체 잔고는 get_account, NH의 주문 내역은 get_orders.
        run_id 없이: 최근에 켠 것부터 20개. run_id를 넣으면 그 전략의 승인 범위와 최근 기록
        50개(오래된 것부터. kind: output 목표, order 주문과 결과, fill 체결, warning, stop)와, 제안
        모드면 종목별 최근 제안(suggestions)도 준다.
        state: starting(켜는 중), running(도는 중), paused(서버 재시작으로 일시정지, 저절로 다시
        켜지지 않음), stopped(멈춤, reason이 사유). explain은 사용자에게 들려줄 설명이다.
        orders_today는 이 전략이 오늘 낸 주문 수. positions(종목별 계약 수, 롱 +)와 pnl_today(원,
        미청산은 마지막 가격으로 평가)는 이 전략이 체결로 센 값이고 running일 때만 있다.
        paused면: get_account·get_orders로 그 종목의 보유와 미체결을 NH와 맞추고(다시 켜려면 그
        종목이 비어 있어야 한다), preview_strategy → start_strategy로 다시 승인받는다. 그 실행은
        stop_strategy로 끈다.
        제안은 목표 포지션이다. 사용자에게 보여 주고 내자고 하면 지금 포지션(get_account)과의
        차이만큼 preview_order → place_order로 낸다.
        """
        user_id = ctx.user_id()
        chosen = (run_id or "").strip()
        data: dict[str, JsonValue]
        try:
            if chosen:
                data = _detail_data(strategies.run(user_id, chosen))
            else:
                runs = [_run_data(v) for v in strategies.runs(user_id)]
                data = {"runs": list[JsonValue](runs), "note": NO_NOTICE}
        except Exception as exc:
            return _failed(ctx, exc)
        return envelope.ok(data, ctx.meta())
