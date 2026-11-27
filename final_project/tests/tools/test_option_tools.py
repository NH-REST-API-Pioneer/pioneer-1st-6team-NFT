"""get_option_chain과 옵션 종목의 get_quote · get_candles, "옵션" 묶음.

전광판은 녹화한 2026-12물 세 쪽(줄인 것, `tests/markets/test_domestic_options.py`)을 차례로 준다.
마스터에는 같은 날 받은 옵션 견본을 붙인다. 시계는 2026-12-01이라 가장 가까운 만기는 2026-12-10이다.
admin-token의 사용자는 "옵션"을 켰고, second-admin-token의 사용자는 켜지 않았다.
"""

import asyncio
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2
from conftest import Serve
from fakes import SETTINGS, FakeNH, World, options_on
from fastmcp import Client

from nh_mcp.auth.web import KEYS_PATH
from nh_mcp.domain import UserId
from nh_mcp.nh.client import Credentials
from nh_mcp.nh.recording import Fixture
from nh_mcp.store import users

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
RECORDED = FIXTURES / "nh" / "domestic"
OPTION_MASTER = FIXTURES / "master" / "kospi200_options.mst"
API = "/trade/v1/domestic/"
BOARD = API + "price-all-option"
KEYS_URL = SETTINGS.public_base_url + KEYS_PATH
OFF = "second-admin-token"
MONTHLY = [
    "2026-12-10",
    "2027-01-14",
    "2027-02-11",
    "2027-03-11",
    "2027-04-08",
    "2027-05-12",
    "2027-06-10",
    "2027-09-09",
    "2027-12-09",
    "2028-06-08",
    "2028-12-14",
]


def world_with_board() -> tuple[World, FakeNH]:
    nh = FakeNH()
    for n in (1, 2, 3):
        body = Fixture.load(RECORDED / f"price-all-option-{n}.json").body
        assert isinstance(body, dict)
        nh.reply(BOARD, body)
    world = World(nh=httpx2.MockTransport(nh))
    world.master.extra["fut_opt"] = OPTION_MASTER.read_text(encoding="utf-8")
    options_on(world.conn, registered(world, "g-admin", "admin@example.com"))
    registered(world, "g-admin2", "second@example.com")
    return world, nh


def registered(world: World, sub: str, email: str) -> UserId:
    user = users.get_or_create(world.conn, sub, email, world.clock())
    credentials = Credentials("fake-app-key-1234", "fake-app-secret-0000")
    asyncio.run(world.store.register(user, "domestic", credentials))
    return user


def run(
    serve: Serve, world: World, *calls: tuple[str, dict[str, Any]], token: str = "admin-token"
) -> list[dict[str, Any]]:
    async def call() -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        async with serve(world.mcp) as url, Client(url, auth=token) as client:
            for tool, args in calls:
                content = (await client.call_tool(tool, args)).structured_content
                assert content is not None
                results.append(content)
        return results

    return asyncio.run(call())


def strikes(data: dict[str, Any]) -> list[str]:
    return [r["strike"] for r in data["rows"]]


def around(center: str, n: int) -> list[str]:
    return [f"{Decimal(center) + Decimal('2.5') * k:.2f}" for k in range(-n, n + 1)]


def test_without_expiry_answers_the_nearest_month_around_the_money(serve: Serve) -> None:
    world, nh = world_with_board()
    [result] = run(serve, world, ("get_option_chain", {}))

    assert result["ok"] is True
    data = result["data"]
    assert data["expiries"] == MONTHLY
    assert (data["underlying"], data["expiry"]) == ("KOSPI200", "2026-12-10")
    assert (data["spot"], data["atm_strike"]) == ("727.08", "727.50")
    assert (data["future_symbol"], data["future_price"]) == ("A016C000", "726.75")
    assert (data["strikes_total"], data["dropped_codes"]) == (95, 0)
    assert strikes(data) == around("727.50", 10)
    assert [p["exp_mon"] for _, p in nh.requests] == ["261210"] * 3


def test_rows_carry_both_sides_and_a_missing_quote_is_null(serve: Serve) -> None:
    world, _ = world_with_board()
    [result] = run(serve, world, ("get_option_chain", {"strikes_around": 0}))

    [at_money] = result["data"]["rows"]
    assert at_money["strike"] == "727.50"
    # 녹화의 콜 매도호가는 "0.00"(호가 없음)이었다.
    assert at_money["call"] == {
        "symbol": "B016C727",
        "last": "35.50",
        "bid": "2.03",
        "ask": None,
        "volume": 2,
        "open_interest": 15,
        "iv": "58.80",
        "delta": "0.52563",
        "theoretical": "33.26",
    }
    assert at_money["put"]["symbol"] == "C016C727"
    assert (at_money["put"]["bid"], at_money["put"]["ask"]) == ("32.40", "36.45")


def test_strikes_around_goes_up_to_forty_each_way(serve: Serve) -> None:
    world, _ = world_with_board()
    [result] = run(serve, world, ("get_option_chain", {"strikes_around": 40}))

    assert strikes(result["data"]) == around("727.50", 40)


