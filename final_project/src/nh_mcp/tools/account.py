"""계좌 도구: get_account, get_orders. 계좌번호와 계좌명은 돌려주지 않는다."""

import contextlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime

from fastmcp import FastMCP

from nh_mcp import envelope
from nh_mcp.domain import KST, ContractKind, JsonValue, Order, OrderError, Position, UserId
from nh_mcp.services.holdings import AccountView, HoldingService
from nh_mcp.services.orders import order_no_key
from nh_mcp.services.queries import BrokerRejected, KeyMissing, QueryService, Unavailable
from nh_mcp.store.option_holdings import Holding
from nh_mcp.tools.context import DOMESTIC_ONLY, ToolContext, text


def _position(p: Position, kind: ContractKind | None) -> dict[str, JsonValue]:
    return {
        "symbol": p.symbol,
        "name": p.name,
        "kind": kind,
        "side": p.side,
        "quantity": p.quantity,
        "closable": p.closable,
        "avg_price": text(p.avg_price),
        "close_price": text(p.close_price),
        "unrealized_pnl": text(p.unrealized_pnl),
    }


def _order(o: Order) -> dict[str, JsonValue]:
    return {
        "order_no": o.order_no,
        "original_order_no": o.original_order_no,
        "symbol": o.symbol,
        "name": o.name,
        "side": o.side,
        "status": o.status,
        "quantity": o.quantity,
        "price": text(o.price),
        "filled_quantity": o.filled_quantity,
        "filled_price": text(o.filled_price),
        "remaining_quantity": o.remaining_quantity,
        "reject_reason": o.reject_reason,
    }


def _order_error(e: OrderError) -> dict[str, JsonValue]:
    return {
        "received_at": e.received_at,
        "symbol": e.symbol,
        "name": e.name,
        "side": e.side,
        "code": e.code,
        "message": e.message,
        "quantity": e.quantity,
        "price": text(e.price),
    }


def _expired(h: Holding) -> dict[str, JsonValue]:
    return {
        "symbol": h.symbol,
        "side": h.side,
        "quantity": h.quantity,
        "expiry": h.expiry.isoformat(),
        "closed_at": None if h.closed_at is None else _kst(h.closed_at),
    }


def _kst(moment: datetime) -> str:
    return moment.astimezone(KST).isoformat(timespec="seconds")


def _account(view: AccountView) -> dict[str, JsonValue]:
    a = view.account
    return {
        "market": a.market,
        "currency": a.currency,
        "deposit": text(a.deposit),
        "orderable_amount": text(a.orderable_amount),
        "realized_pnl": text(a.realized_pnl),
        "unrealized_pnl": text(a.unrealized_pnl),
        "positions": [_position(p, view.kinds.get(p.symbol)) for p in a.positions],
        "open_orders": [_order(o) for o in a.open_orders],
        "expired_options": [_expired(h) for h in view.expired],
        "as_of": _kst(a.as_of),
        "complete": a.complete,
    }


# 응답을 못 받은 주문을 오늘 주문 내역과 맞춰 보는 함수(`OrderService.reconcile`).
type Reconcile = Callable[[UserId], Awaitable[None]]
# 그 사용자의 묶음 다리 주문: 비교용 주문번호 → 묶음 id(`services.combos.combo_ids`).
type ComboIds = Callable[[UserId], Mapping[str, str]]


@dataclass(frozen=True)
class OrderSources:
    """get_orders가 NH 주문 내역에 더하는 이 서버의 기록. reconcile은 주문 스위치가 켜졌을 때만."""

    combo_ids: ComboIds
    reconcile: Reconcile | None = None


# 대조가 NH 쪽 사정으로 실패해도 목록은 보여 준다. 목록 조회가 같은 오류면 그때 오류로 답한다.
_RECONCILE_SKIPPED = (Unavailable, BrokerRejected, KeyMissing)


def register_account_tools(
    mcp: FastMCP,
    ctx: ToolContext,
    queries: QueryService,
    holdings: HoldingService,
    sources: OrderSources,
) -> None:
    """`sources.reconcile`이 있으면(주문 스위치가 켜짐) get_orders가 목록 전에 대조한다."""

    @mcp.tool(annotations={"readOnlyHint": True})
    async def get_account(market: str = "domestic") -> envelope.Response:
        """예수금, 주문가능금액, 오늘 확정손익, 평가손익, 보유 포지션, 미체결 주문을 돌려준다.

        쓸 때: 주문 전에 잔고와 여력을 볼 때, 사용자가 손익이나 포지션을 물을 때.
        쓰지 않을 때: 지난 주문의 체결 여부나 거부 사유는 get_orders.
        금액은 원화 문자열이다. 모르는 값은 null로 온다.
        complete가 false면 보유나 미체결을 다 읽지 못한 것이다. 그동안 주문은 막힌다.
        positions의 kind는 future·call·put(종목 마스터에 없으면 null)이다.
        expired_options는 만기일이 지난 뒤 잔고에서 사라진 옵션(행사 또는 소멸) 중 최근 7일 것이다.
        주문 없이 사라진 것이라 사용자에게 알린다. 만기일까지 사라진 옵션은 청산으로 보고 뺀다.
        """
        if market != "domestic":
            return ctx.invalid(DOMESTIC_ONLY)

        async def work() -> dict[str, JsonValue]:
            return _account(await holdings.account(ctx.user_id()))

        return await ctx.answer(work())

    @mcp.tool(annotations={"readOnlyHint": True})
    async def get_orders(day: str | None = None, market: str = "domestic") -> envelope.Response:
        """주문 내역(체결 · 미체결 · 거부)과 주문 오류 내역을 돌려준다.

        쓸 때: 주문이 들어갔는지, 체결됐는지, 왜 거부됐는지 확인할 때.
        응답을 받지 못한 주문(UNKNOWN_STATE)은 다시 주문하기 전에 반드시 이것으로 확인한다.
        쓰지 않을 때: 잔고와 손익은 get_account.
        옵션 묶음(place_combo)의 다리 주문에는 combo_id가 있다. 묶음 밖 주문은 null이다.
        day: 주문 오류 내역을 볼 날짜(YYYY-MM-DD). 비우면 오늘.
        """
        if market != "domestic":
            return ctx.invalid(DOMESTIC_ONLY)
        today = ctx.clock().astimezone(KST).date()
        try:
            when = date.fromisoformat(day.strip()) if day and day.strip() else today
        except ValueError:
            return ctx.invalid("day는 YYYY-MM-DD로 넣어 주세요.")

        async def work() -> dict[str, JsonValue]:
            user_id = ctx.user_id()
            if sources.reconcile is not None:
                with contextlib.suppress(*_RECONCILE_SKIPPED):
                    await sources.reconcile(user_id)
            orders, errors = await queries.orders(user_id, when)
            combos = sources.combo_ids(user_id)
            rows: list[JsonValue] = [
                _order(o) | {"combo_id": combos.get(order_no_key(o.order_no))} for o in orders
            ]
            return {
                "orders": rows,
                "order_errors": [_order_error(e) for e in errors],
                "day": when.isoformat(),
            }

        return await ctx.answer(work())
