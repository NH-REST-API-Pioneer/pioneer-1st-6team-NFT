"""옵션 묶음 주문(M6): 미리보기 한 번, 승인 한 번, 실행 한 번(`docs/options.md` "묶음 주문").

- 다리 주문은 `OrderService.place_leg` 하나로만 나간다(불변식 2). 이 모듈은 순서를 짜고 체결을
  지켜볼 뿐 `_submit`이나 `send_order`를 부르지 않는다.
- 묶음의 진행(다리별 체결)은 저장하지 않는다. 다리 주문 행과 오늘 주문 내역에서 매번 센다
  (`_progress`). 새 실행과 이어 하기는 같은 길이다. 남은 일은 지금 체결에서 나온다.
- 사람 승인은 묶음 토큰 한 번이다. 토큰에는 묶음 id, 그때의 다리별 체결 수량과 다리 주문 수, 사람이
  본 경고가 묶인다. 구조·다리·가격·순서는 묶음 행(`store.combos`)에 있고 바뀌지 않는다.
- 사용자별 잠금은 다리 하나의 검사와 보냄 동안만 `OrderService`가 쥔다. 체결을 기다리거나 다시
  보내기 전에 쉬는 동안에는 쥐지 않는다. 같은 묶음의 두 실행은 묶음 행을 쥐는 것(lease)으로 막는다.
  다리를 보낼 때마다 `place_leg`가 잠금 안에서 기한을 늘리고, 보내기 바로 앞에서 아직 쥐고 있는지와
  다리 주문 수를 다시 본다(`LegClaim`). 잃었으면 보내지 않고 `lease_lost`로 멈춘다.
- 응답을 받지 못했거나 끝났는지 모르는 다리 주문이 있으면 멈추고 아무것도 다시 보내지 않는다
  (불변식 5). 완성 시도의 다시 보내기는 앞 주문이 끝난 것을 주문 내역에서 본 뒤에만 한다.
"""

import hashlib
import json
import secrets
import sqlite3
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal, cast

from nh_mcp.config import ENV
from nh_mcp.domain import (
    KST,
    Clock,
    ComboContext,
    ComboPlan,
    ComboRequest,
    Contract,
    InstrumentRules,
    JsonValue,
    Leg,
    MaxLoss,
    OrderRequest,
    Side,
    Structure,
    Unbounded,
    UserId,
)
from nh_mcp.guard.combo import COMBO_UNSUPPORTED, evaluate_combo, leg_limit_price, plan
from nh_mcp.guard.confirm import judge
from nh_mcp.guard.env import orders_allowed
from nh_mcp.guard.risk import DEMO_ONLY, LIMITS_NOT_SET, ORDER_FORMAT
from nh_mcp.markets.domestic import DomesticMarket, OrderRow
from nh_mcp.markets.domestic_products import DomesticProducts
from nh_mcp.services.approval import Approval
from nh_mcp.services.orders import (
    NOT_READ,
    SIDE_NAMES,
    WARNING_CHANGED,
    ConfirmInvalid,
    LeaseLost,
    LegClaim,
    LimitsNotSet,
    MarketClosed,
    NotApproved,
    OrdersDisabled,
    OrderService,
    RiskBlocked,
    UnknownState,
    failure,
    order_no_key,
)
from nh_mcp.services.queries import (
    BrokerRejected,
    KeyMissing,
    QueryService,
    Unavailable,
    UnknownSymbol,
    call_nh,
)
from nh_mcp.store import audit, combos, confirm_tokens, features, limits, orders
from nh_mcp.store.combos import Combo, ComboRow
from nh_mcp.store.confirm_tokens import NewToken
from nh_mcp.store.orders import StoredOrder

COMBO_TTL = timedelta(minutes=2)
# 묶음의 다리 수(첫 구현은 둘).
LEGS = 2
# 실행이 묶음을 쥐는 기한. 다리를 보낼 때마다 늘린다.
LEASE = timedelta(seconds=90)
# 다리 주문이 끝났는지 주문 내역을 읽는 간격과 기다리는 한도.
POLL = timedelta(seconds=1)
SETTLE_LIMIT = timedelta(seconds=10)
# 두 번째 다리가 다 체결되지 않으면 이만큼 쉬고 같은 한계선으로 다시 보낸다. 최대 RETRIES번.
RETRY_DELAY = timedelta(seconds=1)
RETRIES = 3
COMBO_RUNNING = "combo_running"

UNSETTLED_LEG = (
    "응답을 받지 못했거나 끝났는지 모르는 다리 주문이 있어 묶음을 보내지 않습니다. 다시 주문하지 "
    "말고 get_orders로 확인하세요."
)

# 다리 주문이 하나도 없으면 previewed, 끝났는지 모르는 다리 주문이 있으면 unknown.
type ComboStatus = Literal["previewed", "filled", "partial", "nothing_filled", "unknown"]
# 실행이 멈춘 이유. rejected는 NH가 다리 주문을 거부함, unavailable은 검사할 데이터를 읽지 못함,
# lease_lost는 다른 실행이 묶음을 쥐어(또는 다리를 더 보내) 이 실행이 보내지 않고 멈춤.
type StopReason = Literal[
    "retries_exhausted",
    "price_out_of_range",
    "risk_blocked",
    "market_closed",
    "rejected",
    "unavailable",
    "lease_lost",
    "unknown",
]
# 기다리는 함수. 테스트가 시계와 함께 바로 넘어가게 주입한다.
type Sleep = Callable[[timedelta], Awaitable[None]]

