"""국내 주문·취소·주문가능수량·오늘 주문 내역.

주문·취소 응답은 녹화가 없어 명세 필드로 만든 가짜다(Task 9에서 녹화로 바꾼다).
"""

import asyncio
import json
from datetime import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fakes import FakeNH, World

from nh_mcp.domain import Order, OrderRequest, UserId
from nh_mcp.markets.domestic import (
    CANCEL,
    ORDER,
    ORDER_HISTORY,
    ORDERABLE_QTY,
    Ack,
    DomesticMarket,
    Fill,
    OrderRow,
    to_nh_order,
)
from nh_mcp.markets.domestic_master import DomesticMaster
from nh_mcp.nh.client import Credentials, Rejected
from nh_mcp.store import users

RECORDED = Path(__file__).resolve().parents[1] / "fixtures" / "nh" / "domestic"
SYMBOL = "A016C000"
LIMIT_BUY = OrderRequest("domestic", SYMBOL, "buy", "limit", 1, Decimal("784.55"))
MARKET_SELL = OrderRequest("domestic", SYMBOL, "sell", "market", 2, None)


def recorded(name: str) -> dict[str, Any]:
    body: dict[str, Any] = json.loads((RECORDED / f"{name}.json").read_text("utf-8"))["body"]
    return body


class Setup:
    def __init__(self) -> None:
        self.nh = FakeNH()
        self.world = World(nh=httpx2.MockTransport(self.nh))
        self.user: UserId = users.get_or_create(
            self.world.conn, "g-admin", "admin@example.com", self.world.clock()
        )
        asyncio.run(
            self.world.store.register(self.user, "domestic", Credentials("fake-key-1", "x-sec-1"))
        )
        master = DomesticMaster(self.world.clock, httpx2.MockTransport(self.world.master))
        self.market = DomesticMarket(self.world.pool, master, self.world.clock)

    def body_of(self, path: str) -> dict[str, object]:
        (body,) = [b for p, b in self.nh.requests if p == path]
        return body


def test_limit_order_fields() -> None:
    assert to_nh_order(LIMIT_BUY) == {
        "sym": SYMBOL,
        "trd_div": "1",
        "ord_tp_cd": "1",
        "ord_qty": "1",
        "ord_pric": "784.55",
        "exec_cnd_cd": "1",
    }


def test_market_order_sends_price_zero() -> None:
    assert to_nh_order(MARKET_SELL) == {
        "sym": SYMBOL,
        "trd_div": "2",
        "ord_tp_cd": "2",
        "ord_qty": "2",
        "ord_pric": "0",
        "exec_cnd_cd": "1",
    }


def test_a_combo_leg_is_a_limit_fak_order() -> None:
    leg = OrderRequest("domestic", "B016C725", "sell", "limit", 2, Decimal("1.25"), "fak")

    assert to_nh_order(leg) == {
        "sym": "B016C725",
        "trd_div": "2",
        "ord_tp_cd": "1",
        "ord_qty": "2",
        "ord_pric": "1.25",
        "exec_cnd_cd": "3",
    }


def test_a_market_fak_order_cannot_be_made() -> None:
    """NH는 시장가에 FAS만 받는다(`domestic_order.md:31`). 그런 주문은 만들어지지도 않는다."""
    with pytest.raises(ValueError):
        OrderRequest("domestic", SYMBOL, "buy", "market", 1, None, "fak")


def test_price_is_a_plain_decimal_string_never_exponent_form() -> None:
    big = OrderRequest("domestic", SYMBOL, "buy", "limit", 1, Decimal("1E+3"))
    trailing = OrderRequest("domestic", SYMBOL, "buy", "limit", 1, Decimal("784.50"))

    assert to_nh_order(big)["ord_pric"] == "1000"
    assert to_nh_order(trailing)["ord_pric"] == "784.50"


def test_limit_order_without_a_price_is_refused() -> None:
    with pytest.raises(ValueError):
        to_nh_order(OrderRequest("domestic", SYMBOL, "buy", "limit", 1, None))


