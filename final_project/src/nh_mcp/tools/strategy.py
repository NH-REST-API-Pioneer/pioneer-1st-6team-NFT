"""전략 실행 승인 도구: preview_strategy.

도구는 입력 검사와 응답 포장만 한다. 한도 비교, 백테스트 확인, 확인 토큰 발급은
`services/strategy.py`가 한다. 속도 제한은 서비스를 부르기 전에 본다.
`ORDERS_ENABLED=false`면 이 도구는 등록되지 않는다(주문 도구와 같다).
"""

from datetime import datetime
from decimal import Decimal
from typing import TypedDict

from fastmcp import FastMCP

from nh_mcp import envelope
from nh_mcp.domain import KST, JsonValue, OnLoss, StrategyMode, StrategyScope
from nh_mcp.ops.rate_limit import PREVIEW_BUCKET, RateLimiter
from nh_mcp.services.strategy import (
    MAX_DAILY_ORDERS,
    StrategyRequest,
    StrategyService,
)
from nh_mcp.tools.backtest import AMOUNT, StrategyCode, parse_strategy, parse_streams
from nh_mcp.tools.context import DOMESTIC_ONLY, ToolContext, Whole, whole
from nh_mcp.tools.order import MAX_QUANTITY

_ON_LOSS: dict[str, OnLoss] = {"liquidate": "liquidate", "hold": "hold"}
_MODES: dict[str, StrategyMode] = {"auto": "auto", "suggest": "suggest"}


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


def register_strategy_tools(
    mcp: FastMCP, ctx: ToolContext, strategies: StrategyService, limiter: RateLimiter
) -> None:
    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False})
    async def preview_strategy(
        strategy: StrategyDraft, scope: ScopeDraft, market: str = "domestic"
    ) -> envelope.Response:
        """전략을 켜도 되는 범위인지 내 위험 한도와 비교하고, 통과하면 확인 토큰을 발급한다.

        전략은 켜지지 않고 주문도 나가지 않는다. 이 토큰을 쓰는 start_strategy가 아직 없다.
        범위와 제약은 strategy://contract.
        쓸 때: backtest_strategy 결과를 사용자에게 보여 주고 실행 범위를 정할 때.
        쓰지 않을 때: 코드를 시험하려면 backtest_strategy, 주문은 preview_order.
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
        daily_orders: 하루 최대 주문 횟수(1~100). daily_loss: 하루 손실 한도, 원 단위 숫자만 쓴
        문자열(예: "300000"). expires_at: 승인이 끝나는 시각, 시간대가 있는 ISO 8601이고
        지금부터 7일 이내(예: "2026-12-03T15:45:00+09:00").
        on_loss: 손실 한도에 닿으면 "liquidate"(포지션 정리) 또는 "hold"(유지).
        mode: "auto"(목표대로 주문) 또는 "suggest"(제안만, 사용자가 확인).
        다음: 응답의 summary를 사용자에게 그대로 보여 주고 승인을 받는다. 토큰은 2분 동안 한 번만
        쓸 수 있다. 막히면 error.code가 LIMITS_NOT_SET(한도 없음), RISK_BLOCKED(error.rule이
        scope_exceeds_limits 또는 not_backtested), INVALID_INPUT(입력 형식)이다.
        """
        user_id = ctx.user_id()
        if not limiter.allow(user_id, PREVIEW_BUCKET):
            return ctx.too_many()
        if market != "domestic":
            return ctx.invalid(DOMESTIC_ONLY)
        request = _request(strategy, scope)
        if isinstance(request, str):
            return ctx.invalid(request)

        async def work() -> dict[str, JsonValue]:
            result = await strategies.preview(user_id, request)
            return {
                "confirm_token": result.confirm_token,
                "code_hash": result.code_hash,
                "summary": result.summary,
                "expires_at": result.expires_at.astimezone(KST).isoformat(timespec="seconds"),
            }

        return await ctx.answer(work())
