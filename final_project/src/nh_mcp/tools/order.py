"""주문 도구: preview_order, place_order, cancel_order.

도구는 입력 검사와 응답 포장만 한다. 한도 검사, 확인 토큰, 전송은 `services/orders.py`가 한다.
`place_order`는 확인 토큰 하나만 받는다(주문 내용은 토큰에 저장된 것만 쓴다).
속도 제한은 서비스를 부르기 전에 본다. `ORDERS_ENABLED=false`면 이 도구들은 등록되지 않는다.
"""

import re
from decimal import Decimal
from typing import NotRequired, Protocol, TypedDict

import mcp_types
from fastmcp import Context, FastMCP

from nh_mcp import envelope
from nh_mcp.domain import KST, JsonValue, Market, OrderRequest, OrderType, Side
from nh_mcp.ops.rate_limit import ORDER_BUCKET, PREVIEW_BUCKET, RateLimiter
from nh_mcp.services.orders import OPTION_ACTIONS, OptionTerms, OrderService
from nh_mcp.tools.approval import ask
from nh_mcp.tools.context import DOMESTIC_ONLY, ToolContext, Whole, whole

# 부호, 지수, NaN, Infinity, 전각 숫자, 밑줄, 쉼표를 모두 막으려고 ASCII 숫자만 받는다.
PRICE = re.compile(r"[0-9]{1,12}(\.[0-9]{1,6})?")
_SIDES: dict[str, Side] = {"buy": "buy", "sell": "sell"}
_TYPES: dict[str, OrderType] = {"limit": "limit", "market": "market"}
MAX_QUANTITY = 10**6
MAX_SYMBOL_LENGTH = 32


class OrderDraft(TypedDict):
    """주문 내용. 가격은 지정가일 때만 숫자만 쓴 문자열로 넣는다."""

    symbol: str
    side: str
    type: str
    quantity: Whole
    price: NotRequired[str | None]


class _OrderLike(Protocol):
    """주문 요청과 저장된 주문이 함께 가진 칸."""

    @property
    def market(self) -> Market: ...
    @property
    def symbol(self) -> str: ...
    @property
    def side(self) -> Side: ...
    @property
    def type(self) -> OrderType: ...
    @property
    def quantity(self) -> int: ...
    @property
    def price(self) -> Decimal | None: ...


def _problem(draft: OrderDraft) -> str | None:
    """종목·방향·유형 칸이 잘못됐으면 알려 줄 문장."""
    code = draft["symbol"].strip()
    if not code or len(code) > MAX_SYMBOL_LENGTH:
        return (
            "symbol은 search_symbol(선물)이나 get_option_chain(옵션) 결과의 종목코드를 넣어 주세요."
        )
    if draft["side"] not in _SIDES:
        return 'side는 "buy"(매수) 또는 "sell"(매도)입니다.'
    if draft["type"] not in _TYPES:
        return 'type은 "limit"(지정가) 또는 "market"(시장가)입니다.'
    return None


def _limit_price(raw: str | None) -> Decimal | str:
    """지정가의 가격, 또는 잘못됐다고 알려 줄 문장."""
    text = "" if raw is None else raw.strip()
    if PRICE.fullmatch(text) is None:
        return '지정가 주문의 price는 0보다 큰 가격을 숫자만으로 써 주세요. 예: "784.55"'
    value = Decimal(text)
    if value <= 0:
        return "price는 0보다 커야 합니다."
    return value


def _parse(draft: OrderDraft) -> OrderRequest | str:
    """주문 요청, 또는 입력이 잘못됐다고 알려 줄 문장."""
    problem = _problem(draft)
    if problem is not None:
        return problem
    order_type = _TYPES[draft["type"]]
    raw_price = draft.get("price")
    if order_type == "market":
        if raw_price is not None:
            return "시장가 주문에는 price를 넣지 않습니다."
        price = None
    else:
        limit = _limit_price(raw_price)
        if isinstance(limit, str):
            return limit
        price = limit
    quantity = whole(draft["quantity"], 1, MAX_QUANTITY)
    if quantity is None:
        return f"quantity는 1 이상 {MAX_QUANTITY} 이하의 정수(계약 수)여야 합니다."
    symbol, side = draft["symbol"].strip(), _SIDES[draft["side"]]
    return OrderRequest("domestic", symbol, side, order_type, quantity, price)


def _order_data(order: _OrderLike) -> dict[str, JsonValue]:
    return {
        "market": order.market,
        "symbol": order.symbol,
        "side": order.side,
        "type": order.type,
        "quantity": order.quantity,
        "price": None if order.price is None else format(order.price, "f"),
    }


def _option_data(option: OptionTerms) -> dict[str, JsonValue]:
    return {
        "action": option.action,
        "label": OPTION_ACTIONS[option.action],
        "premium": None if option.premium is None else format(option.premium, "f"),
    }