def test_bad_arguments_are_rejected_before_nh(serve: Serve) -> None:
    world, nh = world_with_board()
    bad: list[dict[str, Any]] = [
        {"strikes_around": 41},
        {"strikes_around": -1},
        {"strikes_around": True},
        {"strikes_around": 2.5},
        {"strikes_around": "10"},
        {"expiry": "261210"},
        {"expiry": "2026-13-10"},
        # 위클리 만기와 만기가 아닌 날
        {"expiry": "2026-12-03"},
        {"expiry": "2026-12-11"},
    ]
    results = run(serve, world, *(("get_option_chain", args) for args in bad))

    assert [r["error"]["code"] for r in results] == ["INVALID_INPUT"] * len(bad)
    assert nh.requests == []


def test_a_given_expiry_is_asked_for_and_codes_of_another_month_never_get_out(
    serve: Serve,
) -> None:
    # 가짜 NH는 1월물을 물어도 녹화한 12월물을 준다. 12월물 코드는 1월물 마스터 종목이 아니다.
    world, nh = world_with_board()
    [result] = run(serve, world, ("get_option_chain", {"expiry": "2027-01-14"}))

    data = result["data"]
    assert data["expiry"] == "2027-01-14"
    assert [p["exp_mon"] for _, p in nh.requests] == ["270114"] * 3
    assert all(r["call"] is None and r["put"] is None for r in data["rows"])
    assert data["dropped_codes"] == 2 * 95


# 옵션 시세. 칸 이름은 명세(`domestic_price.md`), 값은 2026-11-26 같은 종목의 전광판에서.
OPTION_PRICE = {
    "sym_exch": "B016C727",
    "sym_nm": "코스피200 C 202612   727.5",
    "last_pric": "35.50",
    "chg": "-1.75",
    "rt": "-4.70",
    "open_pric": "38.00",
    "high_pric": "41.00",
    "low_pric": "35.50",
    "trd_qty": "2",
    "up": "102.80",
    "lo": "0.01",
    "ivol": "58.80",
    "delta": "0.52563",
    "theo_pric": "33.26",
}


def test_option_quote_adds_the_option_block(serve: Serve) -> None:
    world, nh = world_with_board()
    nh.reply(API + "price", OPTION_PRICE)
    nh.reply(API + "price-bid", {"nrec": "0", "Occurs1": []})
    [result] = run(serve, world, ("get_quote", {"symbol": "B016C727"}))

    data = result["data"]
    assert data["last"] == "35.50"
    assert data["option"] == {
        "kind": "call",
        "strike": "727.50",
        "expiry": "2026-12-10",
        "iv": "58.80",
        "delta": "0.52563",
        "theoretical": "33.26",
    }


# ---- "옵션" 묶음 ---------------------------------------------------------------

FUTURE_PRICE = {
    "sym_exch": "A016C000",
    "sym_nm": "코스피200 F 202612",
    "last_pric": "726.75",
    "up": "786.00",
    "lo": "667.00",
}
OPTION_DAY = {
    "occurs1_ref_dt": "20261130",
    "occurs1_open_pric": "38.00",
    "occurs1_high_pric": "41.00",
    "occurs1_low_pric": "35.50",
    "occurs1_close_pric": "35.50",
    "occurs1_cum_trd_qty": "2",
}


# (오류 코드, 규칙, 문구에 등록 화면 주소가 있는가)
OPTIONS_OFF = ("RISK_BLOCKED", "options_off", True)


def refusal(result: dict[str, Any]) -> tuple[str, str, bool]:
    error = result["error"]
    return error["code"], error["rule"], KEYS_URL in error["message"]


def test_only_the_user_who_turned_options_on_lists_and_gets_the_chain(serve: Serve) -> None:
    world, nh = world_with_board()

    async def scenario() -> tuple[set[str], set[str], dict[str, Any]]:
        async with (
            serve(world.mcp) as url,
            Client(url, auth="admin-token") as on,
            Client(url, auth=OFF) as off,
        ):
            shown = {t.name for t in await on.list_tools()}
            hidden = {t.name for t in await off.list_tools()}
            refused = (await off.call_tool("get_option_chain", {})).structured_content
            assert refused is not None
            return shown, hidden, refused

    shown, hidden, refused = asyncio.run(scenario())

    assert "get_option_chain" in shown
    assert "get_option_chain" not in hidden
    assert {"get_quote", "get_candles", "search_symbol"} <= hidden
    assert refusal(refused) == OPTIONS_OFF
    assert nh.requests == []


def test_option_quotes_and_candles_need_the_switch_and_futures_do_not(serve: Serve) -> None:
    world, nh = world_with_board()
    nh.by_symbol[(API + "price", "A016C000")] = FUTURE_PRICE
    nh.reply(API + "price-bid", {"nrec": "0", "Occurs1": []})
    nh.reply(API + "chart-daily", {"nxt_key": "", "Occurs1": [OPTION_DAY]})
    option = {"symbol": "B016C727"}

    quote, candles, future = run(
        serve,
        world,
        ("get_quote", option),
        ("get_candles", option),
        ("get_quote", {"symbol": "A016C000"}),
        token=OFF,
    )
    asked_while_off = [payload.get("sym") for _, payload in nh.requests]
    [allowed] = run(serve, world, ("get_candles", option))

    assert refusal(quote) == refusal(candles) == OPTIONS_OFF
    assert future["ok"] is True and future["data"]["last"] == "726.75"
    assert asked_while_off == ["A016C000", "A016C000"]
    assert allowed["ok"] is True
    assert [c["close"] for c in allowed["data"]["candles"]] == ["35.50"]
