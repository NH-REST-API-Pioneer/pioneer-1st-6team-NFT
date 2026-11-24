"""조회 도구 테스트. NH 응답은 명세의 출력 필드 이름으로 만든 가짜다(녹화 전)."""

import asyncio
import json
from typing import Any

import httpx2
from conftest import Serve
from fakes import FAKE_NH_TOKEN, FakeNH, World
from fastmcp import Client

from nh_mcp.nh.client import Credentials
from nh_mcp.store import users

API = "/trade/v1/domestic/"
FAKE_KEY = "fake-app-key-1234"
FAKE_SECRET = "fake-app-secret-0000"
ACCOUNT_NO, ACCOUNT_NAME, USER_IP = "12345678901", "홍길동", "10.20.30.40"

PRICE = {
    "sym_exch": "A016C000",
    "sym_nm": "코스피200 F 202612",
    "last_pric": "350.25",
    "chg": "1.50",
    "rt": "0.43",
    "open_pric": "348.80",
    "high_pric": "351.00",
    "low_pric": "348.50",
    "trd_qty": "123456",
    "up": "378.00",
    "lo": "322.00",
}
PRICE_BID = {
    "nrec": "1",
    "Occurs1": [
        {
            "occurs1_bid_best_pric": "350.20",
            "occurs1_bid_qty": "12",
            "occurs1_ask_best_pric": "350.25",
            "occurs1_ask_qty": "9",
        }
    ],
}


def world_with_key(nh: FakeNH, register: bool = True) -> World:
    world = World(nh=httpx2.MockTransport(nh))
    if register:
        user = users.get_or_create(world.conn, "g-admin", "admin@example.com", world.clock())
        asyncio.run(world.store.register(user, "domestic", Credentials(FAKE_KEY, FAKE_SECRET)))
    return world


