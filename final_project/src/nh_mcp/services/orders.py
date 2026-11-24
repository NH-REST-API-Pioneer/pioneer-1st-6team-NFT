"""주문 유스케이스: 미리보기, 주문, 취소, 응답을 못 받은 주문의 대조.

- NH로 신규 주문을 보내는 곳은 `_submit` 하나이고 `_submit`을 부르는 곳은 `place` 하나다.
  `place`는 토큰을 소진한 뒤 리스크 규칙(`guard.risk.evaluate`)을 다시 돌려 통과해야 보낸다.
- 취소를 보내는 곳은 `cancel` 하나다.
- 응답을 받지 못한 주문(시간 초과, HTTP 5xx·408)은 `UNKNOWN`으로 두고 다시 보내지 않는다.
  오늘 주문 내역과 맞춰 보아 하나로 정해질 때만 주문번호를 붙인다. 그 종목에 `UNKNOWN`이
  남으면 늘리는 주문은 막는다. 잠금 안에서 보이는 `SUBMITTED`(전송 중에 멈춘 행)도 똑같이 다룬다.
- 같은 사용자의 미리보기·주문·대조는 사용자별 잠금으로 하나씩 처리한다. 승인 창은 잠금 밖이다.
"""

import asyncio
import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import cast

from nh_mcp.config import ENV
from nh_mcp.domain import (
    KST,
    Clock,
    Contract,
    JsonValue,
    Order,
    OrderRequest,
    OrderType,
    RiskDecision,
    RiskInput,
    Side,
    UserId,
)
from nh_mcp.guard.confirm import judge
from nh_mcp.guard.env import orders_allowed
from nh_mcp.guard.risk import DEMO_ONLY, LIMITS_NOT_SET, ORDER_FORMAT, TRADING_HOURS, evaluate
from nh_mcp.markets.domestic import Ack, DomesticMarket, NHOrderState, NoSuchSymbol, OrderRow
from nh_mcp.markets.domestic_master import MasterUnavailable
from nh_mcp.markets.domestic_products import DomesticProducts
from nh_mcp.nh.client import NHError, Rejected
from nh_mcp.nh.clients import KeyNotRegistered
from nh_mcp.nh.limits import UnknownApi
from nh_mcp.services.approval import Approval
from nh_mcp.services.queries import BrokerRejected, Unavailable, UnknownSymbol, call_nh
from nh_mcp.store import audit, confirm_tokens, limits, orders
from nh_mcp.store.confirm_tokens import NewToken
from nh_mcp.store.orders import OrderStatus, Outcome, StoredOrder

ORDER_TTL = timedelta(minutes=2)
# 이 HTTP 상태로 거부되면 NH 안쪽에서 처리됐는지 알 수 없다(서버 오류, 시간 초과).
HTTP_REQUEST_TIMEOUT = 408
HTTP_SERVER_ERROR_FROM = 500
UNKNOWN_RULE = "unknown_state"
WARNING_CHANGED = "warning_changed"

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


@dataclass(frozen=True)
class Preview:
    order: OrderRequest
    contract_name: str
    warnings: list[str]
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
    """확인 토큰에 담긴 주문."""

    order: OrderRequest
    contract_name: str
    warnings: list[str]


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


