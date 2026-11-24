"""주문 도구: preview_order → place_order(승인 창) → NH 주문, cancel_order, 차단 스위치, 속도 제한.

MCP 클라이언트로 실제 HTTP 서버를 부른다. NH는 가짜이고 시계는 장중(2026-12-01 화 10:00 KST)이다.
주문·취소·주문내역 행은 녹화가 없어 명세 필드로 만든 가짜다(Task 9에서 녹화로 바꾼다).
"""

import asyncio
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import httpx2
import pytest
from conftest import Serve
from fakes import ADMIN, FAKE_NH_TOKEN, T0, FakeNH, World
from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult
from mcp_types import Tool

from nh_mcp.domain import KST, OrderRequest, RiskLimits, UserId
from nh_mcp.guard.risk import DEMO_ONLY
from nh_mcp.markets.domestic import (
    CANCEL,
    DEPOSIT,
    OPEN_INTEREST,
    ORDER,
    ORDER_ERROR,
    ORDER_HISTORY,
    ORDERABLE_QTY,
    PRICE,
)
from nh_mcp.markets.domestic_products import PRODUCT_DETAIL
from nh_mcp.nh.client import Credentials
from nh_mcp.services.orders import OrdersDisabled
from nh_mcp.store import limits, orders, users
from nh_mcp.store.orders import Outcome, StoredOrder
from nh_mcp.tools.context import ORDERS_OFF_RULE, ToolContext

RECORDED = Path(__file__).resolve().parents[1] / "fixtures" / "nh" / "domestic"
SYMBOL = "A016C000"  # 코스피200 F 202612, 틱 0.05
NAME = "코스피200 F 202612"
APP_KEY = "fake-app-key-0000"
APP_SECRET = "fake-app-secret-0000"
LIMITS = RiskLimits(Decimal("500000"), 3, 2)
BUY: dict[str, Any] = {
    "symbol": SYMBOL,
    "side": "buy",
    "type": "limit",
    "quantity": 1,
    "price": "784.55",
}
ACK = {"ord_no": "0000000101", "msg_cd": "00000", "msg_cntn": "정상 처리"}
ORDER_TOOLS = {"preview_order", "place_order", "cancel_order"}


def recorded(name: str) -> dict[str, Any]:
    body: dict[str, Any] = json.loads((RECORDED / f"{name}.json").read_text("utf-8"))["body"]
    return body


def occurs(*rows: dict[str, str]) -> dict[str, object]:
    return {"nrec": f"{len(rows):04d}", "nxt_key": "", "Occurs1": list(rows)}


def open_order(order_no: str = "0000000301") -> dict[str, str]:
    return {
        "occurs1_ord_no": order_no,
        "occurs1_org_ord_no": "",
        "occurs1_sym": SYMBOL,
        "occurs1_sym_nm": NAME,
        "occurs1_trd_div": "2",
        "occurs1_ord_sts": "1",
        "occurs1_ord_sts_nm": "접수",
        "occurs1_ord_qty": "3",
        "occurs1_ord_pric": "784.55",
        "occurs1_exec_qty": "1",
        "occurs1_rem_ord_qty": "2",
        "occurs1_ord_div_cd": "1",
        "occurs1_exch_rcv_tm": "100001",
    }


@dataclass
class Window:
    """승인 창에서 사용자가 한 일을 흉내 내고, 창에 뜬 문구를 기록한다."""

    action: Literal["accept", "decline", "cancel"]
    value: bool = True
    messages: list[str] = field(default_factory=list[str])

    async def __call__(self, message: str, *_: object) -> ElicitResult:
        self.messages.append(message)
        content = {"value": self.value} if self.action == "accept" else None
        return ElicitResult(action=self.action, content=content)


