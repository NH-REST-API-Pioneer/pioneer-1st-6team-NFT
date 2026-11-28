"""옵션 묶음 도구: preview_combo, place_combo. "옵션" 묶음이다(`docs/tools.md`).

도구는 입력 검사와 응답 포장만 한다. 구조 판정, 한도 검사, 확인 토큰, 다리 순서, 전송은
`services/combos.py`가 한다. `place_combo`는 확인 토큰 하나만 받는다(묶음 내용은 묶음 행에 저장된
것만 쓴다). `ORDERS_ENABLED=false`면 이 도구들은 등록되지 않는다.
"""

from collections.abc import Awaitable
from decimal import Decimal
from typing import TypedDict

import mcp_types
from fastmcp import Context, FastMCP
from pydantic import ConfigDict, with_config

from nh_mcp import envelope
from nh_mcp.domain import KST, ComboRequest, JsonValue, Leg, Side, Unbounded, UserId
from nh_mcp.ops.rate_limit import ORDER_BUCKET, PREVIEW_BUCKET, RateLimiter
from nh_mcp.services.combos import LEGS, ComboPreview, ComboResult, ComboService, LegResult
from nh_mcp.services.orders import SIDE_NAMES
from nh_mcp.tools.approval import ask
from nh_mcp.tools.context import DOMESTIC_ONLY, ToolContext, Whole, text, whole
from nh_mcp.tools.options import OPTIONS_TAG
from nh_mcp.tools.order import MAX_QUANTITY, MAX_SYMBOL_LENGTH, PRICE

_SIDES: dict[str, Side] = {"buy": "buy", "sell": "sell"}
_OPPOSITE: dict[Side, Side] = {"buy": "sell", "sell": "buy"}
SLIPPAGE_STEP = Decimal("0.01")
MAX_COMBO_ID_LENGTH = 64
# 모르는 칸은 조용히 버리지 않고 거절한다.
_EXACT = ConfigDict(extra="forbid")


@with_config(_EXACT)
class LegDraft(TypedDict):
    """다리 하나. price는 계약당 지정가(프리미엄)를 숫자만 쓴 문자열로."""

    symbol: str
    side: str
    price: str


@with_config(_EXACT)
class ComboDraft(TypedDict):
    """묶음 내용. 다리는 2개, 수량은 다리마다 같다. slippage는 순가격 허용 폭(포인트)."""

    legs: list[LegDraft]
    quantity: Whole
    slippage: str


def _decimal(raw: str) -> Decimal | None:
    text = raw.strip()
    return Decimal(text) if PRICE.fullmatch(text) else None


def _leg(draft: LegDraft) -> Leg | str:
    """다리, 또는 입력이 잘못됐다고 알려 줄 문장."""
    symbol = draft["symbol"].strip()
    if not symbol or len(symbol) > MAX_SYMBOL_LENGTH:
        return "legs의 symbol은 get_option_chain 결과의 옵션 종목코드를 넣어 주세요."
    side = _SIDES.get(draft["side"])
    if side is None:
        return 'legs의 side는 "buy"(매수) 또는 "sell"(매도)입니다.'
    price = _decimal(draft["price"])
    if price is None or price <= 0:
        return 'legs의 price는 0보다 큰 지정가를 숫자만으로 써 주세요. 예: "2.00"'
    return Leg(symbol, side, price)


def _parse(draft: ComboDraft) -> ComboRequest | str:
    """묶음 요청, 또는 입력이 잘못됐다고 알려 줄 문장."""
    if len(draft["legs"]) != LEGS:
        return f"legs는 다리 {LEGS}개를 넣어 주세요."
    first, second = (_leg(leg) for leg in draft["legs"])
    if isinstance(first, str):
        return first
    if isinstance(second, str):
        return second
    quantity = whole(draft["quantity"], 1, MAX_QUANTITY)
    if quantity is None:
        return f"quantity는 1 이상 {MAX_QUANTITY} 이하의 정수(다리마다의 계약 수)여야 합니다."
    slippage = _decimal(draft["slippage"])
    if slippage is None or slippage % SLIPPAGE_STEP != 0:
        return 'slippage는 0 이상의 순가격 허용 폭을 0.01 단위로, 숫자만으로 써 주세요. 예: "0.05"'
    return ComboRequest("domestic", (first, second), quantity, slippage)


