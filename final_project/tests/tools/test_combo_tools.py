"""옵션 묶음 도구: preview_combo → place_combo(승인 창) → 다리 주문, 묶음 스위치, get_orders.

MCP 클라이언트로 실제 HTTP 서버를 부른다. NH는 묶음 서비스 시험의 가짜 거래소(`Exchange`)이고
시계는 장중(2026-12-01 화 10:00 KST)이다. 사용자는 "옵션"을 켰고 "옵션 매도"는 켜지 않았다.

기본 묶음은 콜 수직 매수형: 725 콜 2.00 매수 · 730 콜 1.00 매도, 2계약, 허용 폭 0.05.
순가격 1.00, 끝 1.05, 최대 손실 1.05 * 250000 * 2 = 525000원.
"""

import asyncio
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import httpx2
import pytest
from conftest import Serve
from fakes import ADMIN, SETTINGS, T0, Clock, FakeNH, World, options_on
from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult
from mcp_types import Tool
from option_exchange import Exchange, Placed, Snap, filled, recorded

from nh_mcp.auth.web import KEYS_PATH
from nh_mcp.domain import OptionLimits, RiskLimits, UserId
from nh_mcp.markets.domestic import DEPOSIT, ORDER_ERROR, ORDERABLE_QTY
from nh_mcp.markets.domestic_products import PRODUCT_DETAIL
from nh_mcp.nh.client import Credentials
from nh_mcp.store import features, limits, users

OPTION_MASTER = Path(__file__).resolve().parents[1] / "fixtures" / "master" / "kospi200_options.mst"
KEYS_URL = SETTINGS.public_base_url + KEYS_PATH
CALL = "B016C725"  # 코스피200 C 202612 725.0, 승수 250000
CALL_730 = "B016C730"
OPTION_LIMITS = OptionLimits(Decimal("2000000"), 4, 2, Decimal("1000000"))
LIMITS = RiskLimits(Decimal("500000"), 3, 2, OPTION_LIMITS)
COMBO_TOOLS = {"preview_combo", "place_combo"}
VERTICAL: dict[str, Any] = {
    "legs": [
        {"symbol": CALL, "side": "buy", "price": "2.00"},
        {"symbol": CALL_730, "side": "sell", "price": "1.00"},
    ],
    "quantity": 2,
    "slippage": "0.05",
}
BOTH_FILLED = [filled(2, "2.00"), filled(2, "1.00")]


@dataclass
class Window:
    """승인 창에서 사용자가 한 일을 흉내 내고, 창에 뜬 문구를 기록한다."""

    action: Literal["accept", "decline"]
    messages: list[str] = field(default_factory=list[str])

    async def __call__(self, message: str, *_: object) -> ElicitResult:
        self.messages.append(message)
        content = {"value": True} if self.action == "accept" else None
        return ElicitResult(action=self.action, content=content)


class Desk:
    """주문 스위치를 켠 서버, 키·한도를 정하고 "옵션"을 켠 사용자(admin), 가짜 거래소."""

    def __init__(self, options: bool = True, orders_enabled: bool = True) -> None:
        nh = FakeNH()
        nh.reply(PRODUCT_DETAIL, recorded("product-detail"))
        nh.reply(DEPOSIT, recorded("deposit"))
        nh.reply(ORDERABLE_QTY, recorded("orderable-quantity"))
        nh.reply(ORDER_ERROR, {"nxt_data_yn": "N", "Occurs1": []})
        self.exchange = Exchange(nh, Clock())
        self.world = World(nh=httpx2.MockTransport(self.exchange), orders_enabled=orders_enabled)
        self.exchange.clock = self.world.clock
        self.world.master.extra["fut_opt"] = OPTION_MASTER.read_text(encoding="utf-8")
        self.conn = self.world.conn
        self.user: UserId = users.get_or_create(self.conn, "g-admin", ADMIN, T0)
        credentials = Credentials("fake-app-key-0000", "fake-app-secret-0000")
        asyncio.run(self.world.store.register(self.user, "domestic", credentials))
        limits.put(self.conn, self.user, "domestic", LIMITS, T0)
        if options:
            options_on(self.conn, self.user)
        self.exchange.book[CALL_730] = ("0.95", "1.00")  # 매수 1호가 0.95 = 파는 다리 하한

    def set_limits(self, new: RiskLimits) -> None:
        limits.put(self.conn, self.user, "domestic", new, T0)