class Shop:
    """주문 도구가 켜진 서버, 키를 등록하고 한도를 정한 사용자(admin), 가짜 NH."""

    def __init__(self, with_limits: bool = True, orders_enabled: bool = True) -> None:
        self.nh = FakeNH()
        self.nh.reply(PRODUCT_DETAIL, recorded("product-detail"))
        self.nh.reply(DEPOSIT, recorded("deposit"))
        self.nh.reply(OPEN_INTEREST, recorded("open-interest"))
        self.nh.reply(ORDER_HISTORY, recorded("order-history"))
        self.nh.reply(ORDERABLE_QTY, recorded("orderable-quantity"))
        self.nh.reply(PRICE, recorded("price"))
        self.nh.reply(ORDER, ACK)
        self.world = World(nh=httpx2.MockTransport(self.nh), orders_enabled=orders_enabled)
        self.conn: sqlite3.Connection = self.world.conn
        self.user: UserId = users.get_or_create(self.conn, "g-admin", ADMIN, T0)
        credentials = Credentials(APP_KEY, APP_SECRET)
        asyncio.run(self.world.store.register(self.user, "domestic", credentials))
        if with_limits:
            limits.put(self.conn, self.user, "domestic", LIMITS, T0)

    def at(self, when: datetime) -> None:
        self.world.clock.now = when

    def calls(self, path: str) -> int:
        return self.nh.paths().count(path)

    def tokens(self) -> int:
        row: tuple[int] = self.conn.execute("SELECT COUNT(*) FROM confirm_tokens").fetchone()
        return row[0]


type Step = tuple[str, dict[str, Any]]


async def _session(
    serve: Serve, shop: Shop, steps: list[Step], window: Window | None
) -> list[dict[str, Any]]:
    """한 연결에서 도구를 차례로 부른다. 인자의 "$token"은 바로 앞 미리보기의 토큰으로 바꾼다."""
    results: list[dict[str, Any]] = []
    token = ""
    async with (
        serve(shop.world.mcp) as url,
        Client(url, auth="admin-token", elicitation_handler=window) as client,
    ):
        for tool, args in steps:
            filled = {k: token if v == "$token" else v for k, v in args.items()}
            content = (await client.call_tool(tool, filled)).structured_content
            assert content is not None
            results.append(content)
            if tool == "preview_order" and content["ok"]:
                token = content["data"]["confirm_token"]
    return results


def run(
    serve: Serve, shop: Shop, steps: list[Step], window: Window | None = None
) -> list[dict[str, Any]]:
    return asyncio.run(_session(serve, shop, steps, window))


def preview_and_place(
    serve: Serve, shop: Shop, window: Window | None = None, order: dict[str, Any] = BUY
) -> tuple[dict[str, Any], dict[str, Any]]:
    preview, placed = run(
        serve,
        shop,
        [("preview_order", {"order": order}), ("place_order", {"confirm_token": "$token"})],
        window,
    )
    return preview, placed


async def _listed(serve: Serve, world: World) -> list[Tool]:
    async with serve(world.mcp) as url, Client(url, auth="admin-token") as client:
        return await client.list_tools()


def listed(serve: Serve, world: World) -> dict[str, Tool]:
    return {t.name: t for t in asyncio.run(_listed(serve, world))}


# ---- 등록 -----------------------------------------------------------------


def test_place_order_takes_the_confirm_token_and_nothing_else(serve: Serve) -> None:
    tool = listed(serve, World(orders_enabled=True))["place_order"]

    assert set(tool.input_schema["properties"]) == {"confirm_token"}
    assert tool.input_schema["required"] == ["confirm_token"]


def test_order_tools_carry_the_agreed_hints(serve: Serve) -> None:
    tools = listed(serve, World(orders_enabled=True))

    hints: dict[str, tuple[bool | None, bool | None]] = {}
    for name in ORDER_TOOLS:
        annotations = tools[name].annotations
        assert annotations is not None, name
        hints[name] = (annotations.read_only_hint, annotations.destructive_hint)
    assert hints == {
        "preview_order": (False, False),
        "place_order": (False, True),
        "cancel_order": (False, True),
    }