def register_order_tools(
    mcp: FastMCP, ctx: ToolContext, orders: OrderService, limiter: RateLimiter
) -> None:
    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False})
    async def preview_order(order: OrderDraft, market: str = "domestic") -> envelope.Response:
        """주문을 보내기 전에 한도와 거래 시간을 점검한다. 실제 주문은 나가지 않는다.

        쓸 때: 사용자가 사거나 팔겠다고 했을 때 항상 먼저 부른다.
        쓰지 않을 때: 시세만 궁금할 때는 get_quote, 잔고는 get_account.
        다음: 결과의 주문 내용(종목명, 매수/매도, 수량, 가격, 경고)을 사용자에게 보여 주고
        승인을 받은 뒤 place_order(confirm_token). 토큰은 2분 동안 한 번만 쓸 수 있다.
        order: {"symbol", "side", "type", "quantity", "price"}.
        symbol은 search_symbol(선물)이나 get_option_chain(옵션) 결과의 코드만 쓴다. 코드를 추측해서
        만들지 않는다. 옵션이면 결과에 option{action, label, premium}이 더 있다. action은
        new_buy(새 매수)·new_sell(새 매도, 보유 없이 파는 주문)·close(청산), premium은 매수의
        가격·승수·수량의 곱(원, 시장가는 상한가로 셈)이다. 이것도 사용자에게 보여 준다.
        옵션의 새 매수·새 매도는 사용자가 등록 화면에서 "옵션"을 켜고(아니면 error.rule이
        options_off) 옵션 한도(set_risk_limits의 options)를 정해야 한다(아니면 LIMITS_NOT_SET).
        새 매도는 "옵션 매도"도 켜야 한다(아니면 option_sell_off). 스위치는 사람만 켤 수 있다.
        청산은 이 스위치와 옵션 한도에 막히지 않는다.
        side: "buy"(매수) 또는 "sell"(매도). type: "limit"(지정가) 또는 "market"(시장가).
        quantity: 계약 수. price: 지정가일 때만, 숫자만 쓴 문자열(예: "784.55"). 시장가는 뺀다.
        예: {"symbol": "A016C000", "side": "buy", "type": "limit", "quantity": 1, "price": "784.55"}
        막히면 error.code가 LIMITS_NOT_SET(한도 없음, set_risk_limits로 정한다),
        RISK_BLOCKED(error.rule이 막은 규칙), MARKET_CLOSED(거래 시간 아님)다.
        """
        user_id = ctx.user_id()
        if not limiter.allow(user_id, PREVIEW_BUCKET):
            return ctx.too_many()
        if market != "domestic":
            return ctx.invalid(DOMESTIC_ONLY)
        parsed = _parse(order)
        if isinstance(parsed, str):
            return ctx.invalid(parsed)

        async def work() -> dict[str, JsonValue]:
            preview = await orders.preview(user_id, parsed)
            data: dict[str, JsonValue] = {
                "order": _order_data(preview.order),
                "name": preview.contract_name,
                "warnings": list[JsonValue](preview.warnings),
                "confirm_token": preview.confirm_token,
                "expires_at": preview.expires_at.astimezone(KST).isoformat(timespec="seconds"),
            }
            if preview.option is not None:
                data["option"] = _option_data(preview.option)
            return data

        return await ctx.answer(work())

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def place_order(
        context: Context, confirm_token: str
    ) -> envelope.Response | mcp_types.InputRequiredResult:
        """미리보기를 통과한 주문을 NH 모의 계좌로 보낸다.

        preview_order의 confirm_token만 받는다.
        사용자에게 주문 내용을 보여 주고 승인을 받은 뒤 부른다.
        쓸 때: 사용자가 preview_order 결과의 주문을 내자고 승인했을 때.
        쓰지 않을 때: 주문 내용을 바꾸려면 preview_order를 다시 부른다. 토큰을 만들어 내지 않는다.
        지원하는 클라이언트에서는 서버가 사용자 화면에 승인 창을 띄운다. 거절하면 주문하지 않는다.
        다음: 체결 여부는 get_orders. error.code가 UNKNOWN_STATE면 다시 주문하지 말고
        get_orders로 확인한다. CONFIRM_EXPIRED면 preview_order부터 다시 한다.
        """
        user_id = ctx.user_id()
        if not limiter.allow(user_id, ORDER_BUCKET):
            return ctx.too_many()
        try:
            pending = await orders.pending(user_id, confirm_token)
        except Exception as exc:
            return ctx.failed(exc)
        approval = await ask(context, "order", pending.message, pending.state)
        if isinstance(approval, mcp_types.InputRequiredResult):
            return approval

        async def work() -> dict[str, JsonValue]:
            stored = await orders.place(user_id, confirm_token, approval)
            return {
                "status": stored.status,
                "order_no": stored.nh_order_no,
                "order": _order_data(stored),
            }

        return await ctx.answer(work())

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def cancel_order(order_no: str, market: str = "domestic") -> envelope.Response:
        """미체결 주문의 남은 수량 전부를 취소한다. 확인 토큰 없이 바로 보낸다.

        쓸 때: 사용자가 아직 체결되지 않은 주문을 취소하자고 할 때.
        쓰지 않을 때: 이미 체결된 포지션을 정리하는 것은 반대 방향 주문(preview_order).
        order_no는 get_orders나 place_order 결과의 주문번호를 그대로 쓴다.
        다음: 취소 결과는 get_orders로 확인한다. error.code가 UNKNOWN_STATE면 다시 취소하지 말고
        get_orders로 확인한다.
        """
        user_id = ctx.user_id()
        if not limiter.allow(user_id, ORDER_BUCKET):
            return ctx.too_many()
        if market != "domestic":
            return ctx.invalid(DOMESTIC_ONLY)
        number = order_no.strip()

        async def work() -> dict[str, JsonValue]:
            ack = await orders.cancel(user_id, number)
            return {"order_no": number, "cancel_order_no": ack.order_no}

        return await ctx.answer(work())