type Step = tuple[str, dict[str, Any]]


async def _session(
    desk: Desk, serve: Serve, steps: list[Step], window: Window | None
) -> list[dict[str, Any]]:
    """한 연결에서 도구를 차례로 부른다. 인자 값 "$token"은 마지막으로 받은 confirm_token,
    "$combo"는 마지막으로 받은 combo_id로 바꾼다."""
    results: list[dict[str, Any]] = []
    swap: dict[str, Any] = {"$token": "", "$combo": ""}
    async with (
        serve(desk.world.mcp) as url,
        Client(url, auth="admin-token", elicitation_handler=window) as client,
    ):
        for tool, args in steps:
            sent = {k: swap.get(v, v) if isinstance(v, str) else v for k, v in args.items()}
            content = (await client.call_tool(tool, sent)).structured_content
            assert content is not None
            results.append(content)
            data: dict[str, Any] = content.get("data") or {}
            for slot, key in (("$token", "confirm_token"), ("$combo", "combo_id")):
                swap[slot] = data.get(key, swap[slot])
    return results


def run(
    desk: Desk, serve: Serve, steps: list[Step], window: Window | None = None
) -> list[dict[str, Any]]:
    return asyncio.run(_session(desk, serve, steps, window))


def preview_and_place(
    desk: Desk, serve: Serve, window: Window, combo: dict[str, Any] = VERTICAL
) -> tuple[dict[str, Any], dict[str, Any]]:
    steps: list[Step] = [
        ("preview_combo", {"combo": combo}),
        ("place_combo", {"confirm_token": "$token"}),
    ]
    preview, placed = run(desk, serve, steps, window)
    return preview, placed


async def _listed(desk: Desk, serve: Serve) -> dict[str, Tool]:
    async with serve(desk.world.mcp) as url, Client(url, auth="admin-token") as client:
        return {t.name: t for t in await client.list_tools()}


def listed(desk: Desk, serve: Serve) -> dict[str, Tool]:
    return asyncio.run(_listed(desk, serve))


async def _raw_preview(desk: Desk, serve: Serve, arguments: dict[str, Any]) -> tuple[bool, str]:
    async with serve(desk.world.mcp) as url, Client(url, auth="admin-token") as client:
        result = await client.call_tool("preview_combo", arguments, raise_on_error=False)
        return result.is_error, str(result.content)


def tokens(desk: Desk) -> int:
    row: tuple[int] = desk.conn.execute("SELECT COUNT(*) FROM confirm_tokens").fetchone()
    return row[0]


def combo_rows(desk: Desk) -> int:
    row: tuple[int] = desk.conn.execute("SELECT COUNT(*) FROM combos").fetchone()
    return row[0]


# ---- 등록과 입력 스키마 --------------------------------------------------------------------


def test_place_combo_takes_the_confirm_token_and_nothing_else(serve: Serve) -> None:
    # 불변식 3: 실행 도구는 확인 토큰만 받는다. 다리·가격·수량을 인자로 받지 않는다.
    tool = listed(Desk(), serve)["place_combo"]

    assert set(tool.input_schema["properties"]) == {"confirm_token"}
    assert tool.input_schema["required"] == ["confirm_token"]


