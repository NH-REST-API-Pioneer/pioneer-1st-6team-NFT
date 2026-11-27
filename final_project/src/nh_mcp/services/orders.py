"""주문 유스케이스: 미리보기, 주문, 취소, 응답을 못 받은 주문의 대조.

- NH로 신규 주문을 보내는 곳은 `_submit` 하나이고 `_submit`을 부르는 곳은 `place`·`place_auto`
  둘이다. `place`는 토큰을 소진한 뒤 리스크 규칙(`guard.risk.evaluate`)을 다시 돌려 통과해야
  보낸다. `place_auto`(실행 중인 전략)는 같은 리스크 규칙에 승인 범위 규칙
  (`guard.strategy_scope.evaluate_scope`)을 더 지나야 보낸다. 사람 승인은 `start_strategy` 한 번.
- 취소를 보내는 곳은 `cancel` 하나다. 전략은 `cancel_run_order`로 자기 주문만 취소한다.
- 응답을 받지 못한 주문(시간 초과, HTTP 5xx·408)은 `UNKNOWN`으로 두고 다시 보내지 않는다.
  오늘 주문 내역과 맞춰 보아 하나로 정해질 때만 주문번호를 붙인다. 그 종목에 `UNKNOWN`이
  남으면 늘리는 주문은 막는다. 잠금 안에서 보이는 `SUBMITTED`(전송 중에 멈춘 행)도 똑같이 다룬다.
- 같은 사용자의 미리보기·주문·대조는 사용자별 잠금으로 하나씩 처리한다. 승인 창은 잠금 밖이다.
- 옵션(콜·풋) 주문은 `_check`가 옵션 장부(`OptionBook`)와 "옵션"·"옵션 매도" 스위치를 더 넘긴다.
  미리보기와 승인 창에는 새 매수·새 매도·청산과 매수 프리미엄을 보여 주고, 주문 직전 재검사에서
  그것이 바뀌었으면 보내지 않는다.
"""

import asyncio
import hashlib
import json
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal, cast

from nh_mcp.config import ENV
from nh_mcp.domain import (
    KST,
    AccountState,
    Clock,
    Contract,
    JsonValue,
    OptionBook,
    Order,
    OrderRequest,
    OrderType,
    Quote,
    RiskDecision,
    RiskInput,
    RunSnapshot,
    ScopeInput,
    Side,
    UserId,
)
from nh_mcp.guard.confirm import judge
from nh_mcp.guard.env import orders_allowed
from nh_mcp.guard.risk import (
    DEMO_ONLY,
    LIMITS_NOT_SET,
    OPTION_KINDS,
    ORDER_FORMAT,
    TRADING_HOURS,
    evaluate,
    premium,
)
from nh_mcp.guard.strategy_scope import evaluate_scope, net_position
from nh_mcp.markets.domestic import Ack, DomesticMarket, Fill, NHOrderState, NoSuchSymbol, OrderRow
from nh_mcp.markets.domestic_master import MasterUnavailable
from nh_mcp.markets.domestic_products import DomesticProducts
from nh_mcp.nh.client import NHError, Rejected
from nh_mcp.nh.clients import KeyNotRegistered
from nh_mcp.nh.limits import UnknownApi
from nh_mcp.services.approval import Approval
from nh_mcp.services.holdings import HoldingService
from nh_mcp.services.queries import BrokerRejected, Unavailable, UnknownSymbol, call_nh
from nh_mcp.store import audit, confirm_tokens, features, limits, orders
from nh_mcp.store.confirm_tokens import NewToken
from nh_mcp.store.orders import OrderStatus, Outcome, StoredOrder

ORDER_TTL = timedelta(minutes=2)
# 이 HTTP 상태로 거부되면 NH 안쪽에서 처리됐는지 알 수 없다(서버 오류, 시간 초과).
HTTP_REQUEST_TIMEOUT = 408
HTTP_SERVER_ERROR_FROM = 500
UNKNOWN_RULE = "unknown_state"
WARNING_CHANGED = "warning_changed"
# 이 예외로 끝난 시세·주문가능수량 조회는 값을 모르는 것(None)으로 둔다.
NOT_READ = (NHError, UnknownApi, KeyNotRegistered, NoSuchSymbol)

SIDE_NAMES: dict[Side, str] = {"buy": "매수", "sell": "매도"}
TYPE_NAMES: dict[OrderType, str] = {"limit": "지정가", "market": "시장가"}
STATUSES: dict[NHOrderState, OrderStatus] = {
    "accepted": "ACCEPTED",
    "filled": "FILLED",
    "cancelled": "CANCELLED",
    "rejected": "REJECTED",
}
NO_ANSWER = (
    "주문 응답을 받지 못했습니다. 처리됐는지 알 수 없으니 다시 주문하지 말고 "
    "get_orders로 확인하세요."
)
NO_CANCEL_ANSWER = (
    "취소 응답을 받지 못했습니다. 처리됐는지 알 수 없으니 다시 취소하지 말고 "
    "get_orders로 확인하세요."
)
NOT_RECORDED = (
    "주문을 보낸 뒤 결과를 기록하지 못했습니다. 처리됐는지 알 수 없으니 다시 주문하지 말고 "
    "get_orders로 확인하세요."
)
UNKNOWN_HELD = (
    "이 종목에 응답을 받지 못한 주문이 있어 포지션을 늘리는 주문은 막습니다. "
    "get_orders로 확인하세요."
)