def test_send_order_with_an_order_number_is_an_ack() -> None:
    s = Setup()
    s.nh.reply(ORDER, {"ord_no": "0000000101", "msg_cd": "00000", "msg_cntn": "정상 처리"})

    ack = asyncio.run(s.market.send_order(s.user, LIMIT_BUY))

    assert ack == Ack("0000000101", "00000", "정상 처리")
    assert s.body_of(ORDER) == to_nh_order(LIMIT_BUY)


def test_send_order_without_an_order_number_is_rejected() -> None:
    s = Setup()
    s.nh.reply(ORDER, {"ord_no": "", "msg_cd": "40250", "msg_cntn": "주문가능수량 부족"})

    with pytest.raises(Rejected) as caught:
        asyncio.run(s.market.send_order(s.user, LIMIT_BUY))

    assert (caught.value.code, caught.value.message) == ("40250", "주문가능수량 부족")


def test_send_cancel_sends_symbol_side_remaining_and_original_number() -> None:
    s = Setup()
    s.nh.reply(CANCEL, {"ord_no": "0000000102", "msg_cd": "", "msg_cntn": ""})
    target = Order(
        "0000000101", "", SYMBOL, "코스피200 F 202612", "sell", "접수", 3, None, 1, None, 2, ""
    )

    ack = asyncio.run(s.market.send_cancel(s.user, target))

    assert ack.order_no == "0000000102"
    assert s.body_of(CANCEL) == {
        "sym": SYMBOL,
        "trd_div": "2",
        "ord_qty": "2",
        "org_ord_no": "0000000101",
    }


def test_send_cancel_without_an_order_number_is_rejected() -> None:
    s = Setup()
    s.nh.reply(CANCEL, {"ord_no": "", "msg_cd": "40300", "msg_cntn": "취소가능수량 없음"})
    target = Order("0000000101", "", SYMBOL, "", "buy", "접수", 1, None, 0, None, 1, "")

    with pytest.raises(Rejected):
        asyncio.run(s.market.send_cancel(s.user, target))


def test_orderable_qty_reads_the_recorded_answer() -> None:
    s = Setup()
    s.nh.reply(ORDERABLE_QTY, recorded("orderable-quantity"))

    qty = asyncio.run(s.market.orderable_qty(s.user, LIMIT_BUY, None))

    assert qty == 2
    assert s.body_of(ORDERABLE_QTY) == {
        "ord_sts": "1",
        "sym": SYMBOL,
        "trd_div": "1",
        "ord_pric": "784.55",
    }


def test_market_orderable_qty_asks_at_the_upper_limit() -> None:
    s = Setup()
    s.nh.reply(ORDERABLE_QTY, recorded("orderable-quantity"))

    qty = asyncio.run(s.market.orderable_qty(s.user, MARKET_SELL, Decimal("835.60")))

    assert qty == 2
    assert s.body_of(ORDERABLE_QTY)["ord_pric"] == "835.60"
    assert s.body_of(ORDERABLE_QTY)["trd_div"] == "2"


def test_market_orderable_qty_without_an_upper_limit_is_unknown_and_not_asked() -> None:
    s = Setup()

    assert asyncio.run(s.market.orderable_qty(s.user, MARKET_SELL, None)) is None
    assert ORDERABLE_QTY not in s.nh.paths()


def test_unreadable_orderable_qty_is_unknown() -> None:
    s = Setup()
    s.nh.reply(ORDERABLE_QTY, {"ord_psb_qty": ""})

    assert asyncio.run(s.market.orderable_qty(s.user, LIMIT_BUY, None)) is None


def history_row(**changes: str) -> dict[str, str]:
    row = {
        "occurs1_ord_no": "0000000101",
        "occurs1_org_ord_no": "",
        "occurs1_sym": SYMBOL,
        "occurs1_sym_nm": "코스피200 F 202612",
        "occurs1_trd_div": "1",
        "occurs1_ord_sts": "1",
        "occurs1_ord_sts_nm": "접수",
        "occurs1_ord_qty": "1",
        "occurs1_ord_pric": "784.55",
        "occurs1_exec_qty": "0",
        "occurs1_rem_ord_qty": "1",
        "occurs1_ord_div_cd": "1",
        "occurs1_exch_rcv_tm": "100001",
    }
    return row | changes


