"""국내 시장 모듈: NH 응답을 공용 타입으로 바꾼다. NH 원본 필드명은 이 밖으로 나가지 않는다.

필드는 명세(국내 API의 OUTPUT 표)를 따른다. 실제 응답으로 확인하기 전에 정한 것은 (제안)으로 적었다.
숫자는 문자열로 오므로 Decimal로 읽고, 비었거나 읽을 수 없으면 None으로 둔다(지어내지 않는다).
"""

import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Literal

from nh_mcp.domain import (
    KST,
    AccountState,
    BookLevel,
    Candle,
    Clock,
    Contract,
    FillCondition,
    JsonValue,
    Order,
    OrderError,
    OrderRequest,
    OrderType,
    Position,
    Quote,
    Side,
    UserId,
)
from nh_mcp.markets.domestic_master import DomesticMaster
from nh_mcp.nh.client import NHClient, Rejected
from nh_mcp.nh.clients import ClientPool

type JsonObject = dict[str, JsonValue]
type Interval = Literal["minute", "day"]

API = "/trade/v1/domestic/"
PRICE, PRICE_BID = API + "price", API + "price-bid"
CHART_MINUTE, CHART_DAILY = API + "chart-minute", API + "chart-daily"
DEPOSIT, OPEN_INTEREST = API + "deposit", API + "open-interest"
ORDER_HISTORY, ORDER_ERROR = API + "order-history", API + "order-error"
ORDER, CANCEL, ORDERABLE_QTY = API + "order", API + "cancel", API + "orderable-quantity"

DAY_EXCHANGE = "KSP"
DAY_SESSION = "1"
# 차트 한 번에 오는 최대 행 수(명세 최대, nh-api.md). 분봉은 Q26 측정으로도 2000이다.
# 이보다 적게 오면 요청한 기간을 다 받은 것이다. 일봉 최대를 측정하지 못해 명세의 작은 값을 쓴다.
CHART_PAGE_ROWS: dict[Interval, int] = {"minute": 2000, "day": 999}
# 끝 날짜를 옮기며 받는 횟수 상한. 1분봉 25,000개는 하루치(약 411봉)가 겹쳐도 16번이면 된다.
CHART_MAX_PAGES = 20
# nxt_key 페이지를 끝없이 따라가지 않는다. (제안)
MAX_PAGES = 20
CHART_START_KEY = "99999999"
OPEN_ORDERS = "2"  # exec_div: 미체결
FILLED = "1"  # exec_div: 체결
EVERY = "0"
# 오늘 주문 내역 전체(매매·체결·시간·주문 구분 모두 0).
ALL_HISTORY: dict[str, JsonValue] = {
    "trd_div": EVERY,
    "exec_div": EVERY,
    "trd_tm_div_cd": EVERY,
    "ord_div_cd": EVERY,
    "nxt_key": "",
}
TODAY_FILLS: dict[str, JsonValue] = ALL_HISTORY | {"exec_div": FILLED}
# 주문 입력값(nh-api.md "국내 선물 신규주문 입력값").
ORDER_TYPES: dict[OrderType, str] = {"limit": "1", "market": "2"}
# exec_cnd_cd. FOK(2)는 쓰지 않는다. FAK는 묶음 다리만 쓴다(M6).
FILL_CONDITIONS: dict[FillCondition, str] = {"fas": "1", "fak": "3"}
MARKET_PRICE = "0"
DAY_GENERAL = "1"  # orderable-quantity ord_sts: 주간_일반


class NoSuchSymbol(Exception):
    """NH가 모르는 종목이다. 없는 종목에도 NH는 HTTP 200, 오류 코드 없이 빈 칸만 돌려준다."""

    def __init__(self, symbol: str) -> None:
        super().__init__(symbol)
        self.symbol = symbol


def _text(obj: Mapping[str, JsonValue], key: str) -> str:
    value = obj.get(key)
    return value.strip() if isinstance(value, str) else ""


