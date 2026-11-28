"""옵션 묶음 시험용 가짜 거래소: 다리 주문을 받아 정해 둔 주문내역 모습을 차례로 보여 준다.

가짜 거래소(`Exchange`)가 다리 주문을 받아, 주문마다 정해 둔 주문내역 행을 조회 때마다 차례로
돌려준다(마지막 것은 되풀이). 잔고·미체결·오늘 체결은 지금까지 돌려준 행에서 만든다. 행은 명세
필드로 만든 가짜다. 묶음 서비스 시험과 묶음 도구 시험이 함께 쓴다.
"""

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx2
from fakes import Clock, FakeNH

from nh_mcp.markets.domestic import OPEN_INTEREST, ORDER, ORDER_HISTORY, PRICE, PRICE_BID

RECORDED = Path(__file__).resolve().parent / "fixtures" / "nh" / "domestic"


def recorded(name: str) -> dict[str, Any]:
    body: dict[str, Any] = json.loads((RECORDED / f"{name}.json").read_text("utf-8"))["body"]
    return body


def occurs(*rows: dict[str, str]) -> dict[str, object]:
    return {"nrec": f"{len(rows):04d}", "nxt_key": "", "Occurs1": list(rows)}


def held(side: str, quantity: int, symbol: str) -> dict[str, str]:
    """잔고 한 행. side는 NH 코드(1 매수, 2 매도)."""
    return {
        "occurs1_sym": symbol,
        "occurs1_ko_sym_nm": symbol,
        "occurs1_trd_div": side,
        "occurs1_opint_qty": str(quantity),
        "occurs1_cls_psb_qty": str(quantity),
        "occurs1_pl_amt": "0",
    }


@dataclass(frozen=True)
class Snap:
    """주문내역 한 번에 보이는 다리 주문의 모습. done이면 남은 수량이 없다(체결 + 자동취소).

    blank는 빈 값으로 보일 칸 이름이다(읽지 못하는 행). 없으면 빈 문자열.
    """

    filled: int = 0
    price: str = ""
    done: bool = True
    blank: str = ""


WORKING = Snap(done=False)


def filled(quantity: int, price: str) -> list[Snap]:
    return [Snap(quantity, price)]


NOTHING = [Snap()]


@dataclass(frozen=True)
class Placed:
    """NH가 받은 신규 주문 하나. side는 NH 코드(1 매수, 2 매도)."""

    order_no: str
    symbol: str
    side: str
    quantity: int
    price: str

    def row(self, snap: Snap) -> dict[str, str]:
        """그 모습의 주문내역 행. 남은 수량이 없으면 전체체결(6) 또는 자동취소(8)."""
        if snap.done:
            state = "6" if snap.filled == self.quantity else "8"
        else:
            state = "4" if snap.filled else "1"
        row = {
            "occurs1_ord_no": self.order_no,
            "occurs1_sym": self.symbol,
            "occurs1_sym_nm": self.symbol,
            "occurs1_trd_div": self.side,
            "occurs1_ord_qty": str(self.quantity),
            "occurs1_ord_pric": self.price,
            "occurs1_exec_qty": str(snap.filled),
            "occurs1_exec_pric": snap.price,
            "occurs1_rem_ord_qty": "0" if snap.done else str(self.quantity - snap.filled),
            "occurs1_ord_sts": state,
            "occurs1_ord_div_cd": "1",
            "occurs1_exch_rcv_tm": "100000",
        }
        return row | ({snap.blank: ""} if snap.blank else {})


@dataclass
class Sent:
    """받은 다리 주문과 주문내역 조회 때마다 차례로 보일 모습. shown은 지금 보이는 모습."""

    placed: Placed
    snaps: list[Snap]
    shown: int = 0
    reads: int = 0

    @property
    def now(self) -> Snap:
        return self.snaps[self.shown]

    def row(self) -> dict[str, str]:
        return self.placed.row(self.now)