async def call(serve: Serve, world: World, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    async with serve(world.mcp) as url, Client(url, auth="admin-token") as client:
        content = (await client.call_tool(tool, args)).structured_content
    assert content is not None
    return content


def run(serve: Serve, world: World, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    return asyncio.run(call(serve, world, tool, args))


def test_quote_without_key_points_to_the_registration_page(serve: Serve) -> None:
    nh = FakeNH()
    result = run(serve, world_with_key(nh, register=False), "get_quote", {"symbol": "A016C000"})

    assert result["ok"] is False
    assert result["error"]["code"] == "KEY_NOT_REGISTERED"
    assert "http://localhost:8000/keys" in result["error"]["message"]
    assert nh.requests == []


def test_made_up_symbol_never_reaches_nh(serve: Serve) -> None:
    nh = FakeNH()
    result = run(serve, world_with_key(nh), "get_quote", {"symbol": "A9999999"})

    assert result["error"]["code"] == "INVALID_INPUT"
    assert nh.requests == []


def test_quote_reads_prices_as_strings(serve: Serve) -> None:
    nh = FakeNH()
    nh.reply(API + "price", PRICE)
    nh.reply(API + "price-bid", PRICE_BID)
    result = run(serve, world_with_key(nh), "get_quote", {"symbol": "A016C000"})

    assert result["ok"] is True
    data = result["data"]
    assert (data["last"], data["change"], data["volume"]) == ("350.25", "1.50", 123456)
    assert data["book"] == [{"bid": "350.20", "bid_qty": 12, "ask": "350.25", "ask_qty": 9}]
    assert nh.requests[0] == (API + "price", {"sym": "A016C000"})
    assert nh.requests[1] == (API + "price-bid", {"exch_cd": "KSP", "sym": "A016C000"})


def test_nh_refusal_and_silence_become_error_codes(serve: Serve) -> None:
    refused = FakeNH()
    refused.reply(API + "price", {"errorCode": "ETR029", "errorMessage": "종목 정보 없음"})
    silent = FakeNH()
    silent.down = True

    rejected = run(serve, world_with_key(refused), "get_quote", {"symbol": "A016C000"})
    unavailable = run(serve, world_with_key(silent), "get_quote", {"symbol": "A016C000"})

    assert rejected["error"]["code"] == "BROKER_REJECTED"
    assert "ETR029" in rejected["error"]["message"]
    assert unavailable["error"]["code"] == "TEMPORARILY_UNAVAILABLE"
    assert unavailable["error"]["retryable"] is True


def candle_row(day: str, close: str) -> dict[str, str]:
    return {
        "occurs1_ref_dt": day,
        "occurs1_open_pric": close,
        "occurs1_high_pric": close,
        "occurs1_low_pric": close,
        "occurs1_close_pric": close,
        "occurs1_cum_trd_qty": "1000",
    }


def test_candles_follow_pages_merge_and_sort(serve: Serve) -> None:
    nh = FakeNH()
    nh.reply(
        API + "chart-daily",
        {
            "nxt_key": "K1",
            "Occurs1": [candle_row("20261130", "352"), candle_row("20261127", "350")],
        },
    )
    nh.reply(
        API + "chart-daily",
        {"nxt_key": "", "Occurs1": [candle_row("20261126", "349"), candle_row("20261127", "350")]},
    )
    args = {"symbol": "A016C000", "period": {"start": "2026-11-01", "end": "2026-11-30"}}
    result = run(serve, world_with_key(nh), "get_candles", args)

    candles = result["data"]["candles"]
    assert [c["close"] for c in candles] == ["349", "350", "352"]
    assert candles[0]["at"] == "2026-11-26T00:00:00+09:00"
    first, second = nh.requests[0][1], nh.requests[1][1]
    assert (first["nxt_key"], first["req_qty"], first["nght_tp"]) == ("99999999", "200", "1")
    assert second["nxt_key"] == "K1"


def test_candles_reject_bad_period(serve: Serve) -> None:
    world = world_with_key(FakeNH())
    reversed_period = {"start": "2026-11-30", "end": "2026-11-01"}
    for args in (
        {"symbol": "A016C000", "period": reversed_period},
        {"symbol": "A016C000", "period": {"start": "어제"}},
        {"symbol": "A016C000", "limit": 0},
    ):
        assert run(serve, world, "get_candles", args)["error"]["code"] == "INVALID_INPUT"


def test_account_sums_pnl_and_never_shows_account_identity(serve: Serve) -> None:
    nh = FakeNH()
    nh.reply(
        API + "deposit",
        {
            "dps_aset_tot_amt": "50000000",
            "ord_psb_tot_amt": "45000000",
            "fut_rez_pl_amt": "120000",
            "opt_rez_pl_amt": "-20000",
        },
    )
    position = {
        "occurs1_ac_no": ACCOUNT_NO,
        "occurs1_ac_nm": ACCOUNT_NAME,
        "occurs1_sym": "A016C000",
        "occurs1_ko_sym_nm": "코스피200 F 202612",
        "occurs1_trd_div": "1",
        "occurs1_opint_qty": "2",
        "occurs1_cls_psb_qty": "2",
        "occurs1_avg_pric": "349.00",
        "occurs1_close_pric": "350.25",
        "occurs1_pl_amt": "625000",
    }
    nh.reply(API + "open-interest", {"nxt_key": "", "nrec": "1", "Occurs1": [position]})
    open_order = {
        "occurs1_ac_no": ACCOUNT_NO,
        "occurs1_ac_nm": ACCOUNT_NAME,
        "occurs1_user_ip_addr": USER_IP,
        "occurs1_ord_no": "1001",
        "occurs1_sym": "A016C000",
        "occurs1_trd_div": "2",
        "occurs1_ord_sts_nm": "접수",
        "occurs1_ord_qty": "1",
        "occurs1_ord_pric": "355.00",
        "occurs1_rem_ord_qty": "1",
    }
    nh.reply(API + "order-history", {"nxt_key": "", "nrec": "1", "Occurs1": [open_order]})
    result = run(serve, world_with_key(nh), "get_account", {})

    data = result["data"]
    assert data["complete"] is True
    assert (data["deposit"], data["orderable_amount"]) == ("50000000", "45000000")
    assert (data["realized_pnl"], data["unrealized_pnl"]) == ("100000", "625000")
    assert data["positions"][0]["side"] == "buy" and data["positions"][0]["quantity"] == 2
    assert data["open_orders"][0]["order_no"] == "1001"
    shown = json.dumps(result, ensure_ascii=False)
    for secret in (ACCOUNT_NO, ACCOUNT_NAME, USER_IP, FAKE_KEY, FAKE_SECRET, FAKE_NH_TOKEN):
        assert secret not in shown
    history_payload = dict(nh.requests)[API + "order-history"]
    assert history_payload["exec_div"] == "2"


def test_unknown_pnl_stays_unknown(serve: Serve) -> None:
    nh = FakeNH()
    nh.reply(API + "deposit", {"dps_aset_tot_amt": "1", "ord_psb_tot_amt": "1"})
    result = run(serve, world_with_key(nh), "get_account", {})
    assert result["data"]["realized_pnl"] is None


def test_account_says_when_it_could_not_read_everything(serve: Serve) -> None:
    nh = FakeNH()
    unreadable = {"occurs1_sym": "A016C000", "occurs1_trd_div": "", "occurs1_opint_qty": "1"}
    nh.reply(API + "open-interest", {"nxt_key": "", "nrec": "1", "Occurs1": [unreadable]})
    result = run(serve, world_with_key(nh), "get_account", {})

    assert result["ok"] is True
    assert result["data"]["complete"] is False


def test_orders_returns_history_and_errors(serve: Serve) -> None:
    nh = FakeNH()
    nh.reply(
        API + "order-history",
        {
            "nxt_key": "",
            "Occurs1": [
                {
                    "occurs1_ord_no": "1002",
                    "occurs1_sym": "A016C000",
                    "occurs1_trd_div": "1",
                    "occurs1_ord_sts_nm": "거부",
                    "occurs1_ord_rejt_rsn": "주문증거금총액 부족",
                    "occurs1_user_ip_addr": USER_IP,
                }
            ],
        },
    )
    nh.reply(
        API + "order-error",
        {
            "nxt_data_yn": "N",
            "Occurs1": [
                {
                    "occurs1_rcv_tm": "101500",
                    "occurs1_sym": "A016C000",
                    "occurs1_msg_cd": "52",
                    "occurs1_msg": "주문증거금총액 부족",
                    "occurs1_trd_div_cd": "1",
                    "occurs1_ac_no": ACCOUNT_NO,
                }
            ],
        },
    )
    result = run(serve, world_with_key(nh), "get_orders", {"day": "2026-12-01"})

    assert result["data"]["orders"][0]["reject_reason"] == "주문증거금총액 부족"
    assert result["data"]["order_errors"][0]["code"] == "52"
    assert dict(nh.requests)[API + "order-error"]["inq_dt"] == "20261201"
    shown = json.dumps(result, ensure_ascii=False)
    assert ACCOUNT_NO not in shown and USER_IP not in shown
    assert run(serve, world_with_key(nh), "get_orders", {"day": "12/01"})["error"]["code"] == (
        "INVALID_INPUT"
    )