def test_preview_combo_takes_a_combo_or_a_resume_id_and_the_market(serve: Serve) -> None:
    schema = listed(Desk(), serve)["preview_combo"].input_schema

    assert set(schema["properties"]) == {"combo", "resume", "market"}
    assert schema.get("required", []) == []
    combo = schema["properties"]["combo"]["anyOf"][0]
    assert set(combo["properties"]) == {"legs", "quantity", "slippage"}
    assert set(combo["required"]) == {"legs", "quantity", "slippage"}
    assert combo["additionalProperties"] is False
    leg = combo["properties"]["legs"]["items"]
    assert set(leg["properties"]) == {"symbol", "side", "price"}
    assert set(leg["required"]) == {"symbol", "side", "price"}
    assert leg["additionalProperties"] is False
    # 값은 도구가 엄격하게 보지만 AI에게는 정수라고 알린다.
    assert combo["properties"]["quantity"] == {"type": "integer"}


def test_combo_tools_carry_the_agreed_hints(serve: Serve) -> None:
    tools = listed(Desk(), serve)

    hints: dict[str, tuple[bool | None, bool | None]] = {}
    for name in COMBO_TOOLS:
        annotations = tools[name].annotations
        assert annotations is not None, name
        hints[name] = (annotations.read_only_hint, annotations.destructive_hint)
    assert hints == {"preview_combo": (False, False), "place_combo": (False, True)}


def test_with_the_order_switch_off_the_combo_tools_are_not_registered(serve: Serve) -> None:
    names = set(listed(Desk(orders_enabled=False), serve))

    assert not names & COMBO_TOOLS
    assert "get_orders" in names


# ---- "옵션" 묶음 ---------------------------------------------------------------------------


def test_a_user_without_options_does_not_see_the_combo_tools(serve: Serve) -> None:
    names = set(listed(Desk(options=False), serve))

    assert not names & COMBO_TOOLS
    assert "preview_order" in names


def test_a_direct_preview_without_options_is_refused_with_the_sign_up_page(serve: Serve) -> None:
    desk = Desk(options=False)

    (refused,) = run(desk, serve, [("preview_combo", {"combo": VERTICAL})])

    error = refused["error"]
    assert (error["code"], error["rule"]) == ("RISK_BLOCKED", "options_off")
    assert error["message"].endswith(f"등록 화면: {KEYS_URL}")
    assert combo_rows(desk) == 0
    assert tokens(desk) == 0


def test_turning_options_off_after_the_preview_blocks_the_place(serve: Serve) -> None:
    desk = Desk()
    desk.exchange.script = list(BOTH_FILLED)
    window = Window("accept")

    async def steps() -> tuple[dict[str, Any], dict[str, Any]]:
        async with (
            serve(desk.world.mcp) as url,
            Client(url, auth="admin-token", elicitation_handler=window) as client,
        ):
            preview = (
                await client.call_tool("preview_combo", {"combo": VERTICAL})
            ).structured_content
            assert preview is not None
            features.turn_off(desk.conn, desk.user, "options")
            token = {"confirm_token": preview["data"]["confirm_token"]}
            placed = (await client.call_tool("place_combo", token)).structured_content
            assert placed is not None
            return preview, placed

    _, placed = asyncio.run(steps())

    assert (placed["error"]["code"], placed["error"]["rule"]) == ("RISK_BLOCKED", "options_off")
    assert desk.exchange.orders == []


# ---- 입력 검사 ----------------------------------------------------------------------------


def legs(*specs: tuple[str, str, str]) -> list[dict[str, str]]:
    return [{"symbol": s, "side": side, "price": price} for s, side, price in specs]


