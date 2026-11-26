"""실행기 테스트의 가짜들: 멈춰 둔 시계, 상태 있는 가짜 NH(주문·체결·잔고·봉), 대본대로 답하는
세션, 가짜 수신기. 주문은 진짜 `OrderService`(리스크·승인 범위 규칙 포함)를 지나 가짜 NH로 나간다.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import json
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs

import httpx2
from fakes import FakeMaster, accept_any_key

from nh_mcp.domain import (
    KST,
    Candle,
    FillEvent,
    JsonValue,
    Output,
    RiskLimits,
    SessionFailed,
    SessionJob,
    Side,
    StrategyEvent,
    StrategyScope,
    Streams,
    UserId,
)
from nh_mcp.keys.service import KeyStore
from nh_mcp.keys.vault import Vault
from nh_mcp.markets.domestic import DomesticMarket
from nh_mcp.markets.domestic_master import DomesticMaster
from nh_mcp.markets.domestic_products import DomesticProducts
from nh_mcp.nh.client import Credentials
from nh_mcp.nh.clients import ClientPool
from nh_mcp.realtime.feed import Subscription
from nh_mcp.runner.run import Deps, Sessions
from nh_mcp.runner.runner import Runner
from nh_mcp.sandbox.client import SessionGone, SessionHandle
from nh_mcp.sandbox.wire import BadOutput, output_from_wire, output_to_wire
from nh_mcp.services.backtest import code_hash, params_key, streams_data
from nh_mcp.services.orders import OrderService
from nh_mcp.services.queries import QueryService
from nh_mcp.services.runs import LiveFeeds, RunMarket
from nh_mcp.store import db, limits, strategy_runs, ticks, users
from nh_mcp.store.strategy_runs import RunRow
from nh_mcp.store.ticks import Sub, TickStore

RECORDED = Path(__file__).resolve().parents[1] / "fixtures" / "nh" / "domestic"
SYMBOL = "A016C000"  # 코스피200 F 202612, 틱 0.05, 승수 250000, 08:45~15:45(종가 단일가 15:35~)
TICK = Decimal("0.05")
# 2026-12-01(화) 10:00 KST. 장중이다.
T0 = datetime(2026, 12, 1, 10, 0, tzinfo=KST)
LIMITS = RiskLimits(Decimal("5000000"), 3, 2)
CODE = "def on_event(event, ctx):\n    return None\n"
API = "/trade/v1/domestic/"


def scope(**changes: object) -> StrategyScope:
    base = StrategyScope(
        quantity=1,
        max_position=1,
        daily_orders=10,
        daily_loss=Decimal("1000000"),
        expires_at=T0 + timedelta(days=3),
        on_loss="hold",
        mode="auto",
    )
    return replace(base, **changes)


def approval(
    streams: Streams,
    run_scope: StrategyScope,
    code: str = CODE,
    params: dict[str, JsonValue] | None = None,
) -> str:
    """`start_strategy`가 소진한 승인 내용(`services/strategy.py`의 저장 모양)."""
    chosen = params or {}
    values: dict[str, JsonValue] = {
        "market": "domestic",
        "streams": streams_data(streams),
        "code_hash": code_hash(code),
        "params": params_key(chosen),
        "model_hash": None,
        "scope": strategy_runs.scope_data(run_scope),
    }
    return json.dumps(values, sort_keys=True, ensure_ascii=False)


# ---- 시계 -----------------------------------------------------------------


class VirtualTimer:
    """멈춰 둔 시계. 테스트가 run_until로 앞으로 돌리면 그 사이에 깨어날 것을 차례로 깨운다.

    깨울 때마다 다른 작업이 할 일을 다 할 때까지 기다린다(busy가 True인 동안은 실제로 잠깐 잔다:
    실제 컨테이너처럼 바깥에서 도는 일이 있을 때).
    """

    def __init__(self, start: datetime = T0, busy: Callable[[], bool] = lambda: False) -> None:
        self._now = start
        self._sleepers: list[tuple[datetime, int, asyncio.Future[None]]] = []
        self._seq = itertools.count()
        self.busy = busy

    def now(self) -> datetime:
        return self._now

    def __call__(self) -> datetime:
        return self._now

    async def sleep_until(self, when: datetime) -> None:
        if when <= self._now:
            await asyncio.sleep(0)
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._sleepers, (when, next(self._seq), future))
        await future

    async def settle(self) -> None:
        for _ in range(3):
            for _ in range(300):
                await asyncio.sleep(0)
            while self.busy():
                await asyncio.sleep(0.005)

    async def run_until(self, when: datetime) -> None:
        await self.settle()
        while True:
            while self._sleepers and self._sleepers[0][2].done():
                heapq.heappop(self._sleepers)
            if not self._sleepers or self._sleepers[0][0] > when:
                break
            wake, _, future = heapq.heappop(self._sleepers)
            self._now = max(self._now, wake)
            future.set_result(None)
            await self.settle()
        self._now = max(self._now, when)
        await self.settle()

    async def advance(self, seconds: float) -> None:
        await self.run_until(self._now + timedelta(seconds=seconds))


# ---- 가짜 NH ----------------------------------------------------------------


def _num(value: Decimal | int) -> str:
    return format(value, "f")


@dataclass
class Placed:
    account: str
    order_no: str
    symbol: str
    side: Side
    kind: Literal["limit", "market"]
    quantity: int
    price: Decimal | None
    placed_at: datetime
    filled: int = 0
    paid: Decimal = Decimal(0)
    cancelled: bool = False

    @property
    def remaining(self) -> int:
        return 0 if self.cancelled else self.quantity - self.filled


@dataclass
class Bar:
    candle: Candle
    published_at: datetime


@dataclass
class Exchange:
    """NH 모의 서버 흉내(상태 있음). APP Key마다 계좌가 따로다.

    시장가는 `market_price(종목, 방향)`이 가격을 주면 그 자리에서 전량 체결, 지정가는 걸어 두고
    `cross`(체결가 하나)가 넘어설 때 지정가에 전량 체결한다. `tape`을 주면 봉으로 체결한다
    (백테스트와 같은 가정: 시장가는 그 분 봉 시가 ± 1틱, 지정가는 넘어선 첫 봉에서 지정가).
    """

    timer: VirtualTimer
    market_price: Callable[[str, Side], Decimal | None] = lambda _s, _d: None
    tape: dict[str, list[Candle]] = field(default_factory=dict[str, list[Candle]])
    minute_bars: dict[str, list[Bar]] = field(default_factory=dict[str, list[Bar]])
    day_bars: dict[str, list[Bar]] = field(default_factory=dict[str, list[Bar]])
    positions: dict[tuple[str, str], int] = field(default_factory=dict[tuple[str, str], int])
    placed: list[Placed] = field(default_factory=list[Placed])
    requests: list[tuple[datetime, str, str, dict[str, Any]]] = field(
        default_factory=list[tuple[datetime, str, str, dict[str, Any]]]
    )
    reject_orders: bool = False
    silent_orders: bool = False
    down_paths: set[str] = field(default_factory=set[str])
    _applied: set[tuple[str, datetime]] = field(default_factory=set[tuple[str, datetime]])
    _numbers: itertools.count[int] = field(default_factory=lambda: itertools.count(101))

    # ---- 테스트가 보는 것

    def calls(self, name: str, account: str | None = None) -> list[dict[str, Any]]:
        return [
            payload
            for _, acct, path, payload in self.requests
            if path == API + name and (account is None or acct == account)
        ]

    def order_requests(self, account: str | None = None) -> list[dict[str, Any]]:
        return self.calls("order", account)

    def cross(self, symbol: str, price: Decimal) -> None:
        """체결가 하나. 걸린 지정가가 넘어서면 체결된다."""
        for order in self._open(symbol):
            if order.kind == "limit" and order.price is not None:
                crossed = price < order.price if order.side == "buy" else price > order.price
                if crossed:
                    self._fill(order, order.price)

    def publish(self, symbol: str, candle: Candle, delay: timedelta, day: bool = False) -> None:
        bars = self.day_bars if day else self.minute_bars
        end = candle.at + (timedelta(days=1) if day else timedelta(minutes=1))
        bars.setdefault(symbol, []).append(Bar(candle, end + delay))

    # ---- 안쪽

    def _open(self, symbol: str | None = None) -> list[Placed]:
        return [o for o in self.placed if o.remaining and (symbol is None or o.symbol == symbol)]

    def _fill(self, order: Placed, price: Decimal) -> None:
        qty = order.remaining
        order.paid += price * qty
        order.filled += qty
        key = (order.account, order.symbol)
        signed = qty if order.side == "buy" else -qty
        self.positions[key] = self.positions.get(key, 0) + signed

    def _run_tape(self, now: datetime) -> None:
        """봉으로 체결: 봉의 30초째에 그 봉으로 걸린 지정가를 본다(그 봉 시작 전에 낸 것만)."""
        for symbol, candles in self.tape.items():
            for candle in candles:
                moment = candle.at + timedelta(seconds=30)
                if moment > now or (symbol, candle.at) in self._applied:
                    continue
                self._applied.add((symbol, candle.at))
                for order in self._open(symbol):
                    if order.kind != "limit" or order.price is None:
                        continue
                    if order.placed_at >= moment:
                        continue
                    crossed = (
                        candle.low < order.price
                        if order.side == "buy"
                        else candle.high > order.price
                    )
                    if crossed:
                        self._fill(order, order.price)

    def _tape_market(self, symbol: str, side: Side, now: datetime) -> Decimal | None:
        for candle in self.tape.get(symbol, []):
            if candle.at <= now < candle.at + timedelta(minutes=1):
                return candle.open + (TICK if side == "buy" else -TICK)
        return None

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        now = self.timer.now()
        if request.url.path == "/auth-service/v1/token":
            form = parse_qs(request.content.decode())
            token = {"access_token": "tok-" + form["appkey"][0], "expires_in": "86400"}
            return httpx2.Response(200, content=json.dumps(token).encode())
        account = request.headers["authorization"].removeprefix("Bearer tok-")
        payload: dict[str, Any] = json.loads(request.content or b"{}")
        path = request.url.path
        self.requests.append((now, account, path, payload))
        if path in self.down_paths:
            raise httpx2.ReadTimeout("시간 초과", request=request)
        self._run_tape(now)
        body = self._answer(account, path.removeprefix(API), payload, now, request)
        return httpx2.Response(200, content=json.dumps(body, ensure_ascii=False).encode())

    def _answer(
        self,
        account: str,
        name: str,
        payload: dict[str, Any],
        now: datetime,
        request: httpx2.Request,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        match name:
            case "price" | "price-bid" | "deposit" | "orderable-quantity" | "product-detail":
                body = json.loads((RECORDED / f"{name}.json").read_text("utf-8"))["body"]
            case "open-interest":
                mine = [(s, q) for (a, s), q in self.positions.items() if a == account and q]
                body = _occurs([self._held(account, s, q) for s, q in mine])
            case "order-history":
                orders = [o for o in self.placed if o.account == account]
                if payload.get("exec_div") == "2":
                    orders = [o for o in orders if o.remaining]
                body = _occurs([_history(o) for o in orders])
            case "order":
                body = self._order(account, payload, now, request)
            case "cancel":
                for order in self.placed:
                    if order.account == account and order.order_no == payload["org_ord_no"]:
                        order.cancelled = True
                body = {"ord_no": f"{next(self._numbers):010d}", "msg_cd": "00000", "msg_cntn": ""}
            case "chart-minute":
                body = _chart(self.minute_bars.get(payload["sym"], []), payload, now, minute=True)
            case "chart-daily":
                body = _chart(self.day_bars.get(payload["sym"], []), payload, now, minute=False)
            case _:
                pass
        return body

    def _order(
        self, account: str, payload: dict[str, Any], now: datetime, request: httpx2.Request
    ) -> dict[str, Any]:
        if self.silent_orders:
            raise httpx2.ReadTimeout("시간 초과", request=request)
        if self.reject_orders:
            return {"ord_no": "", "msg_cd": "4001", "msg_cntn": "주문이 거부되었습니다"}
        side: Side = "buy" if payload["trd_div"] == "1" else "sell"
        kind: Literal["limit", "market"] = "limit" if payload["ord_tp_cd"] == "1" else "market"
        order = Placed(
            account=account,
            order_no=f"{next(self._numbers):010d}",
            symbol=payload["sym"],
            side=side,
            kind=kind,
            quantity=int(payload["ord_qty"]),
            price=None if kind == "market" else Decimal(payload["ord_pric"]),
            placed_at=now,
        )
        self.placed.append(order)
        if kind == "market":
            price = self._tape_market(order.symbol, side, now) if self.tape else None
            price = price if price is not None else self.market_price(order.symbol, side)
            if price is not None:
                self._fill(order, price)
        return {"ord_no": order.order_no, "msg_cd": "00000", "msg_cntn": "정상 처리"}

    @staticmethod
    def _held(account: str, symbol: str, quantity: int) -> dict[str, str]:
        return {
            "occurs1_sym": symbol,
            "occurs1_ko_sym_nm": "코스피200 F 202612",
            "occurs1_trd_div": "1" if quantity > 0 else "2",
            "occurs1_opint_qty": str(abs(quantity)),
            "occurs1_cls_psb_qty": str(abs(quantity)),
            "occurs1_pl_amt": "0",
        }


def _occurs(rows: list[dict[str, str]]) -> dict[str, Any]:
    return {"nrec": f"{len(rows):04d}", "nxt_key": "", "Occurs1": rows}


def _history(order: Placed) -> dict[str, str]:
    if order.filled == order.quantity:
        status = "6"
    elif order.filled:
        status = "4"
    else:
        status = "1"
    average = order.paid / order.filled if order.filled else Decimal(0)
    return {
        "occurs1_ord_no": order.order_no,
        "occurs1_org_ord_no": "",
        "occurs1_sym": order.symbol,
        "occurs1_sym_nm": "코스피200 F 202612",
        "occurs1_trd_div": "1" if order.side == "buy" else "2",
        "occurs1_ord_sts": status,
        "occurs1_ord_sts_nm": "체결" if status == "6" else "접수",
        "occurs1_ord_qty": str(order.quantity),
        "occurs1_ord_pric": _num(order.price or Decimal(0)),
        "occurs1_exec_qty": str(order.filled),
        "occurs1_exec_pric": _num(average),
        "occurs1_rem_ord_qty": str(order.remaining),
        "occurs1_ord_div_cd": "1",
        "occurs1_exch_rcv_tm": order.placed_at.astimezone(KST).strftime("%H%M%S"),
    }


def _chart(bars: list[Bar], payload: dict[str, Any], now: datetime, minute: bool) -> dict[str, Any]:
    """그때 보이는 봉. 같은 시작 시각의 봉이 여럿 올라왔으면 가장 늦게 올라온 것이다.

    만들어지는 중인 봉이 마감 뒤에 확정되는 NH 모습이다(2026-11-26 장중 측정).
    """
    start, end = payload["inq_strt_dt"], payload["inq_ed_dt"]
    latest: dict[datetime, Bar] = {}
    for b in sorted(bars, key=lambda b: b.published_at):
        if b.published_at <= now:
            latest[b.candle.at] = b
    visible = sorted(latest.values(), key=lambda b: b.candle.at)
    rows = [
        {
            "occurs1_ref_dt": b.candle.at.astimezone(KST).strftime("%Y%m%d"),
            "occurs1_exch_tm": b.candle.at.astimezone(KST).strftime("%H%M%S") if minute else "",
            "occurs1_open_pric": _num(b.candle.open),
            "occurs1_high_pric": _num(b.candle.high),
            "occurs1_low_pric": _num(b.candle.low),
            "occurs1_close_pric": _num(b.candle.close),
            "occurs1_cum_trd_qty": str(b.candle.volume or 0),
        }
        for b in visible
        if start <= b.candle.at.astimezone(KST).strftime("%Y%m%d") <= end
    ]
    rows.reverse()  # 최근 것부터
    return {"nrec": f"{len(rows):04d}", "nxt_key": "", "Occurs1": rows}


# ---- 세션 ------------------------------------------------------------------

type Strategy = Callable[[StrategyEvent, dict[str, int]], Output]


def ignore(_event: StrategyEvent, _positions: dict[str, int]) -> Output:
    return None


class FakeSessions:
    """대본대로 답하는 띄워 둔 컨테이너. 출력은 실행기와 같은 검사(`output_from_wire`)를 지난다.

    strategy는 (이벤트, 체결로 센 포지션)을 받아 출력을 낸다. fail_on이 참인 이벤트에서는 그 실패로
    세션이 끝난다.
    """

    def __init__(self, strategy: Strategy = ignore) -> None:
        self.strategy = strategy
        self.opened: list[SessionJob] = []
        self.fed: list[StrategyEvent] = []
        self.closed: list[str] = []
        self.open_failure: SessionFailed | None = None
        self.fail_on: Callable[[StrategyEvent], SessionFailed | None] = lambda _e: None
        self.gone = False
        self._live: dict[str, dict[str, int]] = {}

    async def open_session(self, job: SessionJob) -> SessionHandle | SessionFailed:
        self.opened.append(job)
        if self.open_failure is not None:
            return self.open_failure
        session_id = secrets.token_hex(8)
        self._live[session_id] = dict.fromkeys(job.streams, 0)
        return SessionHandle(session_id, replace(job, model=None))

    async def feed(self, handle: SessionHandle, event: StrategyEvent) -> Output | SessionFailed:
        positions = self._live.get(handle.session_id)
        if positions is None or self.gone:
            raise SessionGone("전략 세션이 없습니다")
        self.fed.append(event)
        failed = self.fail_on(event)
        if failed is not None:
            del self._live[handle.session_id]
            return failed
        if isinstance(event, FillEvent):
            positions[event.symbol] += event.qty if event.side == "buy" else -event.qty
        output = self.strategy(event, dict(positions))
        try:
            return output_from_wire(output_to_wire(output), handle.job)
        except BadOutput:
            del self._live[handle.session_id]
            return SessionFailed("invalid_output", "계약에 맞지 않는 출력")

    async def close(self, handle: SessionHandle) -> None:
        self.closed.append(handle.session_id)
        self._live.pop(handle.session_id, None)

    def market_events(self) -> list[StrategyEvent]:
        return [e for e in self.fed if not isinstance(e, FillEvent)]

    def fills(self) -> list[FillEvent]:
        return [e for e in self.fed if isinstance(e, FillEvent)]


# ---- 수신기 ----------------------------------------------------------------


class FakeRecorder:
    """수신기 흉내: 구독을 받아 진짜 Subscription을 돌려준다. 테스트가 그 구독에 행을 넣는다."""

    def __init__(self, tmp_path: Path) -> None:
        self._store = TickStore(ticks.connect(tmp_path / "ticks.db"))
        self.handles: list[Subscription] = []
        self.removed: list[Subscription] = []

    async def subscribe(self, user_id: UserId, owner: str, subs: frozenset[Sub]) -> Subscription:
        handle = Subscription(user_id, owner, subs)
        self.handles.append(handle)
        return handle

    async def unsubscribe(self, handle: Subscription) -> None:
        self.removed.append(handle)
        handle.end("unsubscribed")

    @property
    def store(self) -> TickStore:
        return self._store


# ---- 한데 묶기 --------------------------------------------------------------

KEY_A, KEY_B = "key-a-0001", "key-b-0002"


class Rig:
    def __init__(
        self,
        tmp_path: Path,
        strategy: Strategy = ignore,
        start: datetime = T0,
        sessions: Sessions | None = None,
        busy: Callable[[], bool] = lambda: False,
    ) -> None:
        self.timer = VirtualTimer(start, busy)
        self.exchange = Exchange(self.timer)
        self.conn = db.connect(":memory:")
        self.user = users.get_or_create(self.conn, "g-a", "a@example.com", start)
        self.other = users.get_or_create(self.conn, "g-b", "b@example.com", start)
        vault = Vault(bytes(32))
        self.keys = KeyStore(self.conn, vault, accept_any_key, self.timer)
        transport = httpx2.MockTransport(self.exchange)
        self.pool = ClientPool(self.conn, vault, self.timer, transport=transport)
        master = DomesticMaster(self.timer, httpx2.MockTransport(FakeMaster()))
        domestic = DomesticMarket(self.pool, master, self.timer)
        products = DomesticProducts(self.pool, self.timer)
        for user, key in ((self.user, KEY_A), (self.other, KEY_B)):
            asyncio.run(self.keys.register(user, "domestic", Credentials(key, "secret-" + key)))
            limits.put(self.conn, user, "domestic", LIMITS, start)
        self.orders = OrderService(self.conn, self.timer, domestic, products, True)
        self.recorder = FakeRecorder(tmp_path)
        # 대본대로 답하는 세션. 실제 실행기를 쓰는 테스트는 sessions로 넘기고 그쪽을 본다.
        self.sessions = FakeSessions(strategy)
        used: Sessions = self.sessions if sessions is None else sessions
        market = RunMarket(QueryService(domestic), domestic, products)
        self.deps = Deps(self.conn, self.timer, market, LiveFeeds(self.recorder), self.orders, used)
        self.runner = Runner(self.deps)

    def row(self, run_id: str, user: UserId | None = None) -> RunRow:
        found = strategy_runs.get(self.conn, user or self.user, run_id)
        assert found is not None
        return found


def flat_bar(at: datetime, price: str, volume: int = 10) -> Candle:
    value = Decimal(price)
    return Candle(at, value, value, value, value, volume)


def candle(at: datetime, o: str, h: str, low: str, c: str) -> Candle:
    return Candle(at, Decimal(o), Decimal(h), Decimal(low), Decimal(c), 10)


def held(rig: Rig, account: str = KEY_A, symbol: str = SYMBOL) -> int:
    return rig.exchange.positions.get((account, symbol), 0)


def fills_of(rig: Rig) -> Mapping[str, int]:
    return {o.order_no: o.filled for o in rig.exchange.placed}
