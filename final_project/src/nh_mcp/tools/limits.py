"""한도 도구: get_risk_limits, set_risk_limits. 처음 정하기와 올리기는 서버가 직접 승인을 받는다."""

import re
from decimal import Decimal
from typing import NotRequired, TypedDict

import mcp_types
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_context
from pydantic import ConfigDict, with_config

from nh_mcp import envelope
from nh_mcp.domain import KST, JsonValue, OptionLimits, RiskLimits
from nh_mcp.ops.rate_limit import LIMITS_BUCKET, RateLimiter
from nh_mcp.services.limits import LimitService, SetResult
from nh_mcp.tools.approval import ask
from nh_mcp.tools.context import DOMESTIC_ONLY, ToolContext, Whole, whole

# 부호, 지수, NaN, Infinity, 전각 숫자, 밑줄을 모두 막으려고 ASCII 숫자만 받는다.
_AMOUNT = re.compile(r"[0-9]{1,18}(\.[0-9]{1,6})?")
# SQLite 정수 범위를 넘는 값이 저장소 오류가 되지 않게 하는 상한. 실제 한도로는 넉넉하다.
_MAX_COUNT = 10**9

_NEXT = {
    "applied": "한도를 적용했습니다.",
    "declined": "사용자가 거절해 한도를 바꾸지 않았습니다.",
    "changed": (
        "요청하는 사이 한도가 다른 곳에서 바뀌어 적용하지 않았습니다. "
        "get_risk_limits로 지금 값을 확인하고 다시 요청해 주세요."
    ),
    "needs_approval": (
        "이 클라이언트는 승인 창을 띄울 수 없어 적용하지 않았습니다. "
        "사용자에게 approval_url을 열어 로그인한 뒤 승인하라고 안내해 주세요."
    ),
}


@with_config(ConfigDict(extra="forbid"))
class OptionLimitsDraft(TypedDict):
    """옵션 한도. 앞의 셋은 함께 넣는다. 금액은 숫자만 쓴 문자열(원). max_combo_loss는 선택."""

    premium_daily: str
    max_position: Whole
    max_short: Whole
    max_combo_loss: NotRequired[str]


def _options_data(value: OptionLimits | None) -> dict[str, JsonValue] | None:
    if value is None:
        return None
    return {
        "premium_daily": str(value.premium_daily),
        "max_position": value.max_position,
        "max_short": value.max_short,
        "max_combo_loss": None if value.max_combo_loss is None else str(value.max_combo_loss),
    }


def _limits_data(value: RiskLimits | None) -> dict[str, JsonValue] | None:
    if value is None:
        return None
    return {
        "daily_loss": str(value.daily_loss),
        "max_position": value.max_position,
        "max_order_qty": value.max_order_qty,
        "options": _options_data(value.options),
    }


def _amount(value: str) -> Decimal | None:
    """0보다 큰 금액이면 그 값, 아니면 None."""
    text = value.strip()
    if _AMOUNT.fullmatch(text) is None:
        return None
    amount = Decimal(text)
    return amount if amount > 0 else None


def _parse_options(draft: OptionLimitsDraft) -> OptionLimits | str:
    """OptionLimits, 또는 입력이 잘못됐다고 알려 줄 문장. max_combo_loss를 안 주면 None."""
    amount = _amount(draft["premium_daily"])
    if amount is None:
        return 'options.premium_daily는 0보다 큰 금액을 숫자만으로 써 주세요. 예: "500000"'
    position = whole(draft["max_position"], 1, _MAX_COUNT)
    if position is None:
        return f"options.max_position은 1 이상 {_MAX_COUNT} 이하의 정수여야 합니다."
    short = whole(draft["max_short"], 0, _MAX_COUNT)
    if short is None:
        return f"options.max_short는 0 이상 {_MAX_COUNT} 이하의 정수여야 합니다."
    combo_text = draft.get("max_combo_loss")
    combo = None if combo_text is None else _amount(combo_text)
    if combo_text is not None and combo is None:
        return 'options.max_combo_loss는 0보다 큰 금액을 숫자만으로 써 주세요. 예: "300000"'
    return OptionLimits(amount, position, short, combo)


def _parse(
    daily_loss: str, max_position: Whole, max_order_qty: Whole, options: OptionLimitsDraft | None
) -> RiskLimits | str:
    """RiskLimits, 또는 입력이 잘못됐다고 알려 줄 문장. options를 안 주면 옵션 한도는 None."""
    amount = _amount(daily_loss)
    if amount is None:
        return 'daily_loss는 0보다 큰 금액을 숫자만으로 써 주세요. 예: "1000000"'
    position = whole(max_position, 1, _MAX_COUNT)
    if position is None:
        return f"max_position는 1 이상 {_MAX_COUNT} 이하의 정수여야 합니다."
    order_qty = whole(max_order_qty, 1, _MAX_COUNT)
    if order_qty is None:
        return f"max_order_qty는 1 이상 {_MAX_COUNT} 이하의 정수여야 합니다."
    option_limits = None if options is None else _parse_options(options)
    if isinstance(option_limits, str):
        return option_limits
    return RiskLimits(amount, position, order_qty, option_limits)