def test_preview_takes_the_order_fields_and_the_market(serve: Serve) -> None:
    schema = listed(serve, World(orders_enabled=True))["preview_order"].input_schema

    assert set(schema["properties"]) == {"order", "market"}
    assert schema["required"] == ["order"]
    draft = schema["properties"]["order"]
    assert set(draft["properties"]) == {"symbol", "side", "type", "quantity", "price"}
    assert set(draft["required"]) == {"symbol", "side", "type", "quantity"}
    # 값은 도구가 엄격하게 보지만 AI에게는 정수라고 알린다.
    assert draft["properties"]["quantity"] == {"type": "integer"}


def test_with_the_switch_off_order_tools_are_missing_but_limit_tools_stay(serve: Serve) -> None:
    names = set(listed(serve, World(orders_enabled=False)))

    assert not names & ORDER_TOOLS
    assert {"get_risk_limits", "set_risk_limits"} <= names


def test_the_switch_is_off_by_default(serve: Serve) -> None:
    assert not set(listed(serve, World())) & ORDER_TOOLS


# ---- 미리보기 → 주문 --------------------------------------------------------


def test_preview_returns_the_order_and_a_two_minute_token_without_ordering(serve: Serve) -> None:
    shop = Shop()

    (preview,) = run(serve, shop, [("preview_order", {"order": BUY})])

    assert preview["ok"] is True
    data = preview["data"]
    assert set(data) == {"order", "name", "warnings", "confirm_token", "expires_at"}
    assert data["order"] == {
        "market": "domestic",
        "symbol": SYMBOL,
        "side": "buy",
        "type": "limit",
        "quantity": 1,
        "price": "784.55",
    }
    assert data["name"] == NAME
    assert data["warnings"] == []
    assert data["expires_at"] == "2026-12-01T10:02:00+09:00"
    assert preview["meta"]["env"] == "demo"
    assert shop.calls(ORDER) == 0


def test_accepted_in_the_window_sends_one_order(serve: Serve) -> None:
    shop = Shop()
    window = Window("accept")

    _, placed = preview_and_place(serve, shop, window)

    assert placed["ok"] is True, placed
    assert placed["data"] == {
        "status": "ACCEPTED",
        "order_no": "0000000101",
        "order": {
            "market": "domestic",
            "symbol": SYMBOL,
            "side": "buy",
            "type": "limit",
            "quantity": 1,
            "price": "784.55",
        },
    }
    assert shop.calls(ORDER) == 1


def test_the_window_shows_name_side_quantity_and_price_from_the_server(serve: Serve) -> None:
    shop = Shop()
    window = Window("accept")

    preview_and_place(serve, shop, window)

    (message,) = window.messages
    for part in (NAME, SYMBOL, "매수", "지정가", "1계약", "784.55"):
        assert part in message, part


@pytest.mark.parametrize("window", [Window("decline"), Window("cancel"), Window("accept", False)])
def test_declined_in_the_window_sends_nothing(serve: Serve, window: Window) -> None:
    shop = Shop()

    _, placed = preview_and_place(serve, shop, window)

    assert placed["ok"] is False
    assert placed["error"]["code"] == "CONFIRM_EXPIRED"
    assert shop.calls(ORDER) == 0


def test_a_declined_token_cannot_be_tried_again(serve: Serve) -> None:
    shop = Shop()
    window = Window("decline")
    steps: list[Step] = [
        ("preview_order", {"order": BUY}),
        ("place_order", {"confirm_token": "$token"}),
        ("place_order", {"confirm_token": "$token"}),
    ]

    *_, again = run(serve, shop, steps, window)

    assert again["error"]["code"] == "CONFIRM_EXPIRED"
    assert len(window.messages) == 1
    assert shop.calls(ORDER) == 0


