"""get_account의 옵션 칸(M5): 잔고 행의 kind와, 최근 7일 안에 만기로 닫힌 옵션 expired_options.

마스터에는 같은 날 받은 옵션 견본을 붙인다. 2026-12물 옵션의 만기일은 2026-12-10(목)이다.
잔고는 명세 필드로 만든 가짜다. admin-token과 second-admin-token은 서로 다른 사용자다.
"""

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx2
from conftest import Serve
from fakes import FakeNH, World
from fastmcp import Client

from nh_mcp.domain import KST
from nh_mcp.markets.domestic import OPEN_INTEREST, ORDER_HISTORY
from nh_mcp.nh.client import Credentials
from nh_mcp.store import users

OPTION_MASTER = Path(__file__).resolve().parents[1] / "fixtures" / "master" / "kospi200_options.mst"
CALL, PUT, FUTURE, NOT_IN_MASTER = "B016C725", "C016C725", "A016C000", "B016CZZZ"
OTHER = "second-admin-token"

# 도구 하나를 부르기 전에 시각과 잔고를 정하는 단계: (KST 시각, 잔고 행, 토큰).
type Step = tuple[datetime, list[dict[str, str]], str]


def kst(day: int, hour: int = 10) -> datetime:
    return datetime(2026, 12, day, hour, 0, tzinfo=KST)


def held(symbol: str, side: str = "1", quantity: int = 1) -> dict[str, str]:
    """잔고 한 행. side는 NH 코드(1 매수, 2 매도)."""
    return {
        "occurs1_sym": symbol,
        "occurs1_ko_sym_nm": symbol,
        "occurs1_trd_div": side,
        "occurs1_opint_qty": str(quantity),
        "occurs1_cls_psb_qty": str(quantity),
    }


def occurs(rows: list[dict[str, str]]) -> dict[str, object]:
    return {"nrec": f"{len(rows):04d}", "nxt_key": "", "Occurs1": rows}


def setup() -> tuple[World, FakeNH]:
    nh = FakeNH()
    nh.reply(ORDER_HISTORY, occurs([]))
    world = World(nh=httpx2.MockTransport(nh))
    world.master.extra["fut_opt"] = OPTION_MASTER.read_text(encoding="utf-8")
    for sub, email in (("g-admin", "admin@example.com"), ("g-admin2", "second@example.com")):
        user = users.get_or_create(world.conn, sub, email, world.clock())
        credentials = Credentials("fake-app-key-1234", "fake-app-secret-0000")
        asyncio.run(world.store.register(user, "domestic", credentials))
    return world, nh


def accounts(serve: Serve, *steps: Step, master_down: bool = False) -> list[dict[str, Any]]:
    """단계마다 시계와 잔고를 바꾸고 get_account를 불러 data를 모은다."""
    world, nh = setup()
    world.master.down = master_down

    async def call() -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        async with serve(world.mcp) as url:
            for at, rows, token in steps:
                world.clock.now = at
                nh.replies[OPEN_INTEREST] = [(200, occurs(rows))]
                async with Client(url, auth=token) as client:
                    content = (await client.call_tool("get_account", {})).structured_content
                assert content is not None and content["ok"] is True, content
                results.append(content["data"])
        return results

    return asyncio.run(call())


def step(at: datetime, *rows: dict[str, str], token: str = "admin-token") -> Step:
    return at, list(rows), token


def expired(data: dict[str, Any]) -> list[tuple[str, str]]:
    return [(e["symbol"], e["closed_at"]) for e in data["expired_options"]]


def test_positions_say_whether_they_are_futures_calls_or_puts(serve: Serve) -> None:
    [data] = accounts(
        serve, step(kst(1), held(FUTURE), held(CALL), held(PUT, "2"), held(NOT_IN_MASTER))
    )

    kinds = {p["symbol"]: p["kind"] for p in data["positions"]}
    assert kinds == {FUTURE: "future", CALL: "call", PUT: "put", NOT_IN_MASTER: None}
    assert data["expired_options"] == []


def test_the_account_still_answers_when_the_master_is_down(serve: Serve) -> None:
    [data] = accounts(serve, step(kst(1), held(FUTURE), held(CALL)), master_down=True)

    assert [p["kind"] for p in data["positions"]] == [None, None]
    assert data["complete"] is True


def test_options_gone_after_expiry_are_listed_for_seven_days(serve: Serve) -> None:
    results = accounts(
        serve,
        step(kst(1), held(CALL, "1", 2), held(PUT, "2")),
        step(kst(5), held(CALL, "1", 2)),  # 만기 전에 풋이 사라짐: 청산
        step(kst(11)),  # 만기 다음 날 콜이 사라짐: 행사 또는 소멸
        step(kst(11, 15)),
        step(kst(18, 9)),
        step(kst(18, 11)),
    )

    first, before_expiry, after_expiry, again, sixth_day, eighth_day = results
    assert first["expired_options"] == before_expiry["expired_options"] == []
    assert after_expiry["expired_options"] == [
        {
            "symbol": CALL,
            "side": "buy",
            "quantity": 2,
            "expiry": "2026-12-10",
            "closed_at": "2026-12-11T10:00:00+09:00",
        }
    ]
    assert again["expired_options"] == after_expiry["expired_options"]
    assert sixth_day["expired_options"] == after_expiry["expired_options"]
    assert eighth_day["expired_options"] == []


def test_an_incomplete_account_closes_nothing(serve: Serve) -> None:
    unreadable = {"occurs1_sym": PUT, "occurs1_trd_div": "", "occurs1_opint_qty": "1"}
    results = accounts(
        serve, step(kst(1), held(CALL)), step(kst(11), unreadable), step(kst(11, 11))
    )

    _, incomplete, complete = results
    assert incomplete["complete"] is False and incomplete["expired_options"] == []
    assert expired(complete) == [(CALL, "2026-12-11T11:00:00+09:00")]


def test_another_users_account_never_closes_or_shows_my_options(serve: Serve) -> None:
    results = accounts(
        serve,
        step(kst(1), held(CALL)),
        step(kst(11), token=OTHER),
        step(kst(11, 11)),
        step(kst(11, 12), token=OTHER),
    )

    _, others_first, mine, others_again = results
    assert others_first["expired_options"] == others_again["expired_options"] == []
    # 다른 사용자의 빈 잔고(10시)가 내 기록을 닫지 않았다. 닫힌 시각은 내 조회(11시)다.
    assert expired(mine) == [(CALL, "2026-12-11T11:00:00+09:00")]