BUY_725 = (CALL, "buy", "2.00")
SELL_730 = (CALL_730, "sell", "1.00")


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"combo": VERTICAL, "resume": "0123456789abcdef"},
        {"resume": "  "},
        {"combo": VERTICAL, "market": "overseas"},
        {"combo": VERTICAL | {"legs": legs(BUY_725)}},
        {"combo": VERTICAL | {"legs": legs(BUY_725, SELL_730, (CALL, "sell", "1.50"))}},
        {"combo": VERTICAL | {"legs": legs(BUY_725, (CALL_730, "sell", "1e0"))}},
        {"combo": VERTICAL | {"legs": legs(BUY_725, (CALL_730, "sell", "-1.00"))}},
        {"combo": VERTICAL | {"legs": legs(BUY_725, (CALL_730, "sell", "0"))}},
        {"combo": VERTICAL | {"legs": legs(BUY_725, (CALL_730, "short", "1.00"))}},
        {"combo": VERTICAL | {"legs": legs(BUY_725, ("", "sell", "1.00"))}},
        {"combo": VERTICAL | {"slippage": "0.005"}},
        {"combo": VERTICAL | {"slippage": "-0.05"}},
        {"combo": VERTICAL | {"slippage": "NaN"}},
        {"combo": VERTICAL | {"quantity": 0}},
        {"combo": VERTICAL | {"quantity": "2"}},
        {"combo": VERTICAL | {"quantity": True}},
    ],
)
def test_bad_input_is_invalid_and_previews_nothing(serve: Serve, arguments: dict[str, Any]) -> None:
    desk = Desk()

    (result,) = run(desk, serve, [("preview_combo", arguments)])

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT", result
    assert combo_rows(desk) == 0
    assert tokens(desk) == 0


@pytest.mark.parametrize(
    "combo",
    [
        VERTICAL | {"ratio": 2},
        VERTICAL | {"legs": [{"symbol": CALL, "side": "buy", "price": "2.00", "type": "market"}]},
    ],
)
def test_unknown_fields_are_refused_not_dropped(serve: Serve, combo: dict[str, Any]) -> None:
    # set_risk_limits의 options, backtest_strategy처럼 스키마 검사에서 거절한다.
    desk = Desk()

    is_error, content = asyncio.run(_raw_preview(desk, serve, {"combo": combo}))

    assert is_error is True, content
    assert combo_rows(desk) == 0


def test_an_unknown_resume_id_is_invalid(serve: Serve) -> None:
    (result,) = run(Desk(), serve, [("preview_combo", {"resume": "0123456789abcdef"})])

    assert result["error"]["code"] == "INVALID_INPUT"
    assert "combo_id" in result["error"]["message"]


def test_without_a_combo_limit_the_preview_is_limits_not_set(serve: Serve) -> None:
    desk = Desk()
    desk.set_limits(replace(LIMITS, options=replace(OPTION_LIMITS, max_combo_loss=None)))

    (result,) = run(desk, serve, [("preview_combo", {"combo": VERTICAL})])

    assert result["error"]["code"] == "LIMITS_NOT_SET"
    assert "max_combo_loss" in result["error"]["message"]
    assert "rule" not in result["error"]


# ---- 미리보기 → 실행 ----------------------------------------------------------------------


def test_preview_returns_the_structure_legs_in_send_order_and_a_two_minute_token(
    serve: Serve,
) -> None:
    desk = Desk()
    # 파는 다리를 먼저 넣어도 사는 다리부터 보낸다.
    reversed_legs = VERTICAL | {"legs": list(reversed(VERTICAL["legs"]))}

    (preview,) = run(desk, serve, [("preview_combo", {"combo": reversed_legs})])

    assert preview["ok"] is True, preview
    data = preview["data"]
    assert set(data) == {
        "combo_id",
        "structure",
        "quantity",
        "legs",
        "net_debit",
        "worst_net_debit",
        "max_loss",
        "max_loss_unbounded",
        "warnings",
        "confirm_token",
        "expires_at",
    }
    assert data["structure"] == "debit_vertical"
    assert data["quantity"] == 2
    assert [(leg["symbol"], leg["side"], leg["price"], leg["filled"]) for leg in data["legs"]] == [
        (CALL, "buy", "2.00", 0),
        (CALL_730, "sell", "1.00", 0),
    ]
    assert all(leg["name"] for leg in data["legs"])
    assert (data["net_debit"], data["worst_net_debit"]) == ("1.00", "1.05")
    assert Decimal(data["max_loss"]) == Decimal(525000)
    assert data["max_loss_unbounded"] is False
    assert data["warnings"] == []
    assert data["expires_at"] == "2026-12-01T10:02:00+09:00"
    assert preview["meta"]["env"] == "demo"
    assert desk.exchange.orders == []