def _dec(obj: JsonObject, key: str) -> Decimal | None:
    text = _text(obj, key)
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _int(obj: JsonObject, key: str) -> int | None:
    value = _dec(obj, key)
    return int(value) if value is not None and value == value.to_integral_value() else None


def _rows(body: JsonObject) -> list[JsonObject]:
    rows = body.get("Occurs1")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


SIDES: dict[str, Side] = {"1": "buy", "2": "sell"}
SIDE_CODES: dict[Side, str] = {side: code for code, side in SIDES.items()}


def _side(code: str) -> Side | None:
    return SIDES.get(code)


def _sum(values: list[Decimal | None]) -> Decimal | None:
    # 하나라도 모르면 합도 모른다.
    if any(v is None for v in values):
        return None
    return sum((v for v in values if v is not None), Decimal(0))


def parse_quote(symbol: str, price: JsonObject, bid: JsonObject) -> Quote:
    book = [
        BookLevel(
            bid=_dec(row, "occurs1_bid_best_pric"),
            bid_qty=_int(row, "occurs1_bid_qty"),
            ask=_dec(row, "occurs1_ask_best_pric"),
            ask_qty=_int(row, "occurs1_ask_qty"),
        )
        for row in _rows(bid)
    ]
    return Quote(
        symbol=symbol,
        name=_text(price, "sym_nm"),
        last=_dec(price, "last_pric"),
        change=_dec(price, "chg"),
        change_rate=_dec(price, "rt"),
        open=_dec(price, "open_pric"),
        high=_dec(price, "high_pric"),
        low=_dec(price, "low_pric"),
        volume=_int(price, "trd_qty"),
        upper_limit=_dec(price, "up"),
        lower_limit=_dec(price, "lo"),
        book=book,
        iv=_dec(price, "ivol"),
        delta=_dec(price, "delta"),
        theoretical=_dec(price, "theo_pric"),
    )


def parse_candles(body: JsonObject, interval: Interval) -> list[Candle]:
    candles: list[Candle] = []
    for row in _rows(body):
        stamp = _text(row, "occurs1_ref_dt") + (
            _text(row, "occurs1_exch_tm") if interval == "minute" else "000000"
        )
        o, h = _dec(row, "occurs1_open_pric"), _dec(row, "occurs1_high_pric")
        low, c = _dec(row, "occurs1_low_pric"), _dec(row, "occurs1_close_pric")
        try:
            at = datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=KST)
        except ValueError:
            continue
        if o is None or h is None or low is None or c is None:
            continue
        # 명세 이름은 "누적거래량"이지만 녹화(2026-11-24)로 보니 봉 하나의 거래량이다.
        candles.append(Candle(at, o, h, low, c, _int(row, "occurs1_cum_trd_qty")))
    return candles


@dataclass(frozen=True)
class CandleSeries:
    """기간 안 봉(오래된 것부터, 많아야 limit개)과, 기간 앞쪽까지 다 받았는지.

    complete가 False면 NH가 더 과거를 주지 않았거나(진전 없음) 요청 상한(CHART_MAX_PAGES)에
    닿아 시작일까지 닿지 못했다. limit개를 채워 멈춘 것은 complete다(가장 최근 limit개를 원했다).
    """

    candles: list[Candle]
    complete: bool


def _chart_request(symbol: str, interval: Interval, start: date, until: date) -> JsonObject:
    payload: JsonObject = {
        "nght_tp": DAY_SESSION,
        "sym": symbol,
        "inq_strt_dt": start.strftime("%Y%m%d"),
        "inq_ed_dt": until.strftime("%Y%m%d"),
        "req_qty": str(CHART_PAGE_ROWS[interval]),
        "nxt_key": CHART_START_KEY,
    }
    if interval == "minute":
        payload["dcnt"] = "1"
    return payload