def test_a_client_without_a_window_still_needs_the_token_and_sends_once(serve: Serve) -> None:
    shop = Shop()

    _, placed = preview_and_place(serve, shop)

    assert placed["ok"] is True
    assert shop.calls(ORDER) == 1


def test_a_token_is_good_for_one_order_only(serve: Serve) -> None:
    shop = Shop()
    steps: list[Step] = [
        ("preview_order", {"order": BUY}),
        ("place_order", {"confirm_token": "$token"}),
        ("place_order", {"confirm_token": "$token"}),
    ]

    _, first, second = run(serve, shop, steps)

    assert first["ok"] is True
    assert second["error"]["code"] == "CONFIRM_EXPIRED"
    assert shop.calls(ORDER) == 1


@pytest.mark.parametrize("token", ["made-up", "", "x" * 5000])
def test_a_made_up_token_is_confirm_expired_and_calls_no_nh(serve: Serve, token: str) -> None:
    shop = Shop()

    (placed,) = run(serve, shop, [("place_order", {"confirm_token": token})], Window("accept"))

    assert placed["error"]["code"] == "CONFIRM_EXPIRED"
    assert shop.nh.requests == []


def test_a_strategy_token_is_not_an_order_token(serve: Serve) -> None:
    """주문 내용이 그대로 든 토큰이라도 용도가 전략(strategy)이면 주문하지 않는다."""
    shop = Shop()
    (preview,) = run(serve, shop, [("preview_order", {"order": BUY})])
    token = preview["data"]["confirm_token"]
    with shop.conn:
        shop.conn.execute("UPDATE confirm_tokens SET purpose = 'strategy'")

    (placed,) = run(serve, shop, [("place_order", {"confirm_token": token})], Window("accept"))

    assert placed["error"]["code"] == "CONFIRM_EXPIRED"
    assert shop.calls(ORDER) == 0


def test_no_secret_appears_in_any_response(serve: Serve) -> None:
    shop = Shop()
    steps: list[Step] = [
        ("preview_order", {"order": BUY}),
        ("place_order", {"confirm_token": "$token"}),
        ("cancel_order", {"order_no": "0000000999"}),
        ("preview_order", {"order": {**BUY, "quantity": 3}}),
    ]

    results = run(serve, shop, steps)

    text = json.dumps(results, ensure_ascii=False)
    for secret in (FAKE_NH_TOKEN, APP_KEY, APP_SECRET):
        assert secret not in text


# ---- 오류 대응 ---------------------------------------------------------------


def test_without_limits_the_preview_says_how_to_set_them(serve: Serve) -> None:
    shop = Shop(with_limits=False)

    (preview,) = run(serve, shop, [("preview_order", {"order": BUY})])

    assert preview["error"]["code"] == "LIMITS_NOT_SET"
    assert "set_risk_limits" in preview["error"]["message"]
    assert shop.tokens() == 0


def test_a_rule_block_names_the_rule(serve: Serve) -> None:
    shop = Shop()

    (preview,) = run(serve, shop, [("preview_order", {"order": {**BUY, "quantity": 3}})])

    assert preview["error"]["code"] == "RISK_BLOCKED"
    assert preview["error"]["rule"] == "max_order_qty"
    assert shop.tokens() == 0


def test_an_incompletely_read_account_is_a_risk_block_named_account_unknown(serve: Serve) -> None:
    shop = Shop()
    unreadable = {"occurs1_sym": SYMBOL, "occurs1_trd_div": "", "occurs1_opint_qty": "1"}
    shop.nh.replies[OPEN_INTEREST] = [(200, occurs(unreadable))]

    (preview,) = run(serve, shop, [("preview_order", {"order": BUY})])

    assert preview["error"]["code"] == "RISK_BLOCKED"
    assert preview["error"]["rule"] == "account_unknown"
    assert "get_account" in preview["error"]["message"]
    assert shop.tokens() == 0