def test_accepted_in_the_window_sends_the_buy_leg_then_the_sell_leg(serve: Serve) -> None:
    desk = Desk()
    desk.exchange.script = list(BOTH_FILLED)
    window = Window("accept")

    preview, placed = preview_and_place(desk, serve, window)

    assert placed["ok"] is True, placed
    combo_id = preview["data"]["combo_id"]
    assert placed["data"] == {
        "combo_id": combo_id,
        "status": "filled",
        "legs": [
            {
                "symbol": CALL,
                "side": "buy",
                "quantity": 2,
                "filled": 2,
                "avg_price": "2.00",
                "order_nos": ["0000000301"],
            },
            {
                "symbol": CALL_730,
                "side": "sell",
                "quantity": 2,
                "filled": 2,
                "avg_price": "1.00",
                "order_nos": ["0000000302"],
            },
        ],
    }
    assert [order["sym"] for order in desk.exchange.orders] == [CALL, CALL_730]
    (message,) = window.messages
    assert "옵션 묶음 주문" in message
    assert "수직 스프레드 매수형" in message


def test_declined_in_the_window_sends_nothing_and_burns_the_token(serve: Serve) -> None:
    desk = Desk()
    desk.exchange.script = list(BOTH_FILLED)
    window = Window("decline")
    steps: list[Step] = [
        ("preview_combo", {"combo": VERTICAL}),
        ("place_combo", {"confirm_token": "$token"}),
        ("place_combo", {"confirm_token": "$token"}),
    ]

    _, declined, again = run(desk, serve, steps, window)

    assert declined["error"]["code"] == "CONFIRM_EXPIRED"
    assert again["error"]["code"] == "CONFIRM_EXPIRED"
    assert "preview_combo" in again["error"]["message"]
    assert len(window.messages) == 1
    assert desk.exchange.orders == []


def test_a_made_up_token_never_opens_the_window(serve: Serve) -> None:
    desk = Desk()
    window = Window("accept")

    (result,) = run(desk, serve, [("place_combo", {"confirm_token": "made-up"})], window)

    assert result["error"]["code"] == "CONFIRM_EXPIRED"
    assert window.messages == []
    assert desk.exchange.orders == []


def test_a_partial_result_lists_what_the_user_can_choose_next(serve: Serve) -> None:
    # 사는 다리가 1계약만 체결되고(남은 수량 자동취소) 파는 다리가 그만큼 체결된다.
    desk = Desk()
    desk.exchange.script = [filled(1, "2.00"), filled(1, "1.00")]

    preview, placed = preview_and_place(desk, serve, Window("accept"))

    data = placed["data"]
    combo_id = preview["data"]["combo_id"]
    assert data["status"] == "partial"
    assert [leg["filled"] for leg in data["legs"]] == [1, 1]
    assert "stop_reason" not in data
    complete, revert, keep = data["next"]
    assert complete.startswith("완성") and f'preview_combo(resume="{combo_id}")' in complete
    assert revert.startswith("되돌리기") and "preview_order" in revert
    assert f"{CALL} 매도 1계약" in revert and f"{CALL_730} 매수 1계약" in revert
    assert keep.startswith("유지")


