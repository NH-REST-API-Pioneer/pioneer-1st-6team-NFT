"""옵션 종목의 preview_order · place_order: 응답의 option 칸, 승인 창 문구,
스위치 거절에 붙는 등록 화면 주소.

MCP 클라이언트로 실제 HTTP 서버를 부른다. NH는 가짜이고 시계는 장중(2026-12-01 화 10:00 KST)이다.
사용자는 "옵션"을 켰고 "옵션 매도"는 켜지 않았다. 옵션 한도는 정해 두었다.
"""

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2
from conftest import Serve
from fakes import ADMIN, SETTINGS, T0, FakeNH, World, options_on
from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult

from nh_mcp.auth.web import KEYS_PATH
from nh_mcp.domain import OptionLimits, RiskLimits
from nh_mcp.markets.domestic import (
    DEPOSIT,
    OPEN_INTEREST,
    ORDER,
    ORDER_HISTORY,
    ORDERABLE_QTY,
    PRICE,
)
from nh_mcp.markets.domestic_products import PRODUCT_DETAIL
from nh_mcp.nh.client import Credentials
from nh_mcp.store import limits, users

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
RECORDED = FIXTURES / "nh" / "domestic"
OPTION_MASTER = FIXTURES / "master" / "kospi200_options.mst"
KEYS_URL = SETTINGS.public_base_url + KEYS_PATH
CALL = "B016C725"  # 코스피200 C 202612 725.0, 승수 250000
LIMITS = RiskLimits(Decimal("500000"), 3, 2, OptionLimits(Decimal("1000000"), 4, 2))
BUY: dict[str, Any] = {
    "symbol": CALL,
    "side": "buy",
    "type": "limit",
    "quantity": 1,
    "price": "2.00",
}
SELL: dict[str, Any] = BUY | {"side": "sell"}
ACK = {"ord_no": "0000000101", "msg_cd": "00000", "msg_cntn": "정상 처리"}


def recorded(name: str) -> dict[str, Any]:
    body: dict[str, Any] = json.loads((RECORDED / f"{name}.json").read_text("utf-8"))["body"]
    return body


def shop() -> tuple[World, FakeNH]:
    nh = FakeNH()
    for path, name in [
        (PRODUCT_DETAIL, "product-detail"),
        (DEPOSIT, "deposit"),
        (OPEN_INTEREST, "open-interest"),
        (ORDER_HISTORY, "order-history"),
        (ORDERABLE_QTY, "orderable-quantity"),
        (PRICE, "price"),
    ]:
        nh.reply(path, recorded(name))
    nh.reply(ORDER, ACK)
    world = World(nh=httpx2.MockTransport(nh), orders_enabled=True)
    world.master.extra["fut_opt"] = OPTION_MASTER.read_text(encoding="utf-8")
    user = users.get_or_create(world.conn, "g-admin", ADMIN, T0)
    credentials = Credentials("fake-app-key-0000", "fake-app-secret-0000")
    asyncio.run(world.store.register(user, "domestic", credentials))
    limits.put(world.conn, user, "domestic", LIMITS, T0)
    options_on(world.conn, user)
    return world, nh


def run(
    serve: Serve, world: World, order: dict[str, Any], messages: list[str]
) -> list[dict[str, Any]]:
    """미리보기 뒤 통과하면 그 토큰으로 주문한다. 승인 창은 늘 승인하고 문구를 messages에 남긴다."""

    async def window(message: str, *_: object) -> ElicitResult:
        messages.append(message)
        return ElicitResult(action="accept", content={"value": True})

    async def call() -> list[dict[str, Any]]:
        async with (
            serve(world.mcp) as url,
            Client(url, auth="admin-token", elicitation_handler=window) as client,
        ):
            preview = (await client.call_tool("preview_order", {"order": order})).structured_content
            assert preview is not None
            if not preview["ok"]:
                return [preview]
            token = {"confirm_token": preview["data"]["confirm_token"]}
            placed = (await client.call_tool("place_order", token)).structured_content
            assert placed is not None
            return [preview, placed]

    return asyncio.run(call())


def test_option_preview_shows_new_buy_and_premium_and_the_window_repeats_them(
    serve: Serve,
) -> None:
    world, nh = shop()
    messages: list[str] = []

    preview, placed = run(serve, world, BUY, messages)

    assert preview["data"]["option"] == {
        "action": "new_buy",
        "label": "새 매수",
        "premium": "500000.00",
    }
    (message,) = messages
    assert "옵션 새 매수입니다. 프리미엄 500000.00원" in message
    assert placed["ok"] is True, placed
    assert nh.paths().count(ORDER) == 1


def test_new_sell_refusal_points_to_the_sign_up_page_and_sends_nothing(serve: Serve) -> None:
    world, nh = shop()

    (refused,) = run(serve, world, SELL, [])

    error = refused["error"]
    assert (error["code"], error["rule"]) == ("RISK_BLOCKED", "option_sell_off")
    assert error["message"].endswith(f"등록 화면: {KEYS_URL}")
    assert nh.paths().count(ORDER) == 0