@dataclass
class Exchange:
    """가짜 NH의 주문 쪽. script는 받을 주문마다 주문내역 모습들(None이면 응답 없이 끊음)."""

    nh: FakeNH
    clock: Clock
    script: list[list[Snap] | None] = field(default_factory=list[list[Snap] | None])
    sent: list[Sent] = field(default_factory=list[Sent])
    orders: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    # 이 실행 전에 이미 있던 것: (종목, 방향 코드, 수량) 보유, 주문내역 행, 미체결 행.
    holdings: list[tuple[str, str, int]] = field(default_factory=list[tuple[str, str, int]])
    earlier: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    resting: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    # 종목 → (매수 1호가, 매도 1호가). 없으면 호가가 비어 있다.
    book: dict[str, tuple[str, str]] = field(default_factory=dict[str, tuple[str, str]])
    # NH 요청 하나마다 흐르는 시간(느린 NH).
    latency: timedelta = timedelta(0)
    # 주문을 받을 때마다 부르는 것(그 순간의 묶음 쥐기 기한 등을 본다).
    on_order: list[Callable[[], None]] = field(default_factory=list[Callable[[], None]])
    # `handle`로 받는 요청마다 먼저 기다리는 것(두 실행이 엇갈리는 시험). 없으면 바로 답한다.
    before: Callable[[httpx2.Request], Awaitable[None]] | None = None

    async def handle(self, request: httpx2.Request) -> httpx2.Response:
        if self.before is not None:
            await self.before(request)
        return self(request)

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if path not in (ORDER, ORDER_HISTORY, OPEN_INTEREST, PRICE, PRICE_BID):
            if path != "/auth-service/v1/token":
                self.clock.now += self.latency
            return self.nh(request)
        self.clock.now += self.latency
        payload: dict[str, object] = json.loads(request.content or b"{}")
        if path == ORDER:
            return self._order(request, payload)
        if path == ORDER_HISTORY:
            return self._json(occurs(*self._history(str(payload["exec_div"]))))
        if path == OPEN_INTEREST:
            return self._json(occurs(*self._positions()))
        symbol = str(payload["sym"])
        if path == PRICE:
            return self._json(recorded("price") | {"sym_exch": symbol, "sym_nm": symbol})
        bid, ask = self.book.get(symbol, ("", ""))
        level = {"occurs1_bid_best_pric": bid, "occurs1_ask_best_pric": ask}
        return self._json(occurs(level) if symbol in self.book else occurs())

    def _json(self, body: Mapping[str, object]) -> httpx2.Response:
        return httpx2.Response(200, content=json.dumps(dict(body), ensure_ascii=False).encode())

    def _order(self, request: httpx2.Request, payload: dict[str, object]) -> httpx2.Response:
        self.orders.append(payload)
        for hook in self.on_order:
            hook()
        entry = self.script.pop(0)
        if entry is None:
            raise httpx2.ReadTimeout("시간 초과", request=request)
        order_no = f"{301 + len(self.sent):010d}"
        placed = Placed(
            order_no,
            str(payload["sym"]),
            str(payload["trd_div"]),
            int(str(payload["ord_qty"])),
            str(payload["ord_pric"]),
        )
        self.sent.append(Sent(placed, entry))
        return self._json({"ord_no": order_no, "msg_cd": "00000", "msg_cntn": "정상 처리"})

    def _history(self, exec_div: str) -> list[dict[str, str]]:
        if exec_div == "0":
            for sent in self.sent:
                sent.shown = min(sent.reads, len(sent.snaps) - 1)
                sent.reads += 1
            return self.earlier + [s.row() for s in self.sent]
        if exec_div == "2":
            return self.resting + [s.row() for s in self.sent if not s.now.done]
        rows = self.earlier + [s.row() for s in self.sent]
        return [r for r in rows if r["occurs1_exec_qty"] != "0"]

    def _positions(self) -> list[dict[str, str]]:
        net: dict[str, int] = {}
        for symbol, side, qty in self.holdings:
            net[symbol] = net.get(symbol, 0) + (qty if side == "1" else -qty)
        for sent in self.sent:
            symbol, qty = sent.placed.symbol, sent.now.filled
            net[symbol] = net.get(symbol, 0) + (qty if sent.placed.side == "1" else -qty)
        return [held("1" if n > 0 else "2", abs(n), s) for s, n in net.items() if n]

    def legs(self, symbol: str) -> list[dict[str, object]]:
        """그 종목으로 받은 주문들."""
        return [o for o in self.orders if o["sym"] == symbol]