def _payload(order: OrderRequest, contract_name: str, warnings: list[str]) -> str:
    values: dict[str, JsonValue] = {
        **_order_json(order),
        "name": contract_name,
        "warnings": list[JsonValue](warnings),
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


def _parse(payload: str) -> _Ticket | None:
    """우리가 쓴 payload를 다시 읽는다. 모양이 다르면 None(토큰을 쓸 수 없다)."""
    try:
        values = _object(json.loads(payload))
        price = None if values is None else _price(values.get("price"))
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
    return _Ticket(order, name, warnings)


def _message(ticket: _Ticket) -> str:
    order = ticket.order
    price = "" if order.price is None else f", 가격 {_plain(order.price)}"
    lines = [
        f"모의 계좌에 주문을 냅니다. {ticket.contract_name}({order.symbol}) "
        f"{SIDE_NAMES[order.side]} {TYPE_NAMES[order.type]} {order.quantity}계약{price}."
    ]
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
        self._locks: dict[UserId, asyncio.Lock] = {}

    async def preview(self, user_id: UserId, order: OrderRequest) -> Preview:
        """검사를 통과하면 2분짜리 확인 토큰을 발급한다. NH로 주문은 보내지 않는다."""
        self._require_enabled()
        started = time.perf_counter_ns()
        detail: dict[str, JsonValue] = {"order": _order_json(order)}
        try:
            async with self._lock(user_id):
                contract, decision = await self._check(user_id, order)
                payload = _payload(order, contract.name, decision.warnings)
                new = NewToken(user_id, "order", payload)
                token, expires_at = confirm_tokens.issue(self._conn, new, self._clock(), ORDER_TTL)
        except Exception as exc:
            self._audit(user_id, "order.preview", detail | _failure(exc), started)
            raise
        issued: dict[str, JsonValue] = {"outcome": "issued", "warnings": list(decision.warnings)}
        self._audit(user_id, "order.preview", detail | issued, started)
        return Preview(order, contract.name, decision.warnings, token, expires_at)

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
                _, decision = await self._check(user_id, ticket.order)
                # 사람이 보지 못한 경고(예: 확인 뒤 종가 단일가 구간)가 생기면 보내지 않는다.
                new = _new_warnings(ticket, decision)
                if new:
                    raise RiskBlocked(
                        WARNING_CHANGED,
                        f"주문을 확인한 뒤 새 경고가 생겼습니다: {' '.join(new)} "
                        "다시 미리보기 해 주세요.",
                    )
            except Exception as exc:
                self._audit(user_id, "order.place", detail | _failure(exc), started)
                raise
            try:
                stored = await self._submit(user_id, ticket.order, detail, started)
            except asyncio.CancelledError:
                cancelled: dict[str, JsonValue] = {"outcome": "unknown", "cancelled": True}
                self._audit(user_id, "order.place", detail | cancelled, started)
                raise
        return self._placed(user_id, stored, detail, started)

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

    async def _check(self, user_id: UserId, order: OrderRequest) -> tuple[Contract, RiskDecision]:
        """데이터를 모아 규칙을 돌린다. 막히면 예외. 잠금 안에서만 부른다."""
        if order.market != "domestic":
            raise RiskBlocked(ORDER_FORMAT, "국내 선물만 주문할 수 있습니다.")
        await self._reconcile(user_id, tolerate_failure=True)
        contract = await self._contract(order.symbol)
        rules = await self._products.rules_for(user_id, contract)
        user_limits = limits.get(self._conn, user_id, order.market)
        # 계좌를 읽지 못하면 규칙을 돌리지 않는다(Unavailable 등으로 바로 끝난다).
        account = await call_nh(self._domestic.account(user_id))
        orderable = await self._orderable(user_id, order)
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
            )
        )
        if not decision.allowed:
            raise _blocked(decision)
        if decision.increases_position and self._unknown_today(user_id, order.symbol):
            raise UnknownState(UNKNOWN_HELD)
        return contract, decision

    async def _contract(self, symbol: str) -> Contract:
        try:
            contract = await self._domestic.contract(symbol)
        except MasterUnavailable:
            raise Unavailable() from None
        if contract is None:
            raise UnknownSymbol(symbol)
        return contract

    async def _orderable(self, user_id: UserId, order: OrderRequest) -> int | None:
        """주문 가능 수량. 모르면 None이고, 그러면 규칙 7(증거금)이 늘리는 주문을 막는다."""
        unknown = (NHError, UnknownApi, KeyNotRegistered, NoSuchSymbol)
        upper_limit = None
        try:
            if order.type == "market":
                upper_limit = (await self._domestic.quote(user_id, order.symbol)).upper_limit
            return await self._domestic.orderable_qty(user_id, order, upper_limit)
        except unknown:
            return None

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
    ) -> StoredOrder:
        """NH로 신규 주문을 보내는 유일한 곳. SUBMITTED를 먼저 쓰고, 응답으로 상태를 바꾼다.

        응답을 받지 못하면 UNKNOWN으로 두고 다시 보내지 않는다. SUBMITTED를 쓰지 못하면
        보내지 않는다. 보낸 뒤 결과 기록이나 다시 읽기에 실패하면 감사 로그에 unknown을 남기고
        UnknownState로 알린다. 갱신하지 못한 행은 SUBMITTED로 남아 나갔는지 모르는 행이 된다.
        """
        order_id = orders.insert_submitted(self._conn, user_id, order, self._clock())
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