def _result_data(result: SetResult) -> dict[str, JsonValue]:
    data: dict[str, JsonValue] = {
        "outcome": result.outcome,
        "current": _limits_data(result.current),
        "requested": _limits_data(result.requested),
        "message": _NEXT[result.outcome],
    }
    if result.approval_url is not None and result.expires_at is not None:
        data["approval_url"] = result.approval_url
        data["expires_at"] = result.expires_at.astimezone(KST).isoformat(timespec="seconds")
    return data


def register_limit_tools(
    mcp: FastMCP, ctx: ToolContext, service: LimitService, limiter: RateLimiter
) -> None:
    @mcp.tool(annotations={"readOnlyHint": True})
    def get_risk_limits(market: str = "domestic") -> envelope.Response:
        """내가 정한 위험 한도(하루 손실, 종목별·1회 최대 계약 수, 옵션 한도)를 돌려준다.

        쓸 때: 주문하기 전에 한도가 정해져 있는지 볼 때, 사용자가 한도를 물을 때.
        쓰지 않을 때: 한도를 바꾸는 것은 set_risk_limits.
        한도가 없으면 limits가 null이다. 한도가 없으면 주문은 모두 막힌다.
        옵션 한도가 없으면 limits.options가 null이고, 포지션을 늘리는 옵션 주문은 막힌다.
        묶음 최대 손실 한도가 없으면 limits.options.max_combo_loss가 null이고, 묶음 주문만 막힌다.
        """
        if market != "domestic":
            return ctx.invalid(DOMESTIC_ONLY)
        current = service.get(ctx.user_id(), "domestic")
        data: dict[str, JsonValue] = {"market": "domestic", "limits": _limits_data(current)}
        if current is None:
            data["message"] = (
                "한도가 아직 없습니다. 사용자와 값을 정한 뒤 set_risk_limits로 정해 주세요. "
                "처음 정할 때는 사용자의 승인이 필요합니다."
            )
        return envelope.ok(data, ctx.meta())

    @mcp.tool(annotations={"readOnlyHint": False})
    async def set_risk_limits(
        daily_loss: str,
        max_position: Whole,
        max_order_qty: Whole,
        market: str = "domestic",
        options: OptionLimitsDraft | None = None,
    ) -> envelope.Response | mcp_types.InputRequiredResult:
        """위험 한도를 정하거나 바꾼다. 앞의 세 값은 늘 넣고, 옵션 한도는 options로 넣는다.

        쓸 때: 사용자가 하루 손실, 종목별 최대 계약 수, 1회 최대 계약 수, 옵션 한도를 정하거나
        바꾸자고 할 때.
        쓰지 않을 때: 지금 한도를 보는 것은 get_risk_limits.
        daily_loss: 하루 손실 한도(원). 0보다 큰 숫자만 쓴 문자열. 예: "1000000".
        max_order_qty는 선물과 옵션이 같이 쓴다.
        options: {"premium_daily", "max_position", "max_short"} 셋을 함께 넣는다.
        premium_daily는 오늘 옵션 매수에 쓸 프리미엄 합계 한도(원, daily_loss와 같은 모양),
        max_position은 모든 옵션 종목의 계약 수 합계 상한(매수·매도 합, 1 이상),
        max_short는 옵션 매도 계약 수 합계 상한(0 이상, 0이면 새 매도는 막힌다).
        예: {"premium_daily": "500000", "max_position": 10, "max_short": 0}
        max_combo_loss는 선택이다. 묶음 주문 하나의 최대 손실 상한(원, daily_loss와 같은 모양).
        예: {"premium_daily": "500000", "max_position": 10, "max_short": 0,
        "max_combo_loss": "300000"}. 빼면 지금 묶음 한도를 그대로 둔다. 없으면 묶음 주문만 막힌다.
        options를 빼면 지금 옵션 한도를 그대로 둔다. 이 도구로 옵션 한도나 묶음 한도를 지우지는
        못한다.
        옵션 한도가 없으면 포지션을 늘리는 옵션 주문은 막힌다.
        낮추기(모든 값이 지금 이하)는 바로 적용된다. 처음 정하거나(옵션 한도나 묶음 한도를 처음
        넣는 것 포함) 하나라도 올리면 서버가 사용자 화면에 승인 창을 띄운다. 창을 못 띄우는
        클라이언트면 적용하지 않고 outcome이 needs_approval이며 approval_url이 온다. 사용자에게
        그 링크를 열라고 안내한다. 승인은 AI가 대신할 수 없고, 토큰 같은 인자도 없다.
        """
        user_id = ctx.user_id()
        if not limiter.allow(user_id, LIMITS_BUCKET):
            return ctx.too_many()
        if market != "domestic":
            return ctx.invalid(DOMESTIC_ONLY)
        parsed = _parse(daily_loss, max_position, max_order_qty, options)
        if isinstance(parsed, str):
            return ctx.invalid(parsed)
        plan = service.plan(user_id, "domestic", parsed)
        approval = None
        if plan.needs_approval:
            # Context를 인자로 받으면 인자가 lint 상한(5개)을 넘어 get_context()로 꺼낸다.
            asked = await ask(get_context(), "limits", plan.message, plan.state)
            if isinstance(asked, mcp_types.InputRequiredResult):
                return asked
            approval = asked
        result = service.commit(user_id, "domestic", plan, approval)
        return envelope.ok(_result_data(result), ctx.meta())