def test_a_partial_combo_resumes_with_its_remaining_quantity(serve: Serve) -> None:
    desk = Desk()
    desk.exchange.script = [filled(1, "2.00"), filled(1, "1.00")]
    steps: list[Step] = [
        ("preview_combo", {"combo": VERTICAL}),
        ("place_combo", {"confirm_token": "$token"}),
        ("preview_combo", {"resume": "$combo"}),
    ]

    preview, _, resumed = run(desk, serve, steps, Window("accept"))

    assert resumed["ok"] is True, resumed
    data = resumed["data"]
    assert data["combo_id"] == preview["data"]["combo_id"]
    assert [leg["filled"] for leg in data["legs"]] == [1, 1]
    assert data["confirm_token"] != preview["data"]["confirm_token"]


def test_a_filled_combo_cannot_be_resumed(serve: Serve) -> None:
    desk = Desk()
    desk.exchange.script = list(BOTH_FILLED)
    steps: list[Step] = [
        ("preview_combo", {"combo": VERTICAL}),
        ("place_combo", {"confirm_token": "$token"}),
        ("preview_combo", {"resume": "$combo"}),
    ]

    _, _, refused = run(desk, serve, steps, Window("accept"))

    assert refused["error"]["code"] == "INVALID_INPUT"
    assert "일부만 체결된 묶음만" in refused["error"]["message"]


def test_an_unanswered_leg_stops_the_combo_and_points_to_get_orders(serve: Serve) -> None:
    # 불변식 5: 응답을 받지 못한 다리 주문은 다시 보내지 않는다. 다음 다리도 보내지 않는다.
    desk = Desk()
    desk.exchange.script = [None]

    _, placed = preview_and_place(desk, serve, Window("accept"))

    data = placed["data"]
    assert data["status"] == "unknown"
    assert data["stop_reason"] == "unknown"
    (step,) = data["next"]
    assert "get_orders" in step
    assert len(desk.exchange.orders) == 1


def test_a_leg_blocked_before_sending_reports_nothing_filled_with_the_rule(serve: Serve) -> None:
    # 미리보기 뒤 하루 프리미엄 한도를 낮추면 사는 다리의 주문 직전 재검사가 막는다.
    desk = Desk()
    desk.exchange.script = list(BOTH_FILLED)
    window = Window("accept")

    async def steps() -> dict[str, Any]:
        async with (
            serve(desk.world.mcp) as url,
            Client(url, auth="admin-token", elicitation_handler=window) as client,
        ):
            preview = (
                await client.call_tool("preview_combo", {"combo": VERTICAL})
            ).structured_content
            assert preview is not None
            lower = replace(OPTION_LIMITS, premium_daily=Decimal("500000"))
            desk.set_limits(replace(LIMITS, options=lower))
            token = {"confirm_token": preview["data"]["confirm_token"]}
            placed = (await client.call_tool("place_combo", token)).structured_content
            assert placed is not None
            return placed

    placed = asyncio.run(steps())

    data = placed["data"]
    assert data["status"] == "nothing_filled"
    assert (data["stop_reason"], data["rule"]) == ("risk_blocked", "premium_limit")
    assert "next" not in data
    assert desk.exchange.orders == []


# ---- get_orders -----------------------------------------------------------------------------


def test_get_orders_tags_the_leg_orders_with_their_combo(serve: Serve) -> None:
    desk = Desk()
    desk.exchange.script = list(BOTH_FILLED)
    # 묶음 밖의 수동 주문 하나가 내역에 먼저 있다.
    desk.exchange.earlier.append(Placed("0000000777", CALL, "1", 2, "2.00").row(Snap(2, "2.00")))
    steps: list[Step] = [
        ("preview_combo", {"combo": VERTICAL}),
        ("place_combo", {"confirm_token": "$token"}),
        ("get_orders", {}),
    ]

    preview, _, listed_orders = run(desk, serve, steps, Window("accept"))

    assert listed_orders["ok"] is True, listed_orders
    tagged = {row["order_no"]: row["combo_id"] for row in listed_orders["data"]["orders"]}
    combo_id = preview["data"]["combo_id"]
    assert tagged == {"0000000777": None, "0000000301": combo_id, "0000000302": combo_id}