STRUCTURE_NAMES: dict[Structure, str] = {
    "debit_vertical": "수직 스프레드 매수형",
    "credit_vertical": "수직 스프레드 매도형",
    "long_straddle": "스트래들 매수",
    "long_strangle": "스트랭글 매수",
    "short_straddle": "스트래들 매도",
    "short_strangle": "스트랭글 매도",
}

# 저장된 구조 문자열 → 구조. 여기에 없는 값은 읽을 수 없는 계획이다.
_STRUCTURES: dict[str, Structure] = {structure: structure for structure in STRUCTURE_NAMES}


class UnknownCombo(Exception):
    """그 묶음이 없거나 남의 것이다(구별해 알려 주지 않는다)."""


class NotResumable(Exception):
    """이어 할 수 없는 묶음이다: 오늘 만든 것이 아니거나, 지금 실행 중이거나, 일부 체결이 아니다."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class ComboDeps:
    """묶음 서비스가 부르는 것. 다리 주문은 orders(`OrderService.place_leg`)로만 나간다."""

    domestic: DomesticMarket
    products: DomesticProducts
    orders: OrderService


@dataclass(frozen=True)
class LegProgress:
    """한 다리의 체결. paid는 체결가 * 체결 수량의 합(포인트, 부호 없음)."""

    filled: int = 0
    paid: Decimal = Decimal(0)
    order_nos: tuple[str, ...] = ()

    def plus(self, filled: int, paid: Decimal, order_nos: tuple[str, ...]) -> "LegProgress":
        return LegProgress(self.filled + filled, self.paid + paid, self.order_nos + order_nos)


@dataclass(frozen=True)
class Progress:
    """묶음의 진행. legs는 요청의 다리 순서, sent는 다리 주문 행 수, unresolved는 그중 끝났는지
    모르는 것(응답 없음, 내역에 없음, 아직 끝나지 않음, 내역이 낸 것과 다름)이다.
    """

    legs: tuple[LegProgress, LegProgress]
    sent: int
    unresolved: int

    def filled(self) -> tuple[int, int]:
        return self.legs[0].filled, self.legs[1].filled

    def assume(self, leg: int, quantity: int, price: Decimal) -> "Progress":
        """그 다리가 quantity만큼 price에 체결됐다고 본 진행(미리보기)."""
        legs = list(self.legs)
        legs[leg] = legs[leg].plus(quantity, price * quantity, ())
        return replace(self, legs=(legs[0], legs[1]))


NOTHING_YET = Progress((LegProgress(), LegProgress()), 0, 0)


@dataclass(frozen=True)
class ComboStop:
    """실행이 멈춘 이유. rule은 risk_blocked의 규칙 또는 rejected의 NH 오류 코드다."""

    reason: StopReason
    rule: str | None = None


@dataclass(frozen=True)
class ComboLeg:
    """미리보기의 다리 하나. price는 미리보기 가격, filled는 지금까지 체결된 수량."""

    symbol: str
    name: str
    side: Side
    price: Decimal
    filled: int


@dataclass(frozen=True)
class ComboPreview:
    """legs는 보내는 순서다(사는 다리가 먼저)."""

    combo_id: str
    structure: Structure
    quantity: int
    legs: tuple[ComboLeg, ComboLeg]
    net_debit: Decimal
    worst_net_debit: Decimal
    max_loss: MaxLoss
    warnings: list[str]
    confirm_token: str
    expires_at: datetime


@dataclass(frozen=True)
class PendingCombo:
    """승인 창에 띄울 것. `message`는 묶음 행으로 서버가 만든 문구, `state`는 토큰의 SHA-256."""

    combo_id: str
    message: str
    state: str


@dataclass(frozen=True)
class LegResult:
    """quantity는 계획한 수량(묶음 수량), avg_price는 체결 평균가(체결이 없으면 None)."""

    symbol: str
    side: Side
    quantity: int
    filled: int
    avg_price: Decimal | None
    order_nos: tuple[str, ...]


@dataclass(frozen=True)
class ComboResult:
    """legs는 보내는 순서다. stop은 멈춘 이유이고, 할 일을 다 했으면 None이다."""

    combo_id: str
    status: ComboStatus
    legs: tuple[LegResult, LegResult]
    stop: ComboStop | None


@dataclass(frozen=True)
class _Ticket:
    """묶음 토큰에 담긴 것. filled는 토큰을 줄 때의 다리별 체결 수량(요청의 다리 순서), sent는
    그때의 다리 주문 수다.
    """

    combo_id: str
    filled: tuple[int, int]
    sent: int
    warnings: list[str]


@dataclass(frozen=True)
class _Spec:
    """묶음 하나의 바뀌지 않는 것. contracts는 요청의 다리 순서다."""

    combo_id: str
    request: ComboRequest
    plan: ComboPlan
    contracts: tuple[Contract, Contract]

    @property
    def combo(self) -> ComboContext:
        """다리 검사(guard)에 넘기는 묶음."""
        return ComboContext(self.request, self.plan)


@dataclass(frozen=True)
class _Send:
    """다음에 보낼 다리(요청의 다리 위치)와 수량."""

    leg: int
    quantity: int


@dataclass
class _Run:
    """실행 하나. rows는 이 실행이 본 끝난 다리 주문의 내역 행(`order_no_key`로), held는 쥔 기한."""

    user_id: UserId
    spec: _Spec
    warnings: list[str]
    rules: InstrumentRules | None
    rows: dict[str, OrderRow]
    held: datetime


# ---- JSON ---------------------------------------------------------------------------


def _plain(value: Decimal) -> str:
    return format(value, "f")


def _request_json(request: ComboRequest) -> dict[str, JsonValue]:
    legs: list[JsonValue] = [
        {"symbol": leg.symbol, "side": leg.side, "price": _plain(leg.price)} for leg in request.legs
    ]
    return {
        "market": request.market,
        "legs": legs,
        "quantity": request.quantity,
        "slippage": _plain(request.slippage),
    }


def _plan_json(combo: ComboPlan) -> dict[str, JsonValue]:
    loss = combo.max_loss
    return {
        "structure": combo.structure,
        "sequence": list(combo.sequence),
        "net_debit": _plain(combo.net_debit),
        "worst_net_debit": _plain(combo.worst_net_debit),
        "max_loss": "unbounded" if isinstance(loss, Unbounded) else _plain(loss),
    }


def _object(raw: object) -> dict[object, object] | None:
    return cast(dict[object, object], raw) if isinstance(raw, dict) else None


def _json_object(text: str) -> dict[object, object] | None:
    try:
        return _object(json.loads(text))
    except ValueError:
        return None


def _list(raw: object) -> list[object] | None:
    return cast(list[object], raw) if isinstance(raw, list) else None


def _decimal(raw: object) -> Decimal | None:
    if not isinstance(raw, str):
        return None
    try:
        value = Decimal(raw)
    except ArithmeticError:
        return None
    return value if value.is_finite() else None


def _leg_of(raw: object) -> Leg | None:
    values = _object(raw)
    if values is None:
        return None
    symbol, side, price = values.get("symbol"), values.get("side"), _decimal(values.get("price"))
    if not isinstance(symbol, str) or side not in ("buy", "sell") or price is None:
        return None
    return Leg(symbol, "buy" if side == "buy" else "sell", price)


def _parse_request(text: str) -> ComboRequest | None:
    """우리가 쓴 묶음 요청을 다시 읽는다. 모양이 다르면 None."""
    values = _json_object(text)
    if values is None:
        return None
    legs = [_leg_of(raw) for raw in _list(values.get("legs")) or []]
    quantity, slippage = values.get("quantity"), _decimal(values.get("slippage"))
    if (
        values.get("market") != "domestic"
        or len(legs) != LEGS
        or type(quantity) is not int
        or slippage is None
    ):
        return None
    first, second = legs
    if first is None or second is None:
        return None
    return ComboRequest("domestic", (first, second), quantity, slippage)


def _parse_plan(text: str) -> ComboPlan | None:
    """우리가 쓴 계획을 다시 읽는다. 모양이 다르면 None."""
    values = _json_object(text)
    if values is None:
        return None
    raw_structure = values.get("structure")
    structure = _STRUCTURES.get(raw_structure) if isinstance(raw_structure, str) else None
    net, worst = _decimal(values.get("net_debit")), _decimal(values.get("worst_net_debit"))
    raw_loss = values.get("max_loss")
    loss: MaxLoss | None = Unbounded() if raw_loss == "unbounded" else _decimal(raw_loss)
    order = next((o for o in ((0, 1), (1, 0)) if list(o) == values.get("sequence")), None)
    if structure is None or net is None or worst is None or loss is None or order is None:
        return None
    return ComboPlan(structure, order, net, worst, loss)


def _read(found: ComboRow) -> tuple[ComboRequest, ComboPlan] | None:
    request = _parse_request(found.combo.request)
    combo = _parse_plan(found.combo.plan)
    return None if request is None or combo is None else (request, combo)


def _payload(combo_id: str, progress: Progress, warnings: list[str]) -> str:
    values: dict[str, JsonValue] = {
        "combo_id": combo_id,
        "filled": list(progress.filled()),
        "sent": progress.sent,
        "warnings": list[JsonValue](warnings),
    }
    return json.dumps(values, sort_keys=True, ensure_ascii=False)


def _parse_ticket(payload: str) -> _Ticket | None:
    """우리가 쓴 토큰 내용을 다시 읽는다. 모양이 다르면 None(토큰을 쓸 수 없다)."""
    values = _json_object(payload)
    if values is None:
        return None
    combo_id, filled = values.get("combo_id"), _list(values.get("filled"))
    sent, warnings = values.get("sent"), _list(values.get("warnings"))
    if (
        not isinstance(combo_id, str)
        or filled is None
        or len(filled) != LEGS
        or any(type(n) is not int for n in filled)
        or type(sent) is not int
        or warnings is None
        or any(not isinstance(w, str) for w in warnings)
    ):
        return None
    first, second = cast(list[int], filled)
    return _Ticket(combo_id, (first, second), sent, cast(list[str], warnings))


# ---- 진행 계산 (순수) ------------------------------------------------------------------


def _sign(side: Side) -> int:
    return 1 if side == "buy" else -1


def _kst_day(moment: datetime) -> date:
    return moment.astimezone(KST).date()


def _settled(row: OrderRow, sent: StoredOrder) -> bool:
    """그 다리 주문이 끝났고 내역을 믿을 수 있는가.

    끝남: 전체체결·자동취소·거부, 또는 잔량 0. 믿음: 잔량과 체결 수량을 읽었고, 신규 주문 행이고,
    종목·방향·수량이 낸 것과 같고, 체결 수량이 0~주문 수량이고, 체결이 있으면 체결가가 있고,
    전체체결이면 다 체결됐다. 읽지 못한 수량을 0으로 보면 접수 중인 주문이 끝난 것이 된다.
    """
    nh = row.order
    filled = nh.filled_quantity
    return (
        row.quantities_read
        and (row.state != "accepted" or nh.remaining_quantity == 0)
        and row.division == "new"
        and (nh.symbol, nh.side, nh.quantity) == (sent.symbol, sent.side, sent.quantity)
        and 0 <= filled <= nh.quantity
        and (filled == 0 or nh.filled_price is not None)
        and (row.state != "filled" or filled == nh.quantity)
    )


def _by_number(rows: Sequence[OrderRow]) -> dict[str, OrderRow]:
    """신규 주문 행을 비교용 주문번호(`order_no_key`)로. 번호가 빈 행은 뺀다."""
    found: dict[str, OrderRow] = {}
    for row in rows:
        key = order_no_key(row.order.order_no)
        if row.division == "new" and key:
            found[key] = row
    return found


def _progress(
    request: ComboRequest, stored: Sequence[StoredOrder], rows: Mapping[str, OrderRow]
) -> Progress:
    """다리 주문 행과 그 내역 행으로 센 진행. 거부된 행은 체결 0으로 센다."""
    legs = [LegProgress(), LegProgress()]
    unresolved = 0
    for order in stored:
        if order.status == "REJECTED":
            continue
        leg = next(
            (
                i
                for i, planned in enumerate(request.legs)
                if (planned.symbol, planned.side) == (order.symbol, order.side)
            ),
            None,
        )
        number = order.nh_order_no
        row = None if number is None else rows.get(order_no_key(number))
        if leg is None or number is None or row is None or not _settled(row, order):
            unresolved += 1
            continue
        filled = row.order.filled_quantity
        paid = (row.order.filled_price or Decimal(0)) * filled
        legs[leg] = legs[leg].plus(filled, paid, (number,))
    return Progress((legs[0], legs[1]), len(stored), unresolved)


def _status(quantity: int, progress: Progress) -> ComboStatus:
    if progress.unresolved:
        return "unknown"
    if progress.sent == 0:
        return "previewed"
    filled = set(progress.filled())
    if filled == {quantity}:
        return "filled"
    if filled == {0}:
        return "nothing_filled"
    return "partial"


def _next(spec: _Spec, progress: Progress, first_open: bool) -> _Send | None:
    """다음에 보낼 다리. 두 번째 다리는 첫 다리의 체결 수량까지만, 첫 다리는 first_open일 때만.

    두 번째 다리가 첫 다리보다 덜 체결됐으면 그것부터 채운다(어느 순간에도 두 번째 다리가 첫
    다리를 넘지 않는다). first_open은 이번 실행에서 첫 다리를 아직 보내지 않았는가다.
    """
    first, second = spec.plan.sequence
    done = progress.legs[first].filled
    behind = done - progress.legs[second].filled
    if behind > 0:
        return _Send(second, behind)
    if first_open and done < spec.request.quantity:
        return _Send(first, spec.request.quantity - done)
    return None


def _limit(spec: _Spec, progress: Progress, rules: InstrumentRules | None) -> Decimal | None:
    """두 번째 다리의 남은 수량에 줄 한계 가격. 모르면 None.

    첫 다리의 가중 평균 체결가로 `leg_limit_price`를 부른다. 두 번째 다리가 앞서 체결된 것이
    있으면(이어 하기) 그것이 쓴 몫과 그 수량에 허락된 몫(worst_net_debit)의 차이도 남은 수량에
    넘긴다. 그래서 묶음 전체의 계약당 순가격이 worst_net_debit을 넘지 않는다.
    """
    if rules is None:
        return None
    request, combo = spec.request, spec.plan
    first, second = combo.sequence
    a, b = progress.legs[first], progress.legs[second]
    sa, sb = _sign(request.legs[first].side), _sign(request.legs[second].side)
    committed = sa * a.paid + sb * b.paid - combo.worst_net_debit * b.filled
    return leg_limit_price(request, combo, sa * committed / (a.filled - b.filled), rules)


# 다리를 보내다 난, 이유가 늘 같은 예외 → 멈춘 이유.
_STOPS: dict[type[Exception], ComboStop] = {
    LimitsNotSet: ComboStop("risk_blocked", LIMITS_NOT_SET),
    OrdersDisabled: ComboStop("risk_blocked", DEMO_ONLY),
    MarketClosed: ComboStop("market_closed"),
    UnknownState: ComboStop("unknown"),
    LeaseLost: ComboStop("lease_lost"),
    Unavailable: ComboStop("unavailable"),
    KeyMissing: ComboStop("unavailable"),
    UnknownSymbol: ComboStop("unavailable"),
}


def _stop_for(exc: Exception) -> ComboStop | None:
    """다리를 보내다 난 예외를 멈춘 이유로. 모르는 예외는 None(그대로 다시 던진다)."""
    if isinstance(exc, RiskBlocked):
        return ComboStop("risk_blocked", exc.rule)
    if isinstance(exc, BrokerRejected):
        return ComboStop("rejected", exc.code)
    return _STOPS.get(type(exc))


def _planned_price(
    spec: _Spec, progress: Progress, rules: InstrumentRules | None, send: _Send
) -> Decimal:
    """미리보기에서 그 다리를 볼 가격: 첫 다리는 미리보기 가격, 두 번째 다리는 한계 가격."""
    if send.leg == spec.plan.sequence[0]:
        return spec.request.legs[send.leg].price
    limit = _limit(spec, progress, rules)
    if limit is None:
        raise RiskBlocked(
            ORDER_FORMAT, "다음 다리의 호가 단위를 몰라 보낼 가격을 정할 수 없습니다."
        )
    return limit


def _leg_order(request: ComboRequest, send: _Send, price: Decimal) -> OrderRequest:
    leg = request.legs[send.leg]
    return OrderRequest("domestic", leg.symbol, leg.side, "limit", send.quantity, price, "fak")


def _loss_line(loss: MaxLoss) -> str:
    if isinstance(loss, Unbounded):
        return "최대 손실: 한정되지 않음(옵션 매도를 켠 사용자만 낼 수 있는 구조)."
    return f"최대 손실: {_plain(loss)}원(묶음 전체, 허용 폭 끝 순가격 기준)."


def _message(spec: _Spec, ticket: _Ticket) -> str:
    request, combo = spec.request, spec.plan
    first, second = combo.sequence
    quantity = request.quantity
    lines = [
        f"모의 계좌에 옵션 묶음 주문을 냅니다. {STRUCTURE_NAMES[combo.structure]}, "
        f"다리마다 {quantity}계약."
    ]
    for number, i in enumerate((first, second), start=1):
        leg = request.legs[i]
        head = f"{number}. {spec.contracts[i].name}({leg.symbol}) {SIDE_NAMES[leg.side]} "
        if i == first:
            body = f"지정가 {_plain(leg.price)}"
        else:
            body = (
                "1번이 체결된 수량만큼, 두 다리의 순가격이 계약당 "
                f"{_plain(combo.worst_net_debit)}를 넘지 않는 가격으로"
            )
        done = ticket.filled[i]
        lines.append(head + body + (f" (이미 체결 {done}/{quantity}계약)" if done else ""))
    lines += [
        "1번을 먼저 보내고, 1번이 체결된 수량만큼만 2번을 보냅니다. 다리는 바로 체결되는 만큼만 "
        "체결되고 남은 수량은 취소됩니다(FAK). 2번이 다 체결되지 않으면 1초 뒤 같은 가격으로 "
        f"{RETRIES}번까지 다시 보냅니다.",
        f"순가격 계약당 {_plain(combo.net_debit)}, 허용 폭 끝 {_plain(combo.worst_net_debit)}.",
        _loss_line(combo.max_loss),
    ]
    lines += [f"주의: {warning}" for warning in ticket.warnings]
    lines.append("승인할까요?")
    return "\n".join(lines)


def combo_ids(conn: sqlite3.Connection, user_id: UserId) -> dict[str, str]:
    """그 사용자의 다리 주문: 비교용 주문번호(`order_no_key`) → 묶음 id. get_orders가 쓴다."""
    return {
        order_no_key(order.nh_order_no): order.combo_id
        for order in orders.list_for(conn, user_id)
        if order.combo_id is not None and order.nh_order_no
    }


def _combo_failure(exc: Exception) -> dict[str, JsonValue]:
    if isinstance(exc, UnknownCombo):
        return {"outcome": "unknown_combo"}
    if isinstance(exc, NotResumable):
        return {"outcome": "not_resumable"}
    return failure(exc)


class ComboService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        clock: Clock,
        deps: ComboDeps,
        sleep: Sleep,
        orders_enabled: bool,
    ) -> None:
        self._conn = conn
        self._clock = clock
        self._domestic = deps.domestic
        self._products = deps.products
        self._orders = deps.orders
        self._queries = QueryService(deps.domestic)
        self._sleep = sleep
        self._orders_enabled = orders_enabled

    async def preview(self, user_id: UserId, request: ComboRequest) -> ComboPreview:
        """새 묶음. 묶음 규칙과 다리별 검사를 지나면 묶음 행을 넣고 2분짜리 토큰을 준다."""
        self._require_enabled()
        started = time.perf_counter_ns()
        detail: dict[str, JsonValue] = {"combo": _request_json(request), "resume": False}
        try:
            contracts = await self._contracts(request)
            combo = self._admitted(user_id, plan(request, contracts))
            spec = _Spec(secrets.token_hex(8), request, combo, contracts)
            warnings = await self._recheck(user_id, spec, NOTHING_YET)
            row = Combo(
                spec.combo_id,
                user_id,
                "domestic",
                json.dumps(_request_json(request), sort_keys=True),
                json.dumps(_plan_json(combo), sort_keys=True),
                self._clock(),
            )
            combos.insert(self._conn, row)
            preview = self._issue(user_id, spec, NOTHING_YET, warnings)
        except Exception as exc:
            self._audit(user_id, "combo.preview", detail | failure(exc), started)
            raise
        issued: dict[str, JsonValue] = {
            "outcome": "issued",
            "combo_id": spec.combo_id,
            "warnings": list[JsonValue](warnings),
        }
        self._audit(user_id, "combo.preview", detail | issued, started)
        return preview

    async def preview_resume(self, user_id: UserId, combo_id: str) -> ComboPreview:
        """오늘 만든 일부 체결 묶음을 이어 하는 토큰. 남은 일은 지금 체결에서 계산한다.

        가격·순가격 한계·구조는 처음 계획 그대로다. 토큰은 지금의 다리별 체결 수량과 다리 주문 수에
        묶인다.
        남의 묶음이나 없는 묶음은 UnknownCombo다.
        """
        self._require_enabled()
        started = time.perf_counter_ns()
        detail: dict[str, JsonValue] = {"combo_id": combo_id, "resume": True}
        try:
            found = combos.get(self._conn, user_id, combo_id)
            spec = None if found is None else await self._spec(found)
            if found is None or spec is None:
                raise UnknownCombo()
            now = self._clock()
            if _kst_day(found.combo.created_at) != _kst_day(now):
                raise NotResumable("오늘 만든 묶음만 이어 할 수 있습니다.")
            if found.lease_until is not None and found.lease_until >= now:
                raise NotResumable("이 묶음은 지금 보내는 중입니다. 끝난 뒤 다시 해 주세요.")
            self._admitted(user_id, spec.plan)
            progress, _ = await self._read_progress(user_id, spec)
            status = _status(spec.request.quantity, progress)
            if status == "unknown":
                raise UnknownState(UNSETTLED_LEG)
            if status != "partial":
                raise NotResumable(f"일부만 체결된 묶음만 이어 할 수 있습니다(지금 {status}).")
            warnings = await self._recheck(user_id, spec, progress)
            preview = self._issue(user_id, spec, progress, warnings)
        except Exception as exc:
            self._audit(user_id, "combo.preview", detail | _combo_failure(exc), started)
            raise
        issued: dict[str, JsonValue] = {
            "outcome": "issued",
            "filled": list(progress.filled()),
            "warnings": list[JsonValue](warnings),
        }
        self._audit(user_id, "combo.preview", detail | issued, started)
        return preview

    async def pending(self, user_id: UserId, token: str) -> PendingCombo:
        """토큰의 묶음을 승인 창에 띄울 모양으로. 토큰을 소진하지 않는다."""
        self._require_enabled()
        ticket = self._ticket(user_id, token)
        found = combos.get(self._conn, user_id, ticket.combo_id)
        spec = None if found is None else await self._spec(found)
        if spec is None:
            raise ConfirmInvalid()
        state = hashlib.sha256(token.encode()).hexdigest()
        return PendingCombo(ticket.combo_id, _message(spec, ticket), state)

    async def place(self, user_id: UserId, token: str, approval: Approval) -> ComboResult:
        """토큰 확인 → (거절이면 소진하고 끝) → 소진 → 묶음 쥐기 → 체결 대조·묶음 규칙 → 다리들.

        다리를 보내기 시작한 뒤에는 어디서 멈춰도 예외 대신 결과(상태와 멈춘 이유)를 돌려준다.
        """
        self._require_enabled()
        started = time.perf_counter_ns()
        try:
            ticket = self._ticket(user_id, token)
        except ConfirmInvalid as exc:
            self._audit(user_id, "combo.place", failure(exc), started)
            raise
        detail: dict[str, JsonValue] = {"combo_id": ticket.combo_id}
        self._audit(user_id, "combo.approve", detail | {"outcome": approval}, started)
        if approval == "declined":
            # AI가 같은 토큰으로 승인 창을 되풀이해 띄우지 못하게 소진한다.
            confirm_tokens.consume(self._conn, token, user_id, "combo", self._clock())
            raise NotApproved()
        try:
            held = self._claim(user_id, token, ticket)
        except Exception as exc:
            self._audit(user_id, "combo.place", detail | failure(exc), started)
            raise
        run: _Run | None = None
        try:
            run = await self._prepare(user_id, ticket, held)
            stop = await self._run(run)
            result = self._result(run, stop)
        except Exception as exc:
            self._audit(user_id, "combo.place", detail | failure(exc), started)
            raise
        finally:
            combos.release(self._conn, user_id, ticket.combo_id, held if run is None else run.held)
        outcome: dict[str, JsonValue] = {
            "outcome": result.status,
            "stop": None if result.stop is None else result.stop.reason,
            "rule": None if result.stop is None else result.stop.rule,
            "filled": [leg.filled for leg in result.legs],
        }
        self._audit(user_id, "combo.place", detail | outcome, started)
        return result

    # ---- 안쪽 -----------------------------------------------------------

    def _require_enabled(self) -> None:
        if not orders_allowed(ENV, self._orders_enabled):
            raise OrdersDisabled()

    def _ticket(self, user_id: UserId, token: str) -> _Ticket:
        row = confirm_tokens.find(self._conn, token)
        if judge(row, user_id, "combo", self._clock()) != "usable" or row is None:
            raise ConfirmInvalid()
        ticket = _parse_ticket(row.payload)
        if ticket is None:
            raise ConfirmInvalid()
        return ticket

    async def _contracts(self, request: ComboRequest) -> tuple[Contract, Contract]:
        """요청의 다리 순서대로 종목 마스터의 종목. 없으면 UnknownSymbol."""
        first, second = request.legs
        return (
            await self._queries.contract(first.symbol),
            await self._queries.contract(second.symbol),
        )

    async def _spec(self, found: ComboRow) -> _Spec | None:
        """저장된 묶음. 읽을 수 없으면 None."""
        parsed = _read(found)
        if parsed is None:
            return None
        request, combo = parsed
        return _Spec(found.combo.id, request, combo, await self._contracts(request))

    def _admitted(self, user_id: UserId, combo: ComboPlan | None) -> ComboPlan:
        """묶음 전체 규칙 1~5를 지금 한도와 스위치로 지난 계획. 막히면 RiskBlocked."""
        block = evaluate_combo(
            combo,
            limits.get(self._conn, user_id, "domestic"),
            features.enabled(self._conn, user_id, "options"),
            features.enabled(self._conn, user_id, "option_sell"),
        )
        if block is not None:
            raise RiskBlocked(*block)
        if combo is None:
            raise RiskBlocked(COMBO_UNSUPPORTED, "받는 묶음 구조가 아닙니다.")
        return combo

    async def _recheck(self, user_id: UserId, spec: _Spec, progress: Progress) -> list[str]:
        """앞으로 보낼 다리 둘을 미리 검사한다(묶음 규칙 6). 둘째는 첫째가 다 체결됐다고 본 계좌로.

        두 번째 다리는 그 계좌에서 보낼 한계 가격으로 본다(첫 다리가 미리보기 가격에 체결된 경우).
        """
        second_leg = spec.plan.sequence[1]
        rules = await self._products.rules_for(user_id, spec.contracts[second_leg])
        planned: list[OrderRequest] = []
        first_open = True
        for _ in range(LEGS):
            send = _next(spec, progress, first_open)
            if send is None:
                break
            price = _planned_price(spec, progress, rules, send)
            planned.append(_leg_order(spec.request, send, price))
            progress = progress.assume(send.leg, send.quantity, price)
            first_open = first_open and send.leg == second_leg
        if not planned:
            raise NotResumable("남은 다리가 없습니다.")
        then = planned[1] if len(planned) > 1 else None
        return await self._orders.preview_legs(user_id, spec.combo, planned[0], then)

    def _issue(
        self, user_id: UserId, spec: _Spec, progress: Progress, warnings: list[str]
    ) -> ComboPreview:
        payload = _payload(spec.combo_id, progress, warnings)
        new = NewToken(user_id, "combo", payload)
        token, expires_at = confirm_tokens.issue(self._conn, new, self._clock(), COMBO_TTL)

        def view(i: int) -> ComboLeg:
            leg = spec.request.legs[i]
            name = spec.contracts[i].name
            return ComboLeg(leg.symbol, name, leg.side, leg.price, progress.legs[i].filled)

        first, second = spec.plan.sequence
        return ComboPreview(
            spec.combo_id,
            spec.plan.structure,
            spec.request.quantity,
            (view(first), view(second)),
            spec.plan.net_debit,
            spec.plan.worst_net_debit,
            spec.plan.max_loss,
            warnings,
            token,
            expires_at,
        )

    def _claim(self, user_id: UserId, token: str, ticket: _Ticket) -> datetime:
        """토큰을 소진하고 묶음을 쥔다. 쥔 기한을 돌려준다."""
        now = self._clock()
        if not confirm_tokens.consume(self._conn, token, user_id, "combo", now):
            raise ConfirmInvalid()
        if combos.get(self._conn, user_id, ticket.combo_id) is None:
            raise ConfirmInvalid()
        held = now + LEASE
        if not combos.claim(self._conn, user_id, ticket.combo_id, now, held):
            raise RiskBlocked(
                COMBO_RUNNING,
                "이 묶음은 지금 다른 실행이 보내고 있습니다. 끝난 뒤 get_orders로 확인하세요.",
            )
        return held

    async def _prepare(self, user_id: UserId, ticket: _Ticket, held: datetime) -> _Run:
        """보내기 전 대조: 계획이 그대로이고, 묶음 규칙을 지나고, 체결과 다리 주문 수가 토큰을 줄
        때와 같다.
        """
        found = combos.get(self._conn, user_id, ticket.combo_id)
        spec = None if found is None else await self._spec(found)
        if spec is None:
            raise ConfirmInvalid()
        if plan(spec.request, spec.contracts) != spec.plan:
            raise RiskBlocked(
                WARNING_CHANGED,
                "묶음을 확인한 뒤 종목 정보가 바뀌어 구조나 최대 손실이 달라졌습니다. "
                "다시 미리보기 해 주세요.",
            )
        self._admitted(user_id, spec.plan)
        progress, rows = await self._read_progress(user_id, spec)
        if progress.unresolved:
            raise UnknownState(UNSETTLED_LEG)
        if (progress.filled(), progress.sent) != (ticket.filled, ticket.sent):
            raise RiskBlocked(
                WARNING_CHANGED,
                "묶음을 확인한 뒤 다리 주문이나 체결이 바뀌었습니다. preview_combo(resume)로 다시 "
                "미리보기 해 주세요.",
            )
        rules = await self._products.rules_for(user_id, spec.contracts[spec.plan.sequence[1]])
        return _Run(user_id, spec, ticket.warnings, rules, rows, held)

    async def _read_progress(
        self, user_id: UserId, spec: _Spec
    ) -> tuple[Progress, dict[str, OrderRow]]:
        """다리 주문 행과 오늘 주문 내역으로 센 진행. 번호를 받은 다리 주문이 없으면 내역을 읽지
        않는다. 내역을 읽지 못하면 서비스 예외(Unavailable 등)다.
        """
        stored = orders.list_for_combo(self._conn, user_id, spec.combo_id)
        rows: dict[str, OrderRow] = {}
        if any(order.nh_order_no for order in stored):
            rows = _by_number(await call_nh(self._domestic.todays_orders(user_id)))
        return _progress(spec.request, stored, rows), rows

    async def _run(self, run: _Run) -> ComboStop | None:
        """남은 일이 없을 때까지 다리를 하나씩 보낸다. 멈추면 그 이유.

        첫 다리는 한 실행에 한 번, 미리보기 가격으로. 두 번째 다리는 첫 다리의 체결 수량만큼, 한 번
        정한 한계 가격으로, 처음 한 번과 다시 보내기 RETRIES번. 다음 일은 매번 지금 진행에서 정한다.
        """
        first = run.spec.plan.sequence[0]
        first_open, tries = True, 0
        limit: Decimal | None = None
        while True:
            progress = self._known_progress(run)
            if progress.unresolved:
                return ComboStop("unknown")
            send = _next(run.spec, progress, first_open)
            if send is None:
                return None
            if send.leg == first:
                first_open, tries, limit = False, 0, None
                price = run.spec.request.legs[first].price
            else:
                if tries > RETRIES:
                    return ComboStop("retries_exhausted")
                if tries:
                    await self._sleep(RETRY_DELAY)
                if limit is None:
                    limit = _limit(run.spec, progress, run.rules)
                if limit is None:
                    return ComboStop("risk_blocked", ORDER_FORMAT)
                if not await self._in_range(run.user_id, run.spec.request.legs[send.leg], limit):
                    return ComboStop("price_out_of_range")
                tries += 1
                price = limit
            stop = await self._send(run, send, progress.sent, price)
            if stop is not None:
                return stop

    def _known_progress(self, run: _Run) -> Progress:
        stored = orders.list_for_combo(self._conn, run.user_id, run.spec.combo_id)
        return _progress(run.spec.request, stored, run.rows)

    async def _in_range(self, user_id: UserId, leg: Leg, limit: Decimal) -> bool:
        """상대 1호가가 한계선 안인가: 파는 다리는 매수 1호가 >= 하한, 사는 다리는 매도 1호가 <=
        상한. 시세나 호가를 읽지 못하면 False.
        """
        try:
            quote = await self._domestic.quote(user_id, leg.symbol)
        except NOT_READ:
            return False
        best = quote.book[0] if quote.book else None
        if best is None:
            return False
        if leg.side == "sell":
            return best.bid is not None and best.bid >= limit
        return best.ask is not None and best.ask <= limit

    async def _send(self, run: _Run, send: _Send, sent: int, price: Decimal) -> ComboStop | None:
        """다리 주문 하나를 보내고 끝날 때까지 본다. 보내지 못했으면 그 이유.

        sent는 이 실행이 본 다리 주문 수다. `place_leg`가 잠금 안에서 쥔 기한을 늘리고, 보내기
        바로 앞에서 아직 쥐고 있는지와 다리 주문 수를 본다. 잃었으면 `lease_lost`다. 늘린 기한은
        먼저 적어 둔다. 늘리지 못했으면 이 실행의 것이 아니라 놓을 것도 없다.

        끝났는지 모르게 된 주문(응답 없음, 기다리는 한도를 넘김)은 진행에서 unresolved로 남고,
        `_run`이 그것을 보고 멈춘다.
        """
        claim = LegClaim(run.spec.combo_id, run.held, sent, self._clock() + LEASE)
        run.held = claim.until
        order = _leg_order(run.spec.request, send, price)
        try:
            stored = await self._orders.place_leg(
                run.user_id, claim, run.spec.combo, order, run.warnings
            )
        except Exception as exc:
            stop = _stop_for(exc)
            if stop is None:
                raise
            return stop
        started = time.perf_counter_ns()
        row = await self._settle(run.user_id, stored)
        detail: dict[str, JsonValue] = {
            "combo_id": run.spec.combo_id,
            "order_id": stored.id,
            "symbol": order.symbol,
            "side": order.side,
            "quantity": order.quantity,
            "price": _plain(price),
        }
        if row is None:
            self._audit(run.user_id, "combo.leg", detail | {"outcome": "unknown"}, started)
            return None
        fill_price = row.order.filled_price
        settled: dict[str, JsonValue] = {
            "outcome": row.state,
            "filled": row.order.filled_quantity,
            "filled_price": None if fill_price is None else _plain(fill_price),
        }
        self._audit(run.user_id, "combo.leg", detail | settled, started)
        run.rows[order_no_key(row.order.order_no)] = row
        return None

    async def _settle(self, user_id: UserId, stored: StoredOrder) -> OrderRow | None:
        """그 다리 주문이 끝날 때까지 POLL마다 오늘 주문 내역을 본다. SETTLE_LIMIT 안에 끝나지
        않거나 내역을 믿을 수 없으면 None(모른다).
        """
        if stored.nh_order_no is None:
            return None
        key = order_no_key(stored.nh_order_no)
        deadline = self._clock() + SETTLE_LIMIT
        while True:
            try:
                row = _by_number(await call_nh(self._domestic.todays_orders(user_id))).get(key)
            except (Unavailable, BrokerRejected, KeyMissing, UnknownSymbol):
                row = None
            if row is not None and _settled(row, stored):
                return row
            if self._clock() >= deadline:
                return None
            await self._sleep(POLL)

    def _result(self, run: _Run, stop: ComboStop | None) -> ComboResult:
        progress = self._known_progress(run)
        request = run.spec.request

        def leg(i: int) -> LegResult:
            planned, done = request.legs[i], progress.legs[i]
            average = done.paid / done.filled if done.filled else None
            return LegResult(
                planned.symbol, planned.side, request.quantity, done.filled, average, done.order_nos
            )

        first, second = run.spec.plan.sequence
        status = _status(request.quantity, progress)
        return ComboResult(run.spec.combo_id, status, (leg(first), leg(second)), stop)

    def _audit(
        self, user_id: UserId, action: str, detail: dict[str, JsonValue], started: int
    ) -> None:
        elapsed_ms = (time.perf_counter_ns() - started) // 1_000_000
        record = detail | {"elapsed_ms": elapsed_ms}
        audit.record(self._conn, user_id, action, record, self._clock())