def _preview_data(preview: ComboPreview) -> dict[str, JsonValue]:
    loss = preview.max_loss
    unbounded = isinstance(loss, Unbounded)
    legs: list[JsonValue] = [
        {
            "symbol": leg.symbol,
            "name": leg.name,
            "side": leg.side,
            "price": text(leg.price),
            "filled": leg.filled,
        }
        for leg in preview.legs
    ]
    return {
        "combo_id": preview.combo_id,
        "structure": preview.structure,
        "quantity": preview.quantity,
        "legs": legs,
        "net_debit": text(preview.net_debit),
        "worst_net_debit": text(preview.worst_net_debit),
        "max_loss": None if unbounded else text(loss),
        "max_loss_unbounded": unbounded,
        "warnings": list[JsonValue](preview.warnings),
        "confirm_token": preview.confirm_token,
        "expires_at": preview.expires_at.astimezone(KST).isoformat(timespec="seconds"),
    }


def _leg_data(leg: LegResult) -> dict[str, JsonValue]:
    return {
        "symbol": leg.symbol,
        "side": leg.side,
        "quantity": leg.quantity,
        "filled": leg.filled,
        "avg_price": text(leg.avg_price),
        "order_nos": list[JsonValue](leg.order_nos),
    }


def _next(result: ComboResult) -> list[JsonValue]:
    """일부 체결이나 모름일 때 AI가 할 수 있는 일. 고르는 것은 사용자다."""
    resume = f'preview_combo(resume="{result.combo_id}")'
    if result.status == "unknown":
        return [
            "대조: get_orders로 다리 주문의 체결을 확인한다. 그 전에는 어떤 주문도 다시 내지 "
            f"않는다. 확인 뒤 일부만 체결됐으면 {resume}로 이어 할 수 있다."
        ]
    closes = ", ".join(
        f"{leg.symbol} {SIDE_NAMES[_OPPOSITE[leg.side]]} {leg.filled}계약"
        for leg in result.legs
        if leg.filled
    )
    return [
        f"완성: {resume}로 남은 수량을 다시 미리보기한다. 처음 다리 가격과 허용 폭 끝 순가격을 "
        "그대로 쓰고 지금 시세로 가격을 다시 정하지 않는다.",
        f"되돌리기: 체결된 다리를 preview_order로 청산한다({closes}).",
        "유지: 체결된 그대로 둔다.",
    ]


def _result_data(result: ComboResult) -> dict[str, JsonValue]:
    # 첫 다리도 보내지 못하고 멈췄으면(서비스의 previewed) 체결된 것이 없다.
    status = "nothing_filled" if result.status == "previewed" else result.status
    data: dict[str, JsonValue] = {
        "combo_id": result.combo_id,
        "status": status,
        "legs": [_leg_data(leg) for leg in result.legs],
    }
    if result.stop is not None:
        data["stop_reason"] = result.stop.reason
        if result.stop.rule is not None:
            data["rule"] = result.stop.rule
    if status in ("partial", "unknown"):
        data["next"] = _next(result)
    return data


def _call(
    combos: ComboService, user_id: UserId, combo: ComboDraft | None, resume: str | None
) -> Awaitable[ComboPreview] | str:
    """부를 미리보기, 또는 입력이 잘못됐다고 알려 줄 문장."""
    if combo is not None and resume is None:
        parsed = _parse(combo)
        return parsed if isinstance(parsed, str) else combos.preview(user_id, parsed)
    if combo is not None or resume is None:
        return "combo와 resume 중 하나만 넣어 주세요."
    combo_id = resume.strip()
    if not combo_id or len(combo_id) > MAX_COMBO_ID_LENGTH:
        return "resume에는 place_combo 결과의 combo_id를 그대로 넣어 주세요."
    return combos.preview_resume(user_id, combo_id)