def test_outside_trading_hours_is_market_closed(serve: Serve) -> None:
    shop = Shop()
    shop.at(datetime(2026, 12, 1, 20, 0, tzinfo=KST))

    (preview,) = run(serve, shop, [("preview_order", {"order": BUY})])

    assert preview["error"]["code"] == "MARKET_CLOSED"


def test_no_answer_from_nh_is_unknown_state_and_is_not_resent(serve: Serve) -> None:
    shop = Shop()
    shop.nh.down_paths.add(ORDER)

    _, placed = preview_and_place(serve, shop)

    assert placed["error"]["code"] == "UNKNOWN_STATE"
    assert "다시 주문하지 말고 get_orders로 확인하세요" in placed["error"]["message"]
    assert placed["error"]["retryable"] is False
    assert shop.calls(ORDER) == 1


def test_orders_disabled_in_the_service_is_a_demo_only_block() -> None:
    world = World()
    ctx = ToolContext(world.conn, world.clock, "http://localhost:8000/keys")

    response = ctx.failed(OrdersDisabled())

    assert response["ok"] is False
    assert "error" in response
    assert (response["error"]["code"], response["error"].get("rule")) == (
        "RISK_BLOCKED",
        "demo_only",
    )


def test_the_switch_rule_name_matches_the_guard() -> None:
    assert ORDERS_OFF_RULE == DEMO_ONLY


def test_an_unexpected_error_is_not_turned_into_an_answer() -> None:
    world = World()
    ctx = ToolContext(world.conn, world.clock, "http://localhost:8000/keys")

    with pytest.raises(RuntimeError):
        ctx.failed(RuntimeError("버그"))


# ---- 입력 검사 ---------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        {"symbol": ""},
        {"symbol": "   "},
        {"symbol": "A" * 40},
        {"side": "BUY"},
        {"side": "long"},
        {"type": "stop"},
        {"quantity": 0},
        {"quantity": -1},
        {"quantity": 10**6 + 1},
        {"quantity": True},
        {"quantity": False},
        {"quantity": "1"},
        {"quantity": " 2 "},
        {"quantity": 1.0},
        {"price": None},
        {"price": ""},
        {"price": "0"},
        {"price": "0.00"},
        {"price": "-784.55"},
        {"price": "+784.55"},
        {"price": "NaN"},
        {"price": "Infinity"},
        {"price": "-Infinity"},
        {"price": "7.8455e2"},
        {"price": "1E+3"},
        {"price": "784,55"},
        {"price": "784.5.5"},
        {"price": "1_000"},
        {"price": "\uff17\uff18\uff14"},
        {"price": "1" * 40},
        {"type": "market", "price": "784.55"},
        {"type": "market", "price": ""},
    ],
)
def test_bad_input_is_invalid_before_anything_is_called(serve: Serve, bad: dict[str, Any]) -> None:
    shop = Shop()

    (preview,) = run(serve, shop, [("preview_order", {"order": {**BUY, **bad}})])

    assert preview["ok"] is False
    assert preview["error"]["code"] == "INVALID_INPUT"
    assert shop.nh.requests == []
    assert shop.tokens() == 0


def test_a_limit_order_with_no_price_key_is_invalid(serve: Serve) -> None:
    shop = Shop()
    no_price = {k: v for k, v in BUY.items() if k != "price"}

    (preview,) = run(serve, shop, [("preview_order", {"order": no_price})])

    assert preview["error"]["code"] == "INVALID_INPUT"
    assert shop.nh.requests == []


def test_overseas_preview_is_not_supported(serve: Serve) -> None:
    shop = Shop()

    (preview,) = run(serve, shop, [("preview_order", {"order": BUY, "market": "overseas"})])

    assert preview["error"]["code"] == "INVALID_INPUT"
    assert shop.nh.requests == []