def parse_positions(body: JsonObject) -> list[Position]:
    positions: list[Position] = []
    for row in _rows(body):
        side = _side(_text(row, "occurs1_trd_div"))
        quantity = _int(row, "occurs1_opint_qty")
        if side is None or quantity is None:
            continue
        positions.append(
            Position(
                symbol=_text(row, "occurs1_sym"),
                name=_text(row, "occurs1_ko_sym_nm"),
                side=side,
                quantity=quantity,
                closable=_int(row, "occurs1_cls_psb_qty") or 0,
                avg_price=_dec(row, "occurs1_avg_pric"),
                close_price=_dec(row, "occurs1_close_pric"),
                unrealized_pnl=_dec(row, "occurs1_pl_amt"),
            )
        )
    return positions


def _blank(row: JsonObject) -> bool:
    """종목코드가 빈 행. NH가 채워 보내는 빈 칸으로 보고 건너뛴다."""
    return not _text(row, "occurs1_sym")


def positions_complete(body: JsonObject) -> bool:
    """빈 행이 아닌 보유 행의 방향·수량·청산가능수량을 모두 읽었는가."""
    return all(
        _side(_text(row, "occurs1_trd_div")) is not None
        and _int(row, "occurs1_opint_qty") is not None
        and _int(row, "occurs1_cls_psb_qty") is not None
        for row in _rows(body)
        if not _blank(row)
    )


def open_orders_complete(body: JsonObject) -> bool:
    """빈 행이 아닌 미체결 행의 방향·잔량을 모두 읽었는가."""
    return all(
        _side(_text(row, "occurs1_trd_div")) is not None
        and _int(row, "occurs1_rem_ord_qty") is not None
        for row in _rows(body)
        if not _blank(row)
    )


def _order(row: JsonObject) -> Order | None:
    side = _side(_text(row, "occurs1_trd_div"))
    if side is None:
        return None
    return Order(
        order_no=_text(row, "occurs1_ord_no"),
        original_order_no=_text(row, "occurs1_org_ord_no"),
        symbol=_text(row, "occurs1_sym"),
        name=_text(row, "occurs1_sym_nm"),
        side=side,
        status=_text(row, "occurs1_ord_sts_nm"),
        quantity=_int(row, "occurs1_ord_qty") or 0,
        price=_dec(row, "occurs1_ord_pric"),
        filled_quantity=_int(row, "occurs1_exec_qty") or 0,
        filled_price=_dec(row, "occurs1_exec_pric"),
        remaining_quantity=_int(row, "occurs1_rem_ord_qty") or 0,
        reject_reason=_text(row, "occurs1_ord_rejt_rsn"),
    )


def parse_orders(body: JsonObject) -> list[Order]:
    return [order for row in _rows(body) if (order := _order(row)) is not None]


type Division = Literal["new", "modify", "cancel"]
type NHOrderState = Literal["accepted", "filled", "cancelled", "rejected"]

# occurs1_ord_div_cd: 1 신규, 2 정정, 3 취소
DIVISIONS: dict[str, Division] = {"1": "new", "2": "modify", "3": "cancel"}
# occurs1_ord_sts: 1 접수, 3 확인, 4 부분체결, 6 전체체결, 8 자동취소, 9 거부. 나머지는 접수로 본다.
STATES: dict[str, NHOrderState] = {"6": "filled", "8": "cancelled", "9": "rejected"}
_HHMMSS = re.compile(r"(\d{2})(\d{2})(\d{2})\d*")


@dataclass(frozen=True)
class OrderRow:
    """주문 내역 한 행(대조·취소용). `received`는 거래소 접수 시각(KST, 그날), 모르면 None.

    `quantities_read`는 잔량(`occurs1_rem_ord_qty`)과 체결 수량(`occurs1_exec_qty`)을 둘 다
    읽었는가다. `order`의 두 수량은 읽지 못하면 0이라, 끝났는지 볼 때는 이 값을 먼저 본다.
    """

    order: Order
    division: Division | None
    received: time | None
    state: NHOrderState
    quantities_read: bool


def _received(text: str) -> time | None:
    """명세는 HHMMSS. 뒤에 자리가 더 붙으면 앞 여섯 자리만 본다. (제안, Task 9 녹화로 확인)"""
    match = _HHMMSS.fullmatch(text)
    if match is None:
        return None
    hour, minute, second = (int(g) for g in match.groups())
    try:
        return time(hour, minute, second)
    except ValueError:
        return None