def register_combo_tools(
    mcp: FastMCP, ctx: ToolContext, combos: ComboService, limiter: RateLimiter
) -> None:
    @mcp.tool(tags={OPTIONS_TAG}, annotations={"readOnlyHint": False, "destructiveHint": False})
    async def preview_combo(
        combo: ComboDraft | None = None, resume: str | None = None, market: str = "domestic"
    ) -> envelope.Response:
        """옵션 두 종목을 묶은 주문을 보내기 전에 구조 전체로 점검한다. 실제 주문은 나가지 않는다.

        쓸 때: 사용자가 수직 스프레드, 스트래들, 스트랭글처럼 옵션 두 종목을 함께 사고팔자고 할 때.
        쓰지 않을 때: 한 종목 주문은 preview_order. 옵션 시세와 종목코드는 get_option_chain.
        다음: 결과의 구조, 다리 순서, 순가격, 최대 손실, 경고를 사용자에게 보여 주고 승인을 받은 뒤
        place_combo(confirm_token). 토큰은 2분 동안 한 번만 쓸 수 있다.
        combo와 resume 중 하나만 넣는다.
        combo: {"legs": [{"symbol", "side", "price"}, {...}], "quantity", "slippage"}. 다리는 2개.
        symbol은 get_option_chain 결과의 종목코드만 쓴다. 코드를 추측해서 만들지 않는다.
        side는 "buy" 또는 "sell", price는 계약당 지정가 문자열(예: "2.00"), quantity는 다리마다의
        계약 수, slippage는 순가격 허용 폭(포인트, 0.01 단위 문자열, 예: "0.05").
        resume: 일부만 체결된 묶음의 combo_id. 남은 수량을 처음 다리 가격과 허용 폭 끝 순가격
        그대로 다시 미리보기한다. 지금 시세로 가격을 다시 정하지 않는다.
        구조는 서버가 다리에서 읽는다. structure는 debit_vertical·credit_vertical·long_straddle·
        long_strangle·short_straddle·short_strangle 중 하나다. legs는 보내는 순서(사는 다리 먼저)다.
        net_debit은 계약당 순가격(받는 구조는 음수), worst_net_debit은 허용 폭 끝의 순가격이다.
        max_loss는 묶음 전체의 최대 손실(원)이고, 한정되지 않으면 null이고 max_loss_unbounded가
        true다. 막히면 error.code가 LIMITS_NOT_SET(set_risk_limits의 options.max_combo_loss를
        사용자와 정한다), RISK_BLOCKED(error.rule: combo_unsupported 받지 않는 구조,
        combo_loss_limit 묶음 한도 넘음, combo_uncovered 사는 다리를 보유하지 않아 파는 다리를
        보낼 수 없음(resume 전에 사용자가 직접 청산했으면 생긴다), options_off·option_sell_off 등록
        화면 스위치), MARKET_CLOSED다.
        """
        user_id = ctx.user_id()
        if not limiter.allow(user_id, PREVIEW_BUCKET):
            return ctx.too_many()
        if market != "domestic":
            return ctx.invalid(DOMESTIC_ONLY)
        call = _call(combos, user_id, combo, resume)
        if isinstance(call, str):
            return ctx.invalid(call)

        async def work() -> dict[str, JsonValue]:
            return _preview_data(await call)

        return await ctx.answer(work())

    @mcp.tool(tags={OPTIONS_TAG}, annotations={"readOnlyHint": False, "destructiveHint": True})
    async def place_combo(
        context: Context, confirm_token: str
    ) -> envelope.Response | mcp_types.InputRequiredResult:
        """미리보기를 통과한 옵션 묶음을 NH 모의 계좌로 보낸다. 사는 다리부터 한 다리씩 보낸다.

        preview_combo의 confirm_token만 받는다. 사용자에게 묶음 내용을 보여 주고 승인을 받은 뒤
        부른다. 토큰을 만들어 내지 않는다. 지원하는 클라이언트에서는 서버가 승인 창을 띄운다.
        거절하면 주문하지 않는다.
        결과 status: filled(다 체결), partial(일부만 체결), nothing_filled(체결 없음),
        unknown(끝났는지 모르는 다리 주문이 있음). stop_reason은 멈춘 이유, rule은 막은 규칙이다.
        partial이면 next의 완성·되돌리기·유지를 사용자에게 보여 주고 사용자가 고르게 한다.
        AI가 혼자 고르지 않는다. unknown이면 다시 주문하지 말고 get_orders로 확인한다.
        error.code가 CONFIRM_EXPIRED면 preview_combo부터 다시 한다.
        """
        user_id = ctx.user_id()
        if not limiter.allow(user_id, ORDER_BUCKET):
            return ctx.too_many()
        try:
            pending = await combos.pending(user_id, confirm_token)
        except Exception as exc:
            return ctx.failed(exc)
        approval = await ask(context, "combo", pending.message, pending.state)
        if isinstance(approval, mcp_types.InputRequiredResult):
            return approval

        async def work() -> dict[str, JsonValue]:
            return _result_data(await combos.place(user_id, confirm_token, approval))

        return await ctx.answer(work())