def test_a_market_order_without_a_price_and_spaces_around_values_are_fine(serve: Serve) -> None:
    shop = Shop()
    market_buy = {"symbol": f" {SYMBOL} ", "side": "buy", "type": "market", "quantity": 1}
    limit_buy = {**BUY, "price": " 784.55 "}
    steps: list[Step] = [
        ("preview_order", {"order": market_buy}),
        ("preview_order", {"order": limit_buy}),
    ]

    market, limit = run(serve, shop, steps)

    assert market["ok"] is True, market
    assert market["data"]["order"]["price"] is None
    assert market["data"]["order"]["symbol"] == SYMBOL
    assert limit["ok"] is True, limit
    assert limit["data"]["order"]["price"] == "784.55"


# ---- 취소 -------------------------------------------------------------------


def test_cancel_sends_the_cancel_without_a_token(serve: Serve) -> None:
    shop = Shop()
    shop.nh.replies[ORDER_HISTORY] = [(200, occurs(open_order()))]
    shop.nh.reply(CANCEL, {"ord_no": "0000000302", "msg_cd": "", "msg_cntn": ""})

    (cancelled,) = run(serve, shop, [("cancel_order", {"order_no": "0000000301"})])

    assert cancelled["ok"] is True, cancelled
    assert cancelled["data"]["order_no"] == "0000000301"
    assert cancelled["data"]["cancel_order_no"] == "0000000302"
    assert shop.calls(CANCEL) == 1


def test_cancel_of_an_order_that_is_not_open_is_invalid_input(serve: Serve) -> None:
    shop = Shop()
    shop.nh.replies[ORDER_HISTORY] = [(200, occurs(open_order()))]

    (cancelled,) = run(serve, shop, [("cancel_order", {"order_no": "0000000999"})])

    assert cancelled["error"]["code"] == "INVALID_INPUT"
    assert "get_orders" in cancelled["error"]["message"]
    assert shop.calls(CANCEL) == 0


def test_cancel_overseas_is_not_supported(serve: Serve) -> None:
    shop = Shop()

    (cancelled,) = run(serve, shop, [("cancel_order", {"order_no": "1", "market": "overseas"})])

    assert cancelled["error"]["code"] == "INVALID_INPUT"
    assert shop.nh.requests == []


def test_no_answer_to_a_cancel_is_unknown_state(serve: Serve) -> None:
    shop = Shop()
    shop.nh.replies[ORDER_HISTORY] = [(200, occurs(open_order()))]
    shop.nh.down_paths.add(CANCEL)

    (cancelled,) = run(serve, shop, [("cancel_order", {"order_no": "0000000301"})])

    assert cancelled["error"]["code"] == "UNKNOWN_STATE"
    assert shop.calls(CANCEL) == 1


# ---- 속도 제한 ---------------------------------------------------------------


def test_the_twenty_first_preview_in_a_minute_is_refused_before_nh(serve: Serve) -> None:
    shop = Shop()
    steps: list[Step] = [("preview_order", {"order": BUY})] * 21

    results = run(serve, shop, steps)

    assert all(r["ok"] for r in results[:20])
    last = results[20]
    assert last["error"]["code"] == "TEMPORARILY_UNAVAILABLE"
    assert last["error"]["retryable"] is True
    assert shop.tokens() == 20


def test_previews_are_allowed_again_a_minute_later(serve: Serve) -> None:
    shop = Shop()
    run(serve, shop, [("preview_order", {"order": BUY})] * 20)
    before = len(shop.nh.requests)

    (refused,) = run(serve, shop, [("preview_order", {"order": BUY})])
    assert refused["error"]["code"] == "TEMPORARILY_UNAVAILABLE"
    assert len(shop.nh.requests) == before

    shop.at(T0.replace(minute=1))
    (allowed,) = run(serve, shop, [("preview_order", {"order": BUY})])
    assert allowed["ok"] is True