def parse_order_rows(body: JsonObject) -> list[OrderRow]:
    rows: list[OrderRow] = []
    for row in _rows(body):
        order = _order(row)
        if order is None:
            continue
        rows.append(
            OrderRow(
                order=order,
                division=DIVISIONS.get(_text(row, "occurs1_ord_div_cd")),
                received=_received(_text(row, "occurs1_exch_rcv_tm")),
                state=STATES.get(_text(row, "occurs1_ord_sts"), "accepted"),
                quantities_read=_int(row, "occurs1_rem_ord_qty") is not None
                and _int(row, "occurs1_exec_qty") is not None,
            )
        )
    return rows


@dataclass(frozen=True)
class Fill:
    """오늘 체결이 있는 주문 내역 한 행. quantity는 체결 수량, price는 체결가(체결 수량 평균)."""

    symbol: str
    side: Side
    quantity: int
    price: Decimal


def parse_fills(body: JsonObject) -> list[Fill] | None:
    """체결 행(`exec_div=1`). 빈 행이 아닌데 방향·체결 수량, 체결이 있는 행의 체결가 중 하나라도
    읽지 못하면 None(모름). 체결 수량이 0인 행은 뺀다.
    """
    fills: list[Fill] = []
    for row in _rows(body):
        if _blank(row):
            continue
        side = _side(_text(row, "occurs1_trd_div"))
        quantity = _int(row, "occurs1_exec_qty")
        price = _dec(row, "occurs1_exec_pric")
        if side is None or quantity is None or quantity < 0:
            return None
        if quantity == 0:
            continue
        if price is None:
            return None
        fills.append(Fill(_text(row, "occurs1_sym"), side, quantity, price))
    return fills


def parse_order_errors(body: JsonObject) -> list[OrderError]:
    return [
        OrderError(
            received_at=_text(row, "occurs1_rcv_tm"),
            symbol=_text(row, "occurs1_sym"),
            name=_text(row, "occurs1_sym_nm"),
            side=_side(_text(row, "occurs1_trd_div_cd")),
            code=_text(row, "occurs1_msg_cd"),
            message=_text(row, "occurs1_msg"),
            quantity=_int(row, "occurs1_ord_qty"),
            price=_dec(row, "occurs1_ord_pric"),
        )
        for row in _rows(body)
    ]


def _plain(value: Decimal) -> str:
    """지수 표기(1E+3) 없이 쓴다."""
    return format(value, "f")


def to_nh_order(order: OrderRequest) -> dict[str, str]:
    """신규주문 입력값. 시장가는 가격 0. 체결 조건은 `order.fill`(시장가는 늘 FAS)."""
    if order.type == "market":
        price = MARKET_PRICE
    elif order.price is None:
        raise ValueError("지정가 주문에 가격이 없습니다.")
    else:
        price = _plain(order.price)
    return {
        "sym": order.symbol,
        "trd_div": SIDE_CODES[order.side],
        "ord_tp_cd": ORDER_TYPES[order.type],
        "ord_qty": str(order.quantity),
        "ord_pric": price,
        "exec_cnd_cd": FILL_CONDITIONS[order.fill],
    }


@dataclass(frozen=True)
class Ack:
    """주문·취소 접수 응답."""

    order_no: str
    msg_cd: str
    msg_cntn: str


HTTP_OK = 200


def _ack(body: JsonObject) -> Ack:
    """주문번호가 있으면 접수로 본다. `msg_cd`의 성공 값을 몰라서 쓰는 임시 판정이다(Task 9)."""
    ack = Ack(_text(body, "ord_no"), _text(body, "msg_cd"), _text(body, "msg_cntn"))
    if not ack.order_no:
        raise Rejected(ack.msg_cd, ack.msg_cntn, HTTP_OK)
    return ack


