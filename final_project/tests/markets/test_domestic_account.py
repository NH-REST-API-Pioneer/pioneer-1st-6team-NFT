"""계좌 상태를 다 읽었는가(`AccountState.complete`).

리스크 규칙은 보유·미체결·평가손익을 이 값으로 본다. 빈 칸 아닌 행을 읽지 못했거나 페이지를
끝까지 받지 못했으면 complete=False이고, 그러면 모든 주문이 막힌다(규칙 account_unknown).
보유·미체결 행은 녹화가 빈 계좌뿐이라 명세 필드로 만든 가짜다.
"""

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fakes import FakeNH, World

from nh_mcp.domain import AccountState, UserId
from nh_mcp.markets.domestic import (
    DEPOSIT,
    MAX_PAGES,
    OPEN_INTEREST,
    ORDER_HISTORY,
    DomesticMarket,
)
from nh_mcp.markets.domestic_master import DomesticMaster
from nh_mcp.nh.client import Credentials
from nh_mcp.store import users

RECORDED = Path(__file__).resolve().parents[1] / "fixtures" / "nh" / "domestic"
SYMBOL = "A016C000"


def recorded(name: str) -> dict[str, Any]:
    body: dict[str, Any] = json.loads((RECORDED / f"{name}.json").read_text("utf-8"))["body"]
    return body


def page(*rows: dict[str, str], key: str = "") -> dict[str, object]:
    return {"nrec": f"{len(rows):04d}", "nxt_key": key, "Occurs1": list(rows)}


def held(**changes: str) -> dict[str, str]:
    row = {
        "occurs1_sym": SYMBOL,
        "occurs1_ko_sym_nm": "코스피200 F 202612",
        "occurs1_trd_div": "1",
        "occurs1_opint_qty": "2",
        "occurs1_cls_psb_qty": "2",
        "occurs1_pl_amt": "0",
    }
    return row | changes


def resting(**changes: str) -> dict[str, str]:
    row = {
        "occurs1_ord_no": "0000000301",
        "occurs1_sym": SYMBOL,
        "occurs1_sym_nm": "코스피200 F 202612",
        "occurs1_trd_div": "2",
        "occurs1_ord_sts_nm": "접수",
        "occurs1_ord_qty": "1",
        "occurs1_ord_pric": "784.55",
        "occurs1_rem_ord_qty": "1",
    }
    return row | changes


def without(row: dict[str, str], key: str) -> dict[str, str]:
    return {k: v for k, v in row.items() if k != key}


class Setup:
    def __init__(self) -> None:
        self.nh = FakeNH()
        self.nh.reply(DEPOSIT, recorded("deposit"))
        self.world = World(nh=httpx2.MockTransport(self.nh))
        self.user: UserId = users.get_or_create(
            self.world.conn, "g-admin", "admin@example.com", self.world.clock()
        )
        asyncio.run(
            self.world.store.register(self.user, "domestic", Credentials("fake-key-1", "x-sec-1"))
        )
        master = DomesticMaster(self.world.clock, httpx2.MockTransport(self.world.master))
        self.market = DomesticMarket(self.world.pool, master, self.world.clock)

    def account(self) -> AccountState:
        return asyncio.run(self.market.account(self.user))


def test_the_recorded_empty_account_is_complete() -> None:
    s = Setup()
    s.nh.reply(OPEN_INTEREST, recorded("open-interest"))
    s.nh.reply(ORDER_HISTORY, recorded("order-history"))

    assert s.account().complete is True


def test_readable_rows_are_complete() -> None:
    s = Setup()
    s.nh.reply(OPEN_INTEREST, page(held()))
    s.nh.reply(ORDER_HISTORY, page(resting()))

    account = s.account()

    assert account.complete is True
    assert [p.quantity for p in account.positions] == [2]
    assert [o.remaining_quantity for o in account.open_orders] == [1]


@pytest.mark.parametrize(
    "row",
    [
        held(occurs1_trd_div=""),
        held(occurs1_trd_div="3"),
        held(occurs1_opint_qty=""),
        held(occurs1_opint_qty="1.5"),
        held(occurs1_opint_qty="x"),
        held(occurs1_cls_psb_qty=""),
        without(held(), "occurs1_cls_psb_qty"),
    ],
)
def test_an_unreadable_position_row_makes_the_account_incomplete(row: dict[str, str]) -> None:
    s = Setup()
    s.nh.reply(OPEN_INTEREST, page(held(occurs1_sym="A656C000"), row))

    account = s.account()

    assert account.complete is False
    # 화면(get_account)에는 읽은 행을 그대로 보여 준다.
    assert "A656C000" in [p.symbol for p in account.positions]


@pytest.mark.parametrize(
    "row",
    [
        resting(occurs1_trd_div=""),
        resting(occurs1_rem_ord_qty=""),
        resting(occurs1_rem_ord_qty="x"),
        without(resting(), "occurs1_rem_ord_qty"),
    ],
)
def test_an_unreadable_open_order_row_makes_the_account_incomplete(row: dict[str, str]) -> None:
    s = Setup()
    s.nh.reply(ORDER_HISTORY, page(row))

    assert s.account().complete is False


def test_blank_rows_are_skipped_and_keep_the_account_complete() -> None:
    s = Setup()
    blank_position = {key: "" for key in held()}
    blank_order = {key: "" for key in resting()}
    s.nh.reply(OPEN_INTEREST, page(held(), blank_position))
    s.nh.reply(ORDER_HISTORY, page(blank_order))

    account = s.account()

    assert account.complete is True
    assert [p.symbol for p in account.positions] == [SYMBOL]
    assert account.open_orders == []


@pytest.mark.parametrize("path", [OPEN_INTEREST, ORDER_HISTORY])
def test_a_repeated_next_key_makes_the_account_incomplete(path: str) -> None:
    s = Setup()
    row = held() if path == OPEN_INTEREST else resting()
    s.nh.reply(path, page(row, key="K1"))  # 늘 같은 키를 돌려준다

    account = s.account()

    assert account.complete is False
    assert s.nh.paths().count(path) == 2


@pytest.mark.parametrize("path", [OPEN_INTEREST, ORDER_HISTORY])
def test_stopping_at_the_page_cap_makes_the_account_incomplete(path: str) -> None:
    s = Setup()
    row = held() if path == OPEN_INTEREST else resting()
    for n in range(1, MAX_PAGES + 2):
        s.nh.reply(path, page(row, key=f"K{n}"))

    account = s.account()

    assert account.complete is False
    assert s.nh.paths().count(path) == MAX_PAGES


def test_a_natural_end_on_the_last_allowed_page_is_complete() -> None:
    s = Setup()
    for n in range(1, MAX_PAGES):
        s.nh.reply(OPEN_INTEREST, page(held(), key=f"K{n}"))
    s.nh.reply(OPEN_INTEREST, page(held()))

    account = s.account()

    assert account.complete is True
    assert len(account.positions) == MAX_PAGES


def test_next_data_no_ends_the_pages_naturally() -> None:
    s = Setup()
    s.nh.reply(ORDER_HISTORY, page(resting(), key="K1") | {"nxt_data_yn": "N"})

    assert s.account().complete is True
    assert s.nh.paths().count(ORDER_HISTORY) == 1
