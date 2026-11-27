"""모의 서버 응답을 녹화해 tests/fixtures/nh/domestic/에 저장한다.

조회 API만 녹화한다. 주문 등급 API는 녹화하지 않는다. 실패 응답도 남긴다.
실행(평일 장중, final_project 폴더에서):
    uv run --env-file .env python -m nh_mcp.nh.record --sym <종목코드>
코스피200 옵션 전광판만(price-all-option-1.json, -2.json, …):
    uv run --env-file .env python -m nh_mcp.nh.record --board <만기일 뒤 6자리, 예 261210>
"""

import argparse
import asyncio
import contextlib
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx2

from nh_mcp.domain import KST
from nh_mcp.nh.client import Credentials, JsonObject, NHClient, Rejected
from nh_mcp.nh.limits import grade_of
from nh_mcp.nh.recording import Fixture, RecordingTransport

FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "nh" / "domestic"
DOMESTIC = "/trade/v1/domestic/"
# 없는 종목코드. 오류 응답의 실제 모양을 남기려고 일부러 보낸다.
UNKNOWN_SYMBOL = "A0000000"
BOARD = DOMESTIC + "price-all-option"
KOSPI200 = "01"  # prd
FIRST_STRIKE = "99999999"  # nxt_stkr_pric 첫 쪽
# 전광판 쪽을 따라가는 상한. 2026-12물 567행이 3쪽(쪽당 261행)이었다.
BOARD_MAX_PAGES = 10


def cases(sym: str, today: str) -> list[tuple[str, str, JsonObject]]:
    every = {"trd_div": "0", "exec_div": "0", "trd_tm_div_cd": "0", "ord_div_cd": "0"}
    return [
        ("price", DOMESTIC + "price", {"sym": sym}),
        ("price_unknown_symbol", DOMESTIC + "price", {"sym": UNKNOWN_SYMBOL}),
        ("price-bid", DOMESTIC + "price-bid", {"exch_cd": "KSP", "sym": sym}),
        (
            "chart-minute",
            DOMESTIC + "chart-minute",
            {"nght_tp": "1", "sym": sym, "req_qty": "30", "dcnt": "1"},
        ),
        ("chart-daily", DOMESTIC + "chart-daily", {"nght_tp": "1", "sym": sym, "req_qty": "30"}),
        ("product-detail", DOMESTIC + "product-detail", {}),
        ("deposit", DOMESTIC + "deposit", {"biz_dt": today}),
        ("open-interest", DOMESTIC + "open-interest", {"nxt_key": ""}),
        ("order-history", DOMESTIC + "order-history", {**every, "nxt_key": ""}),
        (
            "order-error",
            DOMESTIC + "order-error",
            {"inq_dt": today, "trd_div_cd": "0", "nxt_key": ""},
        ),
    ]


def orderable_case(sym: str, price: str) -> tuple[str, str, JsonObject]:
    payload: JsonObject = {"ord_sts": "1", "sym": sym, "trd_div": "1", "ord_pric": price}
    return ("orderable-quantity", DOMESTIC + "orderable-quantity", payload)


async def record_one(
    client: NHClient, captured: list[Fixture], case: tuple[str, str, JsonObject]
) -> Fixture | None:
    name, path, payload = case
    if grade_of(path) == "order":
        raise ValueError(f"주문 API는 녹화하지 않습니다: {path}")
    before = len(captured)
    # 실패 응답도 녹화 대상이다.
    with contextlib.suppress(Rejected):
        await client.post(path, payload)
    if len(captured) == before:
        print(f"{name}: 녹화된 응답 없음")
        return None
    fixture = captured[-1]
    (FIXTURES / f"{name}.json").write_text(fixture.to_json(), encoding="utf-8")
    code = fixture.body.get("errorCode") if isinstance(fixture.body, dict) else None
    print(f"{name}: HTTP {fixture.status}, errorCode={code or '-'}")
    return fixture


def board_case(exp_mon: str, page: int, strike: str) -> tuple[str, str, JsonObject]:
    payload: JsonObject = {"prd": KOSPI200, "exp_mon": exp_mon, "nxt_stkr_pric": strike}
    return (f"price-all-option-{page}", BOARD, payload)


async def record_board(client: NHClient, captured: list[Fixture], exp_mon: str) -> None:
    """응답의 다음 행사가(nxt_stkr_pric)를 따라 끝 쪽까지 녹화한다."""
    strike = FIRST_STRIKE
    seen = {strike}
    for page in range(1, BOARD_MAX_PAGES + 1):
        fixture = await record_one(client, captured, board_case(exp_mon, page, strike))
        body = fixture.body if fixture else None
        if not isinstance(body, dict):
            return
        rows = body.get("Occurs1")
        count = len(rows) if isinstance(rows, list) else 0
        strike = str(body.get("nxt_stkr_pric", "")).strip()
        print(f"  {count}행, nxt_stkr_pric={strike!r}")
        if not count or not strike or strike in seen:
            return
        seen.add(strike)
    print(f"{BOARD_MAX_PAGES}쪽에서 멈춤")


def _client() -> tuple[NHClient, list[Fixture]]:
    key = os.environ["NH_DEV_DOMESTIC_APP_KEY"]
    secret = os.environ["NH_DEV_DOMESTIC_APP_SECRET"]
    captured: list[Fixture] = []
    transport = RecordingTransport(httpx2.AsyncHTTPTransport(), captured.append, [key, secret])
    client = NHClient(Credentials(key, secret), lambda: datetime.now(UTC), transport=transport)
    FIXTURES.mkdir(parents=True, exist_ok=True)
    return client, captured


async def record(sym: str) -> None:
    client, captured = _client()
    today = datetime.now(KST).strftime("%Y%m%d")
    try:
        price = None
        for case in cases(sym, today):
            fixture = await record_one(client, captured, case)
            if case[0] == "price" and fixture and isinstance(fixture.body, dict):
                price = fixture.body.get("last_pric")
        if isinstance(price, str) and price:
            await record_one(client, captured, orderable_case(sym, price))
        else:
            print("orderable-quantity: 현재가를 받지 못해 건너뜀")
    finally:
        await client.aclose()


async def record_board_only(exp_mon: str) -> None:
    client, captured = _client()
    try:
        await record_board(client, captured, exp_mon)
    finally:
        await client.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description="NH 모의 서버 응답 녹화(조회 API만)")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--sym", help="녹화에 쓸 국내 선물 종목코드")
    target.add_argument("--board", help="코스피200 옵션 전광판만. 만기일 뒤 6자리(예 261210)")
    args = parser.parse_args()
    if args.board:
        asyncio.run(record_board_only(args.board))
    else:
        asyncio.run(record(args.sym))


if __name__ == "__main__":
    main()