def test_place_and_cancel_share_one_bucket_of_ten(serve: Serve) -> None:
    shop = Shop()
    shop.nh.replies[ORDER_HISTORY] = [(200, occurs())]
    steps: list[Step] = [("cancel_order", {"order_no": "0000000999"})] * 10
    steps += [("preview_order", {"order": BUY}), ("place_order", {"confirm_token": "$token"})]

    results = run(serve, shop, steps)

    assert [r["error"]["code"] for r in results[:10]] == ["INVALID_INPUT"] * 10
    assert results[10]["ok"] is True  # 미리보기는 다른 버킷
    assert results[11]["error"]["code"] == "TEMPORARILY_UNAVAILABLE"
    assert shop.calls(ORDER) == 0


# ---- get_orders가 응답을 못 받은 주문을 대조한다 ----------------------------------


def bought(order_no: str = "0000000201") -> dict[str, str]:
    """BUY(784.55 지정가 1계약)와 같은, 10:00:01에 접수된 신규 주문의 주문내역 행."""
    return {
        **open_order(order_no),
        "occurs1_trd_div": "1",
        "occurs1_ord_qty": "1",
        "occurs1_exec_qty": "0",
        "occurs1_rem_ord_qty": "1",
    }


def stuck(shop: Shop) -> int:
    """10:00에 보냈지만 응답을 못 받은 BUY 주문 행."""
    order = OrderRequest("domestic", SYMBOL, "buy", "limit", 1, Decimal("784.55"))
    order_id = orders.insert_submitted(shop.conn, shop.user, order, T0)
    orders.update(shop.conn, shop.user, order_id, Outcome("UNKNOWN"), T0)
    return order_id


def row(shop: Shop, order_id: int) -> StoredOrder:
    found = orders.get(shop.conn, shop.user, order_id)
    assert found is not None
    return found


def test_get_orders_settles_an_unknown_order_that_matches_one_nh_order(serve: Serve) -> None:
    shop = Shop()
    order_id = stuck(shop)
    shop.nh.replies[ORDER_HISTORY] = [(200, occurs(bought()))]
    shop.nh.reply(ORDER_ERROR, {"nxt_data_yn": "N", "Occurs1": []})

    (listed_orders,) = run(serve, shop, [("get_orders", {})])

    assert listed_orders["ok"] is True, listed_orders
    assert [o["order_no"] for o in listed_orders["data"]["orders"]] == ["0000000201"]
    settled = row(shop, order_id)
    assert (settled.status, settled.nh_order_no) == ("ACCEPTED", "0000000201")

    (preview,) = run(serve, shop, [("preview_order", {"order": BUY})])
    assert preview["ok"] is True, preview


def test_get_orders_still_lists_when_the_reconcile_cannot_reach_nh(serve: Serve) -> None:
    shop = Shop()
    order_id = stuck(shop)
    # 첫 주문내역 요청(대조)은 실패하고, 다음 요청(목록)은 답한다.
    shop.nh.replies[ORDER_HISTORY] = [(503, b""), (200, occurs(bought()))]
    shop.nh.reply(ORDER_ERROR, {"nxt_data_yn": "N", "Occurs1": []})

    (listed_orders,) = run(serve, shop, [("get_orders", {})])

    assert listed_orders["ok"] is True, listed_orders
    assert [o["order_no"] for o in listed_orders["data"]["orders"]] == ["0000000201"]
    assert shop.calls(ORDER_HISTORY) == 2
    assert row(shop, order_id).status == "UNKNOWN"


def test_get_orders_works_with_the_switch_off_and_does_not_reconcile(serve: Serve) -> None:
    shop = Shop(orders_enabled=False)
    order_id = stuck(shop)
    shop.nh.replies[ORDER_HISTORY] = [(200, occurs(bought()))]
    shop.nh.reply(ORDER_ERROR, {"nxt_data_yn": "N", "Occurs1": []})

    (listed_orders,) = run(serve, shop, [("get_orders", {})])

    assert listed_orders["ok"] is True, listed_orders
    assert shop.calls(ORDER_HISTORY) == 1
    assert row(shop, order_id).status == "UNKNOWN"