class _Pages:
    """nxt_key를 따라 다음 페이지를 받는다. 부르는 쪽이 멈출 수 있게 한 페이지씩 내준다.

    키가 비거나 `nxt_data_yn`이 N이거나 행이 없으면 끝까지 받은 것이다(`complete`가 True).
    키가 되풀이되거나 페이지 상한(MAX_PAGES)에 닿아 멈추면 `complete`는 False로 남는다. (제안)
    """

    def __init__(self, client: NHClient, path: str, payload: Mapping[str, JsonValue]) -> None:
        self._client = client
        self._path = path
        self._payload = payload
        self.complete = False

    def __aiter__(self) -> AsyncIterator[JsonObject]:
        return self._follow()

    async def _follow(self) -> AsyncIterator[JsonObject]:
        key = _text(self._payload, "nxt_key")
        seen = {key}
        for _ in range(MAX_PAGES):
            body = await self._client.post(self._path, {**self._payload, "nxt_key": key})
            yield body
            next_key = _text(body, "nxt_key")
            if not _rows(body) or not next_key or _text(body, "nxt_data_yn") == "N":
                self.complete = True
                return
            if next_key in seen:
                return
            seen.add(next_key)
            key = next_key


class DomesticMarket:
    name = "domestic"
    currency = "KRW"

    def __init__(self, pool: ClientPool, master: DomesticMaster, clock: Clock) -> None:
        self._pool = pool
        self._master = master
        self._clock = clock

    async def contract(self, symbol: str) -> Contract | None:
        return await self._master.find(symbol)

    async def quote(self, user_id: UserId, symbol: str) -> Quote:
        client = await self._pool.client_for(user_id, "domestic")
        price = await client.post(PRICE, {"sym": symbol})
        # 빈 시세를 성공으로 내보내지 않는다(모르면 막는다).
        if not _text(price, "sym_exch"):
            raise NoSuchSymbol(symbol)
        bid = await client.post(PRICE_BID, {"exch_cd": DAY_EXCHANGE, "sym": symbol})
        return parse_quote(symbol, price, bid)

    async def candles(
        self,
        user_id: UserId,
        symbol: str,
        interval: Interval,
        period: tuple[date, date],
        limit: int,
    ) -> CandleSeries:
        """끝 날짜를 옮기며 과거로 받는다(Q26). nxt_key는 같은 행을 되풀이해서 따르지 않는다.

        첫 요청은 기간 끝 날까지, 다음 요청은 앞 응답의 가장 이른 날까지 받는다. 그날은 두 번
        오므로 봉 시작 시각으로 합친다. 멈춤: limit개를 채움, 한 번에 오는 최대보다 적게 옴(기간을
        다 받음), 시작일 전 봉이 옴, 새 봉이 없음(진전 없음), 요청 상한.
        """
        client = await self._pool.client_for(user_id, "domestic")
        start, end = period
        path = CHART_MINUTE if interval == "minute" else CHART_DAILY
        merged: dict[datetime, Candle] = {}
        until = end
        complete = False
        for _ in range(CHART_MAX_PAGES):
            body = await client.post(path, _chart_request(symbol, interval, start, until))
            got = parse_candles(body, interval)
            fresh = [c for c in got if start <= c.at.date() <= end and c.at not in merged]
            merged.update((c.at, c) for c in fresh)
            earliest = min((c.at.date() for c in got), default=until)
            if (
                len(merged) >= limit
                or len(_rows(body)) < CHART_PAGE_ROWS[interval]
                or earliest < start
            ):
                complete = True
                break
            if not fresh:
                break
            until = earliest
        return CandleSeries(sorted(merged.values(), key=lambda c: c.at)[-limit:], complete)

    async def account(self, user_id: UserId) -> AccountState:
        client = await self._pool.client_for(user_id, "domestic")
        now = self._clock()
        deposit = await client.post(DEPOSIT, {"biz_dt": now.astimezone(KST).strftime("%Y%m%d")})
        interest = _Pages(client, OPEN_INTEREST, {"nxt_key": ""})
        held = [body async for body in interest]
        history = {
            "trd_div": EVERY,
            "exec_div": OPEN_ORDERS,
            "trd_tm_div_cd": EVERY,
            "ord_div_cd": EVERY,
            "nxt_key": "",
        }
        resting = _Pages(client, ORDER_HISTORY, history)
        waiting = [body async for body in resting]
        positions = [p for body in held for p in parse_positions(body)]
        fut, opt = _dec(deposit, "fut_rez_pl_amt"), _dec(deposit, "opt_rez_pl_amt")
        return AccountState(
            market="domestic",
            currency=self.currency,
            deposit=_dec(deposit, "dps_aset_tot_amt"),
            orderable_amount=_dec(deposit, "ord_psb_tot_amt"),
            realized_pnl=_sum([fut, opt]),
            unrealized_pnl=_sum([p.unrealized_pnl for p in positions]),
            positions=positions,
            open_orders=[o for body in waiting for o in parse_orders(body)],
            as_of=now,
            complete=interest.complete
            and resting.complete
            and all(positions_complete(body) for body in held)
            and all(open_orders_complete(body) for body in waiting),
        )

    async def orders(self, user_id: UserId, day: date) -> tuple[list[Order], list[OrderError]]:
        client = await self._pool.client_for(user_id, "domestic")
        orders = [
            o
            async for body in _Pages(client, ORDER_HISTORY, ALL_HISTORY)
            for o in parse_orders(body)
        ]
        errors_payload: JsonObject = {
            "inq_dt": day.strftime("%Y%m%d"),
            "trd_div_cd": EVERY,
            "nxt_key": "",
        }
        errors = [
            e
            async for body in _Pages(client, ORDER_ERROR, errors_payload)
            for e in parse_order_errors(body)
        ]
        return orders, errors

    async def todays_orders(self, user_id: UserId) -> list[OrderRow]:
        """오늘 주문 내역 전체(신규·정정·취소, 체결·미체결)."""
        client = await self._pool.client_for(user_id, "domestic")
        return [
            row
            async for body in _Pages(client, ORDER_HISTORY, ALL_HISTORY)
            for row in parse_order_rows(body)
        ]

    async def todays_fills(self, user_id: UserId) -> list[Fill] | None:
        """오늘 체결이 있는 주문 행 전체. 읽지 못한 행이 있거나 끝 쪽까지 받지 못하면 None."""
        client = await self._pool.client_for(user_id, "domestic")
        pages = _Pages(client, ORDER_HISTORY, TODAY_FILLS)
        parsed = [parse_fills(body) async for body in pages]
        if not pages.complete:
            return None
        fills: list[Fill] = []
        for page in parsed:
            if page is None:
                return None
            fills += page
        return fills

    async def orderable_qty(
        self, user_id: UserId, order: OrderRequest, upper_limit: Decimal | None
    ) -> int | None:
        """주문 가능 수량. 시장가는 상한가로 묻는다(증거금이 가장 크게 잡힌다). 모르면 None."""
        price = upper_limit if order.type == "market" else order.price
        if price is None:
            return None
        client = await self._pool.client_for(user_id, "domestic")
        payload = {
            "ord_sts": DAY_GENERAL,
            "sym": order.symbol,
            "trd_div": SIDE_CODES[order.side],
            "ord_pric": _plain(price),
        }
        return _int(await client.post(ORDERABLE_QTY, payload), "ord_psb_qty")

    async def send_order(self, user_id: UserId, order: OrderRequest) -> Ack:
        """신규 주문을 보낸다. 부르는 곳은 `services/orders.OrderService._submit` 하나다."""
        client = await self._pool.client_for(user_id, "domestic")
        return _ack(await client.post(ORDER, to_nh_order(order)))

    async def send_cancel(self, user_id: UserId, order: Order) -> Ack:
        """미체결 잔량 전부를 취소한다. 부르는 곳은 `services/orders.OrderService.cancel` 하나다."""
        client = await self._pool.client_for(user_id, "domestic")
        payload = {
            "sym": order.symbol,
            "trd_div": SIDE_CODES[order.side],
            "ord_qty": str(order.remaining_quantity),
            "org_ord_no": order.order_no,
        }
        return _ack(await client.post(CANCEL, payload))