type OptionAction = Literal["new_buy", "new_sell", "close"]
OPTION_ACTIONS: dict[OptionAction, str] = {
    "new_buy": "새 매수",
    "new_sell": "새 매도",
    "close": "청산",
}


class LimitsNotSet(Exception):
    """리스크 한도가 없다."""


class RiskBlocked(Exception):
    def __init__(self, rule: str, message: str) -> None:
        super().__init__(message)
        self.rule = rule
        self.message = message


class MarketClosed(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ConfirmInvalid(Exception):
    """확인 토큰이 없거나 남의 것이거나 만료됐거나 이미 썼다(구별해 알려 주지 않는다)."""


class NotApproved(Exception):
    """사람이 승인 창에서 거절했다. 토큰은 소진됐다."""


class UnknownState(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class OrdersDisabled(Exception):
    """주문 스위치가 꺼져 있다(`guard.env.orders_allowed`)."""


class InvalidCancel(Exception):
    """취소할 수 있는 주문이 아니다: 오늘 주문 내역에 없거나 남은 수량이 없다."""

    def __init__(self, order_no: str) -> None:
        super().__init__(order_no)
        self.order_no = order_no


class NotRunOrder(Exception):
    """그 전략이 낸, 주문번호를 받은 행이 아니다(`cancel_run_order`). 아무것도 보내지 않았다."""

    def __init__(self, order_id: int) -> None:
        super().__init__(order_id)
        self.order_id = order_id


@dataclass(frozen=True)
class OptionTerms:
    """옵션 주문을 사람에게 보여 줄 때 더하는 것. 청산은 포지션을 늘리지 않는 주문이다.

    premium은 매수의 가격·승수·수량의 곱(원, 시장가는 상한가로 셈)이다. 매도이거나 셀 수 없으면
    None.
    """

    action: OptionAction
    premium: Decimal | None


@dataclass(frozen=True)
class Preview:
    order: OrderRequest
    contract_name: str
    warnings: list[str]
    option: OptionTerms | None
    confirm_token: str
    expires_at: datetime


@dataclass(frozen=True)
class PendingOrder:
    """승인 창에 띄울 것. `message`는 저장된 주문으로 서버가 만든 문구, `state`는 토큰의 SHA-256."""

    order: OrderRequest
    contract_name: str
    warnings: list[str]
    message: str
    state: str


@dataclass(frozen=True)
class _Ticket:
    """확인 토큰에 담긴 주문. option은 옵션 주문일 때만 있다."""

    order: OrderRequest
    contract_name: str
    warnings: list[str]
    option: OptionTerms | None


def _plain(value: Decimal) -> str:
    return format(value, "f")


def _order_json(order: OrderRequest) -> dict[str, JsonValue]:
    return {
        "market": order.market,
        "symbol": order.symbol,
        "side": order.side,
        "type": order.type,
        "quantity": order.quantity,
        "price": None if order.price is None else _plain(order.price),
    }


def _option_json(option: OptionTerms | None) -> JsonValue:
    if option is None:
        return None
    cost = None if option.premium is None else _plain(option.premium)
    return {"action": option.action, "premium": cost}


def _payload(
    order: OrderRequest, contract_name: str, warnings: list[str], option: OptionTerms | None
) -> str:
    values: dict[str, JsonValue] = {
        **_order_json(order),
        "name": contract_name,
        "warnings": list[JsonValue](warnings),
        "option": _option_json(option),
    }
    return json.dumps(values, sort_keys=True, ensure_ascii=False)


def _object(raw: object) -> dict[object, object] | None:
    return cast(dict[object, object], raw) if isinstance(raw, dict) else None


def _strings(raw: object) -> list[str] | None:
    if not isinstance(raw, list):
        return None
    items: list[str] = []
    for item in cast(list[object], raw):
        if not isinstance(item, str):
            return None
        items.append(item)
    return items


def _price(raw: object) -> Decimal | None:
    """저장된 가격. 없으면 None, 읽을 수 없으면 ValueError."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError("가격이 문자열이 아닙니다.")
    try:
        value = Decimal(raw)
    except ArithmeticError:
        raise ValueError("가격을 읽을 수 없습니다.") from None
    if not value.is_finite():
        raise ValueError("가격을 읽을 수 없습니다.")
    return value


def _option_of(values: dict[object, object]) -> OptionTerms | None:
    """저장된 옵션 칸. 선물 주문은 None. 칸이 없거나 모양·값이 주문과 맞지 않으면 ValueError."""
    if "option" not in values:
        raise ValueError("옵션 칸이 없습니다.")
    raw = values["option"]
    if raw is None:
        return None
    terms = _object(raw)
    if terms is None or set(terms) != {"action", "premium"}:
        raise ValueError("옵션 칸의 모양이 다릅니다.")
    action = next((a for a in OPTION_ACTIONS if a == terms["action"]), None)
    cost = _price(terms["premium"])
    side = values.get("side")
    opening: OptionAction = "new_buy" if side == "buy" else "new_sell"
    if action not in ("close", opening) or (side != "buy" and cost is not None):
        raise ValueError("옵션 칸이 주문과 맞지 않습니다.")
    return OptionTerms(action, cost)


def _parse(payload: str) -> _Ticket | None:
    """우리가 쓴 payload를 다시 읽는다. 모양이 다르면 None(토큰을 쓸 수 없다)."""
    try:
        values = _object(json.loads(payload))
        price = None if values is None else _price(values.get("price"))
        option = None if values is None else _option_of(values)
    except ValueError:
        return None
    if values is None:
        return None
    symbol, name, quantity = values.get("symbol"), values.get("name"), values.get("quantity")
    side, kind = values.get("side"), values.get("type")
    warnings = _strings(values.get("warnings"))
    if (
        values.get("market") != "domestic"
        or not isinstance(symbol, str)
        or not isinstance(name, str)
        or type(quantity) is not int
        or side not in ("buy", "sell")
        or kind not in ("limit", "market")
        or warnings is None
    ):
        return None
    order = OrderRequest(
        "domestic",
        symbol,
        "buy" if side == "buy" else "sell",
        "limit" if kind == "limit" else "market",
        quantity,
        price,
    )
    return _Ticket(order, name, warnings, option)


def _option_terms(
    order: OrderRequest, contract: Contract, decision: RiskDecision
) -> OptionTerms | None:
    """옵션 주문이면 새 매수·새 매도·청산과 매수 프리미엄. 선물은 None."""
    if contract.kind not in OPTION_KINDS:
        return None
    if order.side == "sell":
        return OptionTerms("new_sell" if decision.increases_position else "close", None)
    action: OptionAction = "new_buy" if decision.increases_position else "close"
    return OptionTerms(action, premium(order, contract))


def _option_label(option: OptionTerms | None) -> str:
    if option is None:
        return "옵션 아님"
    cost = "" if option.premium is None else f"(프리미엄 {_plain(option.premium)}원)"
    return OPTION_ACTIONS[option.action] + cost


def _option_line(order: OrderRequest, option: OptionTerms) -> str:
    line = f"옵션 {OPTION_ACTIONS[option.action]}입니다."
    if option.action == "new_sell":
        return line + " 보유하지 않은 옵션을 팔아 손실이 크게 날 수 있습니다."
    if order.side == "sell":
        return line
    if option.premium is None:
        return line + " 상한가를 몰라 프리미엄을 셀 수 없습니다."
    if order.type == "market":
        return line + f" 프리미엄은 많아야 {_plain(option.premium)}원입니다(시장가라 상한가로 셈)."
    return line + f" 프리미엄 {_plain(option.premium)}원(가격·승수·수량의 곱)."


def _message(ticket: _Ticket) -> str:
    order = ticket.order
    price = "" if order.price is None else f", 가격 {_plain(order.price)}"
    lines = [
        f"모의 계좌에 주문을 냅니다. {ticket.contract_name}({order.symbol}) "
        f"{SIDE_NAMES[order.side]} {TYPE_NAMES[order.type]} {order.quantity}계약{price}."
    ]
    if ticket.option is not None:
        lines.append(_option_line(order, ticket.option))
    lines += [f"주의: {warning}" for warning in ticket.warnings]
    lines.append("승인할까요?")
    return "\n".join(lines)


def _blocked(decision: RiskDecision) -> Exception:
    if decision.rule == LIMITS_NOT_SET:
        return LimitsNotSet()
    if decision.rule == TRADING_HOURS:
        return MarketClosed(decision.message)
    if decision.rule == DEMO_ONLY:
        return OrdersDisabled()
    return RiskBlocked(decision.rule or ORDER_FORMAT, decision.message)


def _failure(exc: Exception) -> dict[str, JsonValue]:
    """감사 로그에 남길 실패 내용. 예외 메시지는 넣지 않는다(이름과 규칙, NH 코드만)."""
    rules: dict[type[Exception], str] = {
        LimitsNotSet: LIMITS_NOT_SET,
        MarketClosed: TRADING_HOURS,
        UnknownState: UNKNOWN_RULE,
        OrdersDisabled: DEMO_ONLY,
    }
    if isinstance(exc, RiskBlocked):
        return {"outcome": "blocked", "rule": exc.rule}
    if type(exc) in rules:
        return {"outcome": "blocked", "rule": rules[type(exc)]}
    if isinstance(exc, ConfirmInvalid):
        return {"outcome": "invalid_token"}
    if isinstance(exc, InvalidCancel):
        return {"outcome": "invalid"}
    if isinstance(exc, BrokerRejected):
        return {"outcome": "rejected", "msg_cd": exc.code}
    return {"outcome": "error", "error": type(exc).__name__}


def order_no_key(order_no: str) -> str:
    """비교용 주문번호. 앞뒤 공백과 앞자리 0을 뗀다. 빈 값은 빈 값, 0만 있으면 "0".

    주문 응답(`ord_no`)과 주문 내역(`occurs1_ord_no`)이 0 채움을 다르게 할 수 있다(Task 9 확인).
    저장과 전송에는 NH가 보낸 그대로를 쓰고, 비교할 때만 이 값을 쓴다.
    """
    text = order_no.strip()
    if not text:
        return ""
    return text.lstrip("0") or "0"


def same_order_no(a: str, b: str) -> bool:
    """같은 주문번호인가. 빈 번호는 무엇과도 같지 않다."""
    key = order_no_key(a)
    return bool(key) and key == order_no_key(b)


def _new_warnings(ticket: _Ticket, decision: RiskDecision) -> list[str]:
    return [warning for warning in decision.warnings if warning not in ticket.warnings]


def _todays_limit(contract: Contract, quote: Quote | None) -> Contract:
    """시장가 매수를 셀 상한가를 넣은 종목: 오늘 시세의 상한가와 마스터의 상한가 중 큰 값.

    마스터는 하루 한 번 받고 전날 파일로 물러설 수 있는데, 옵션 상한가는 날마다 바뀐다. 둘 다
    모르면 None이고, 그러면 늘리는 시장가 매수는 프리미엄을 셀 수 없어 막힌다.
    """
    live = None if quote is None else quote.upper_limit
    known = [limit for limit in (live, contract.upper_limit) if limit is not None]
    return replace(contract, upper_limit=max(known, default=None))


def _at_market(order: Order) -> bool:
    """주문가가 없거나 0인 미체결(시장가). 매수는 상한가로 센다."""
    return order.price is None or order.price <= 0


def _book(
    account: AccountState, fills: list[Fill], contracts: Mapping[str, Contract]
) -> OptionBook | None:
    """모든 옵션 종목의 장부. contracts에는 계좌와 체결의 모든 종목이 있다.

    미체결은 종목·방향마다 반대 방향 보유 수량을 넘는 만큼만 늘리는 주문으로 센다(다 체결된 뒤
    포지션이 늘어나는 양). 시장가 미체결 매수는 contracts의 상한가로 센다(`_option_book`이 오늘
    시세와 견준 값을 넣는다). 그 상한가를 모르거나 옵션 매수 체결가가 0 이하이면 None.
    """
    options = {symbol for symbol, c in contracts.items() if c.kind in OPTION_KINDS}
    held = [p for p in account.positions if p.symbol in options]
    resting = [o for o in account.open_orders if o.symbol in options]
    pending: dict[Side, int] = {"buy": 0, "sell": 0}
    for symbol in {o.symbol for o in resting}:
        for side in pending:
            waiting = sum(
                o.remaining_quantity for o in resting if o.symbol == symbol and o.side == side
            )
            against = sum(p.quantity for p in held if p.symbol == symbol and p.side != side)
            pending[side] += max(0, waiting - against)
    pending_cost = Decimal(0)
    for o in resting:
        if o.side == "sell":
            continue
        contract = contracts[o.symbol]
        price = contract.upper_limit if _at_market(o) else o.price
        if price is None:
            return None
        pending_cost += price * contract.multiplier * o.remaining_quantity
    spent = Decimal(0)
    for fill in fills:
        if fill.symbol not in options or fill.side == "sell":
            continue
        if fill.price <= 0:
            return None
        spent += fill.price * contracts[fill.symbol].multiplier * fill.quantity
    return OptionBook(
        long_total=sum(p.quantity for p in held if p.side == "buy"),
        short_total=sum(p.quantity for p in held if p.side == "sell"),
        pending_increase=pending["buy"] + pending["sell"],
        pending_short=pending["sell"],
        premium_spent=spent,
        premium_pending=pending_cost,
    )


def _undecided(exc: Rejected) -> bool:
    """처리됐는지 모르는 거부인가.

    5xx·408은 모른다. 200 + 오류 코드, 빈 주문번호, 그 밖의 4xx는 거부다.
    """
    return exc.http_status >= HTTP_SERVER_ERROR_FROM or exc.http_status == HTTP_REQUEST_TIMEOUT


def _matches(stored: StoredOrder, row: OrderRow, today: date, bound: set[str]) -> bool:
    """`UNKNOWN` 행과 같은 주문으로 볼 수 있는 주문 내역 행인가. `bound`는 `order_no_key` 값."""
    nh = row.order
    number = order_no_key(nh.order_no)
    if row.division != "new" or row.received is None or not number or number in bound:
        return False
    if (nh.symbol, nh.side, nh.quantity) != (stored.symbol, stored.side, stored.quantity):
        return False
    if stored.type == "limit" and nh.price != stored.price:
        return False
    # 접수 시각은 초 단위라 보낸 시각도 초로 자른다.
    sent = stored.submitted_at.astimezone(KST).replace(microsecond=0)
    return datetime.combine(today, row.received, KST) >= sent


class OrderService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        clock: Clock,
        domestic: DomesticMarket,
        products: DomesticProducts,
        orders_enabled: bool,
    ) -> None:
        self._conn = conn
        self._clock = clock
        self._domestic = domestic
        self._products = products
        self._orders_enabled = orders_enabled
        self._holdings = HoldingService(conn, domestic)
        self._locks: dict[UserId, asyncio.Lock] = {}

    async def preview(self, user_id: UserId, order: OrderRequest) -> Preview:
        """검사를 통과하면 2분짜리 확인 토큰을 발급한다. NH로 주문은 보내지 않는다."""
        self._require_enabled()
        started = time.perf_counter_ns()
        detail: dict[str, JsonValue] = {"order": _order_json(order)}
        try:
            async with self._lock(user_id):
                contract, decision, _ = await self._check(user_id, order)
                option = _option_terms(order, contract, decision)
                payload = _payload(order, contract.name, decision.warnings, option)
                new = NewToken(user_id, "order", payload)
                token, expires_at = confirm_tokens.issue(self._conn, new, self._clock(), ORDER_TTL)
        except Exception as exc:
            self._audit(user_id, "order.preview", detail | _failure(exc), started)
            raise
        issued: dict[str, JsonValue] = {"outcome": "issued", "warnings": list(decision.warnings)}
        self._audit(user_id, "order.preview", detail | issued, started)
        return Preview(order, contract.name, decision.warnings, option, token, expires_at)

    async def pending(self, user_id: UserId, token: str) -> PendingOrder:
        """토큰의 주문을 승인 창에 띄울 모양으로. 토큰을 소진하지 않는다."""
        self._require_enabled()
        ticket = self._ticket(user_id, token)
        state = hashlib.sha256(token.encode()).hexdigest()
        return PendingOrder(
            ticket.order, ticket.contract_name, ticket.warnings, _message(ticket), state
        )

    async def place(self, user_id: UserId, token: str, approval: Approval) -> StoredOrder:
        """토큰 확인 → (거절이면 소진하고 끝) → 소진 → 다시 검사 → 전송."""
        self._require_enabled()
        started = time.perf_counter_ns()
        try:
            ticket = self._ticket(user_id, token)
        except ConfirmInvalid as exc:
            self._audit(user_id, "order.place", _failure(exc), started)
            raise
        detail: dict[str, JsonValue] = {"order": _order_json(ticket.order)}
        self._audit(user_id, "order.approve", detail | {"outcome": approval}, started)
        if approval == "declined":
            # AI가 같은 토큰으로 승인 창을 되풀이해 띄우지 못하게 소진한다.
            confirm_tokens.consume(self._conn, token, user_id, "order", self._clock())
            raise NotApproved()
        async with self._lock(user_id):
            try:
                if not confirm_tokens.consume(self._conn, token, user_id, "order", self._clock()):
                    raise ConfirmInvalid()
                contract, decision, _ = await self._check(user_id, ticket.order)
                # 사람이 보지 못한 경고(예: 확인 뒤 종가 단일가 구간)가 생기면 보내지 않는다.
                new = _new_warnings(ticket, decision)
                if new:
                    raise RiskBlocked(
                        WARNING_CHANGED,
                        f"주문을 확인한 뒤 새 경고가 생겼습니다: {' '.join(new)} "
                        "다시 미리보기 해 주세요.",
                    )
                # 승인 창에 "청산"으로 보인 매도가 새 매도로 나가는 일 등을 막는다.
                option = _option_terms(ticket.order, contract, decision)
                if option != ticket.option:
                    raise RiskBlocked(
                        WARNING_CHANGED,
                        "주문을 확인한 뒤 옵션 주문이 바뀌었습니다: "
                        f"{_option_label(ticket.option)} → {_option_label(option)}. "
                        "다시 미리보기 해 주세요.",
                    )
            except Exception as exc:
                self._audit(user_id, "order.place", detail | _failure(exc), started)
                raise
            try:
                stored = await self._submit(user_id, ticket.order, detail, started, run_id=None)
            except asyncio.CancelledError:
                cancelled: dict[str, JsonValue] = {"outcome": "unknown", "cancelled": True}
                self._audit(user_id, "order.place", detail | cancelled, started)
                raise
        return self._placed(user_id, stored, detail, started)

    async def place_auto(
        self, user_id: UserId, run: RunSnapshot, order: OrderRequest
    ) -> StoredOrder:
        """실행 중인 전략의 주문. 검사 → 승인 범위 → 전송. 확인 토큰과 주문마다의 승인 창은 없다.

        사람 승인은 `start_strategy` 승인 창 한 번이고, 그 범위가 `run.scope`다. 응답을 받지 못하면
        `UnknownState`로 알리고 다시 보내지 않는다(부른 쪽이 전략을 멈춘다).
        """
        self._require_enabled()
        started = time.perf_counter_ns()
        detail: dict[str, JsonValue] = {"order": _order_json(order), "run_id": run.run_id}
        async with self._lock(user_id):
            try:
                _, decision, account = await self._check(user_id, order)
                position = net_position(account.positions, order.symbol)
                scope = evaluate_scope(ScopeInput(run, order, position, self._clock()))
                if scope.rule is not None:
                    raise RiskBlocked(scope.rule, scope.message)
            except Exception as exc:
                self._audit(user_id, "order.place", detail | _failure(exc), started)
                raise
            # 경고(시장가, 종가 단일가, 만기 임박)는 막지 않고 감사 로그에 남긴다. 주문마다 사람이
            # 보는 미리보기가 없어 `warning_changed`(본 것과 다르면 막기)는 뜻이 없다. 셋 다 켤 때
            # 알 수 있는 것이라 `start_strategy` 승인 창에서 한 번 보여 준다.
            shown: dict[str, JsonValue] = {"warnings": list[JsonValue](decision.warnings)}
            warned = detail | shown
            try:
                stored = await self._submit(user_id, order, warned, started, run_id=run.run_id)
            except asyncio.CancelledError:
                cancelled: dict[str, JsonValue] = {"outcome": "unknown", "cancelled": True}
                self._audit(user_id, "order.place", warned | cancelled, started)
                raise
        return self._placed(user_id, stored, warned, started)

    async def cancel_run_order(self, user_id: UserId, run_id: str, order_id: int) -> Ack:
        """실행 중인 전략이 자기 주문을 취소한다. `order_id`는 `place_auto`가 돌려준 행 번호다.

        그 전략이 낸 행이 아니거나(수동 주문, 다른 전략) 주문번호가 없으면 보내지 않는다.
        """
        self._require_enabled()
        row = orders.get(self._conn, user_id, order_id)
        if row is None or row.run_id != run_id or row.nh_order_no is None:
            refused: dict[str, JsonValue] = {
                "order_id": order_id,
                "run_id": run_id,
                "outcome": "not_run_order",
            }
            self._audit(user_id, "order.cancel", refused, time.perf_counter_ns())
            raise NotRunOrder(order_id)
        return await self.cancel(user_id, row.nh_order_no)

    async def cancel(self, user_id: UserId, order_no: str) -> Ack:
        """오늘 주문 내역에서 그 주문을 찾아 남은 수량 전부를 취소한다. 확인 토큰은 없다."""
        self._require_enabled()
        started = time.perf_counter_ns()
        detail: dict[str, JsonValue] = {"order_no": order_no}
        try:
            target = await self._cancellable(user_id, order_no)
        except Exception as exc:
            self._audit(user_id, "order.cancel", detail | _failure(exc), started)
            raise
        try:
            ack = await self._domestic.send_cancel(user_id, target)
        except Rejected as exc:
            if _undecided(exc):
                self._audit(user_id, "order.cancel", detail | {"outcome": "unknown"}, started)
                raise UnknownState(NO_CANCEL_ANSWER) from None
            rejected: dict[str, JsonValue] = {"outcome": "rejected", "msg_cd": exc.code}
            self._audit(user_id, "order.cancel", detail | rejected, started)
            raise BrokerRejected(exc.code, exc.message) from None
        except Exception:
            # 응답을 못 받았다. 취소됐는지 모르므로 다시 보내지 않는다.
            self._audit(user_id, "order.cancel", detail | {"outcome": "unknown"}, started)
            raise UnknownState(NO_CANCEL_ANSWER) from None
        accepted: dict[str, JsonValue] = {
            "outcome": "accepted",
            "cancel_no": ack.order_no,
            "msg_cd": ack.msg_cd,
        }
        self._audit(user_id, "order.cancel", detail | accepted, started)
        return ack

    async def reconcile(self, user_id: UserId) -> None:
        """오늘 `UNKNOWN` 주문을 주문 내역과 맞춰 본다. 하나로 정해질 때만 주문번호를 붙인다."""
        async with self._lock(user_id):
            await self._reconcile(user_id)

    # ---- 안쪽 -----------------------------------------------------------

    def _lock(self, user_id: UserId) -> asyncio.Lock:
        lock = self._locks.get(user_id)
        if lock is None:
            lock = self._locks[user_id] = asyncio.Lock()
        return lock

    def _require_enabled(self) -> None:
        if not orders_allowed(ENV, self._orders_enabled):
            raise OrdersDisabled()

    def _today(self) -> date:
        return self._clock().astimezone(KST).date()

    def _ticket(self, user_id: UserId, token: str) -> _Ticket:
        row = confirm_tokens.find(self._conn, token)
        if judge(row, user_id, "order", self._clock()) != "usable" or row is None:
            raise ConfirmInvalid()
        ticket = _parse(row.payload)
        if ticket is None:
            raise ConfirmInvalid()
        return ticket

    async def _check(
        self, user_id: UserId, order: OrderRequest
    ) -> tuple[Contract, RiskDecision, AccountState]:
        """데이터를 모아 규칙을 돌린다. 막히면 예외. 잠금 안에서만 부른다.

        돌려주는 계좌는 다 읽은 것이다(못 읽은 행이 있으면 `account_unknown`으로 막힌다).
        """
        if order.market != "domestic":
            raise RiskBlocked(ORDER_FORMAT, "국내(domestic) 선물과 옵션만 주문할 수 있습니다.")
        await self._reconcile(user_id, tolerate_failure=True)
        # 이 검사에서 읽은 시세. 시장가 주문과 시장가 미체결 옵션 매수의 종목마다 한 번만 읽는다.
        quotes: dict[str, Quote | None] = {}
        master = await self._contract(order.symbol)
        live = await self._quote(user_id, order.symbol, quotes) if order.type == "market" else None
        contract = _todays_limit(master, live)
        rules = await self._products.rules_for(user_id, contract)
        user_limits = limits.get(self._conn, user_id, order.market)
        # 계좌를 읽지 못하면 규칙을 돌리지 않는다(Unavailable 등으로 바로 끝난다).
        account = await call_nh(self._domestic.account(user_id))
        # 옵션 보유 기록을 맞춘다(다 읽은 계좌일 때만). 막히는 주문의 점검에서도 맞춘다.
        await self._holdings.record(user_id, account)
        orderable = await self._orderable(user_id, order, live)
        option = contract.kind in OPTION_KINDS
        decision = evaluate(
            RiskInput(
                order=order,
                env=ENV,
                instrument=rules,
                limits=user_limits,
                account=account,
                orderable_qty=orderable,
                now=self._clock(),
                orders_enabled=self._orders_enabled,
                options_enabled=features.enabled(self._conn, user_id, "options"),
                option_sell=features.enabled(self._conn, user_id, "option_sell"),
                option_book=await self._option_book(user_id, account, quotes) if option else None,
            )
        )
        if not decision.allowed:
            raise _blocked(decision)
        if decision.increases_position and self._unknown_today(user_id, order.symbol):
            raise UnknownState(UNKNOWN_HELD)
        return contract, decision, account

    async def _contract(self, symbol: str) -> Contract:
        try:
            contract = await self._domestic.contract(symbol)
        except MasterUnavailable:
            raise Unavailable() from None
        if contract is None:
            raise UnknownSymbol(symbol)
        return contract

    async def _quote(
        self, user_id: UserId, symbol: str, quotes: dict[str, Quote | None]
    ) -> Quote | None:
        """그 종목의 오늘 시세. 읽지 못하면 None. quotes에 이미 있으면 다시 읽지 않는다."""
        if symbol not in quotes:
            try:
                quotes[symbol] = await self._domestic.quote(user_id, symbol)
            except NOT_READ:
                quotes[symbol] = None
        return quotes[symbol]

    async def _orderable(
        self, user_id: UserId, order: OrderRequest, quote: Quote | None
    ) -> int | None:
        """주문 가능 수량. 시장가는 오늘 시세(quote)의 상한가로 묻는다.

        모르면 None이고, 그러면 규칙 7(증거금)이 늘리는 주문을 막는다.
        """
        upper_limit = None if quote is None else quote.upper_limit
        try:
            return await self._domestic.orderable_qty(user_id, order, upper_limit)
        except NOT_READ:
            return None

    async def _option_book(
        self, user_id: UserId, account: AccountState, quotes: dict[str, Quote | None]
    ) -> OptionBook | None:
        """옵션 규칙이 볼 장부. 오늘 체결을 읽지 못하거나 계좌·체결의 종목이 마스터에 없거나, 시장가
        미체결 옵션 매수의 시세를 읽지 못하면 None.

        시장가 미체결 매수는 오늘 시세와 마스터의 상한가 중 큰 값으로 센다(`_todays_limit`).
        None이면 늘리는 옵션 주문이 `option_book_unknown`으로 막히고, 청산은 지나간다.
        """
        try:
            fills = await self._domestic.todays_fills(user_id)
        except (NHError, UnknownApi, KeyNotRegistered):
            return None
        if fills is None:
            return None
        symbols = (
            {p.symbol for p in account.positions}
            | {o.symbol for o in account.open_orders}
            | {f.symbol for f in fills}
        )
        contracts: dict[str, Contract] = {}
        for symbol in symbols:
            try:
                found = await self._domestic.contract(symbol)
            except MasterUnavailable:
                return None
            if found is None:
                return None
            contracts[symbol] = found
        market_buys = {
            o.symbol
            for o in account.open_orders
            if o.side == "buy" and _at_market(o) and contracts[o.symbol].kind in OPTION_KINDS
        }
        for symbol in market_buys:
            quote = await self._quote(user_id, symbol, quotes)
            if quote is None:
                return None
            contracts[symbol] = _todays_limit(contracts[symbol], quote)
        return _book(account, fills, contracts)

    def _unknown_today(self, user_id: UserId, symbol: str) -> bool:
        """오늘 낸, 나갔는지 모르는 주문(UNKNOWN·멈춘 SUBMITTED)이 그 종목에 있는가.

        전날 것은 막지 않는다(주문 내역이 하루치).
        """
        today = self._today()
        stuck = orders.unsettled(self._conn, user_id, symbol)
        return any(row.submitted_at.astimezone(KST).date() == today for row in stuck)

    async def _reconcile(self, user_id: UserId, *, tolerate_failure: bool = False) -> None:
        """`tolerate_failure`면 주문 내역을 읽지 못해도 멈추지 않는다(미리보기·주문 재검사).

        행은 그대로 남아 그 종목의 늘리는 주문을 계속 막고, 줄이는 주문은 지나갈 수 있다.
        """
        today = self._today()
        stuck = [
            row
            for row in orders.unsettled(self._conn, user_id)
            if row.submitted_at.astimezone(KST).date() == today
        ]
        if not stuck:
            return
        started = time.perf_counter_ns()
        try:
            history = await call_nh(self._domestic.todays_orders(user_id))
        except (Unavailable, BrokerRejected) as exc:
            if not tolerate_failure:
                raise
            failed: dict[str, JsonValue] = {
                "outcome": "failed",
                "stuck": len(stuck),
                "error": type(exc).__name__,
            }
            self._audit(user_id, "order.reconcile", failed, started)
            return
        bound = {order_no_key(n) for n in orders.bound_order_numbers(self._conn, user_id)}
        for row in stuck:
            found = [candidate for candidate in history if _matches(row, candidate, today, bound)]
            detail: dict[str, JsonValue] = {"order_id": row.id, "candidates": len(found)}
            if len(found) == 1:
                match = found[0]
                status = STATUSES[match.state]
                self._update(user_id, row.id, Outcome(status, nh_order_no=match.order.order_no))
                bound.add(order_no_key(match.order.order_no))
                detail |= {"outcome": "matched", "status": status}
            else:
                detail["outcome"] = "ambiguous" if found else "unmatched"
            self._audit(user_id, "order.reconcile", detail, started)

    async def _submit(
        self,
        user_id: UserId,
        order: OrderRequest,
        detail: dict[str, JsonValue],
        started: int,
        *,
        run_id: str | None,
    ) -> StoredOrder:
        """NH로 신규 주문을 보내는 유일한 곳. SUBMITTED를 먼저 쓰고, 응답으로 상태를 바꾼다.

        `run_id`는 자동 주문을 낸 전략 실행(수동은 None)이고 주문 행에 함께 쓴다.

        응답을 받지 못하면 UNKNOWN으로 두고 다시 보내지 않는다. SUBMITTED를 쓰지 못하면
        보내지 않는다. 보낸 뒤 결과 기록이나 다시 읽기에 실패하면 감사 로그에 unknown을 남기고
        UnknownState로 알린다. 갱신하지 못한 행은 SUBMITTED로 남아 나갔는지 모르는 행이 된다.
        """
        order_id = orders.insert_submitted(self._conn, user_id, order, self._clock(), run_id)
        try:
            ack = await self._domestic.send_order(user_id, order)
        except Rejected as exc:
            if _undecided(exc):
                outcome = Outcome("UNKNOWN", msg_cd=exc.code or None, msg_cntn=exc.message or None)
            else:
                outcome = Outcome("REJECTED", msg_cd=exc.code or None, msg_cntn=exc.message or None)
        except Exception:
            # 응답을 못 받았거나 읽지 못했다. 나갔는지 모른다.
            outcome = Outcome("UNKNOWN")
        except BaseException:
            # 기다리는 중에 취소됐다. 나갔는지 모른다. SUBMITTED로 두면 대조되지 않는다.
            self._update(user_id, order_id, Outcome("UNKNOWN"))
            raise
        else:
            outcome = Outcome(
                "ACCEPTED",
                nh_order_no=ack.order_no,
                msg_cd=ack.msg_cd or None,
                msg_cntn=ack.msg_cntn or None,
            )
        try:
            self._update(user_id, order_id, outcome)
            stored = orders.get(self._conn, user_id, order_id)
            if stored is None:
                raise RuntimeError("방금 쓴 주문 행을 읽지 못했습니다.")
        except Exception as exc:
            lost: dict[str, JsonValue] = {
                "outcome": "unknown",
                "order_id": order_id,
                "error": type(exc).__name__,
            }
            self._audit(user_id, "order.place", detail | lost, started)
            raise UnknownState(NOT_RECORDED) from None
        return stored

    def _placed(
        self,
        user_id: UserId,
        stored: StoredOrder,
        detail: dict[str, JsonValue],
        started: int,
    ) -> StoredOrder:
        """전송 결과를 감사 로그에 남기고, 접수가 아니면 예외로 바꾼다."""
        result: dict[str, JsonValue] = {
            "outcome": stored.status.lower(),
            "order_id": stored.id,
            "msg_cd": stored.msg_cd,
        }
        self._audit(user_id, "order.place", detail | result, started)
        if stored.status == "UNKNOWN":
            raise UnknownState(NO_ANSWER)
        if stored.status == "REJECTED":
            raise BrokerRejected(stored.msg_cd or "", stored.msg_cntn or "")
        return stored

    async def _cancellable(self, user_id: UserId, order_no: str) -> Order:
        """오늘 주문 내역에서 그 번호의 남은 수량이 있는 주문(취소 주문 행은 빼고)."""
        if not order_no.strip():
            raise InvalidCancel(order_no)
        for row in await call_nh(self._domestic.todays_orders(user_id)):
            found = row.order
            cancellable = row.division != "cancel" and found.remaining_quantity > 0
            if same_order_no(found.order_no, order_no) and cancellable:
                return found
        raise InvalidCancel(order_no)

    def _update(self, user_id: UserId, order_id: int, outcome: Outcome) -> None:
        if not orders.update(self._conn, user_id, order_id, outcome, self._clock()):
            raise RuntimeError("주문 행을 갱신하지 못했습니다.")

    def _audit(
        self, user_id: UserId, action: str, detail: dict[str, JsonValue], started: int
    ) -> None:
        elapsed_ms = (time.perf_counter_ns() - started) // 1_000_000
        record = detail | {"elapsed_ms": elapsed_ms}
        audit.record(self._conn, user_id, action, record, self._clock())