def test_todays_orders_reads_division_receive_time_and_state() -> None:
    s = Setup()
    rows = [
        history_row(),
        history_row(occurs1_ord_no="2", occurs1_ord_sts="6", occurs1_ord_div_cd="2"),
        history_row(occurs1_ord_no="3", occurs1_ord_sts="8", occurs1_ord_div_cd="3"),
        history_row(occurs1_ord_no="4", occurs1_ord_sts="9", occurs1_exch_rcv_tm=""),
        history_row(occurs1_ord_no="5", occurs1_ord_sts="4", occurs1_exch_rcv_tm="1000"),
        history_row(occurs1_ord_no="6", occurs1_ord_div_cd="7", occurs1_exch_rcv_tm="250000"),
    ]
    s.nh.reply(ORDER_HISTORY, {"nrec": "0006", "nxt_key": "", "Occurs1": rows})

    found = asyncio.run(s.market.todays_orders(s.user))

    assert s.body_of(ORDER_HISTORY) == {
        "trd_div": "0",
        "exec_div": "0",
        "trd_tm_div_cd": "0",
        "ord_div_cd": "0",
        "nxt_key": "",
    }
    first = found[0]
    assert isinstance(first, OrderRow)
    assert (first.order.order_no, first.order.quantity, first.order.price) == (
        "0000000101",
        1,
        Decimal("784.55"),
    )
    assert [(r.division, r.received, r.state) for r in found] == [
        ("new", time(10, 0, 1), "accepted"),
        ("modify", time(10, 0, 1), "filled"),
        ("cancel", time(10, 0, 1), "cancelled"),
        ("new", None, "rejected"),
        ("new", None, "accepted"),
        (None, None, "accepted"),
    ]


def test_todays_fills_asks_for_filled_rows_and_reads_quantity_and_price() -> None:
    s = Setup()
    rows = [
        history_row(occurs1_exec_qty="2", occurs1_exec_pric="1.25", occurs1_rem_ord_qty="0"),
        history_row(occurs1_trd_div="2", occurs1_exec_qty="1", occurs1_exec_pric="3.10"),
        history_row(occurs1_exec_qty="0", occurs1_exec_pric=""),  # 체결 없는 행은 뺀다
        history_row(occurs1_sym="", occurs1_trd_div=""),  # 빈 행
    ]
    s.nh.reply(ORDER_HISTORY, {"nrec": "0004", "nxt_key": "", "Occurs1": rows})

    fills = asyncio.run(s.market.todays_fills(s.user))

    assert s.body_of(ORDER_HISTORY)["exec_div"] == "1"
    assert fills == [
        Fill(SYMBOL, "buy", 2, Decimal("1.25")),
        Fill(SYMBOL, "sell", 1, Decimal("3.10")),
    ]


@pytest.mark.parametrize(
    "bad",
    [
        {"occurs1_exec_qty": "1", "occurs1_exec_pric": ""},
        {"occurs1_exec_qty": "", "occurs1_exec_pric": "1.00"},
        {"occurs1_exec_qty": "-1", "occurs1_exec_pric": "1.00"},
        {"occurs1_trd_div": "9", "occurs1_exec_qty": "1", "occurs1_exec_pric": "1.00"},
    ],
)
def test_todays_fills_with_an_unreadable_row_is_unknown(bad: dict[str, str]) -> None:
    s = Setup()
    rows = [history_row(occurs1_exec_qty="1", occurs1_exec_pric="1.00"), history_row(**bad)]
    s.nh.reply(ORDER_HISTORY, {"nrec": "0002", "nxt_key": "", "Occurs1": rows})

    assert asyncio.run(s.market.todays_fills(s.user)) is None


def test_todays_fills_that_never_reach_the_last_page_are_unknown() -> None:
    s = Setup()
    page = [history_row(occurs1_exec_qty="1", occurs1_exec_pric="1.00")]
    # 다음 쪽 열쇠가 되풀이되면 끝까지 받지 못한 것이다.
    s.nh.reply(ORDER_HISTORY, {"nrec": "0001", "nxt_key": "K1", "Occurs1": page})

    assert asyncio.run(s.market.todays_fills(s.user)) is None
