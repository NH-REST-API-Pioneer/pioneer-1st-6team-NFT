"""한도 도구: 조회, 낮추기(창 없음), 처음·올리기(승인 창 또는 링크), 입력 검사."""

import asyncio
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

import pytest
from conftest import Serve
from fakes import World
from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult

from nh_mcp.domain import RiskLimits, UserId
from nh_mcp.store import limits, users

CURRENT = RiskLimits(Decimal("100000"), 3, 2)
ARGS: dict[str, Any] = {"daily_loss": "100000", "max_position": 3, "max_order_qty": 2}
LOWER: dict[str, Any] = {"daily_loss": "50000", "max_position": 3, "max_order_qty": 2}
HIGHER: dict[str, Any] = {"daily_loss": "50000", "max_position": 5, "max_order_qty": 2}


@dataclass
class Window:
    """승인 창에서 사용자가 한 일을 흉내 내고, 창이 몇 번 어떤 문구로 떴는지 기록한다."""

    action: Literal["accept", "decline"]
    value: bool = True
    messages: list[str] = field(default_factory=list[str])

    async def __call__(self, message: str, *_: object) -> ElicitResult:
        self.messages.append(message)
        content = {"value": self.value} if self.action == "accept" else None
        return ElicitResult(action=self.action, content=content)


def me(world: World) -> UserId:
    return users.get_or_create(world.conn, "g-admin", "admin@example.com", world.clock())


def fresh() -> World:
    """로그인한 적 있는 사용자(admin)가 있는 서버."""
    world = World()
    return world


def stored(world: World) -> RiskLimits | None:
    return limits.get(world.conn, me(world), "domestic")


async def call(
    serve: Serve,
    world: World,
    tool: str,
    args: dict[str, Any],
    *,
    window: Window | None = None,
) -> dict[str, Any]:
    async with (
        serve(world.mcp) as url,
        Client(url, auth="admin-token", elicitation_handler=window) as client,
    ):
        content = (await client.call_tool(tool, args)).structured_content
    assert content is not None
    return content


def run(
    serve: Serve,
    world: World,
    tool: str,
    args: dict[str, Any],
    window: Window | None = None,
) -> dict[str, Any]:
    return asyncio.run(call(serve, world, tool, args, window=window))


def test_both_tools_are_listed_with_the_right_hints(serve: Serve) -> None:
    async def listed() -> dict[str, bool | None]:
        async with serve(fresh().mcp) as url, Client(url, auth="admin-token") as client:
            tools = await client.list_tools()
        return {
            t.name: t.annotations.read_only_hint if t.annotations else None
            for t in tools
            if t.name.endswith("risk_limits")
        }

    assert asyncio.run(listed()) == {"get_risk_limits": True, "set_risk_limits": False}


def test_get_without_limits_says_null_and_how_to_set(serve: Serve) -> None:
    world = fresh()

    result = run(serve, world, "get_risk_limits", {})

    assert result["ok"] is True
    assert result["data"]["limits"] is None
    assert "set_risk_limits" in result["data"]["message"]


def test_get_returns_my_limits_only(serve: Serve) -> None:
    world = fresh()
    limits.put(world.conn, me(world), "domestic", CURRENT, world.clock())
    users.get_or_create(world.conn, "g-admin2", "second@example.com", world.clock())

    mine = run(serve, world, "get_risk_limits", {})

    async def as_second_user() -> dict[str, Any]:
        async with serve(world.mcp) as url, Client(url, auth="second-admin-token") as client:
            content = (await client.call_tool("get_risk_limits", {})).structured_content
        assert content is not None
        return content

    theirs = asyncio.run(as_second_user())

    assert mine["data"]["limits"] == {"daily_loss": "100000", "max_position": 3, "max_order_qty": 2}
    assert theirs["data"]["limits"] is None


def test_overseas_is_not_supported_yet(serve: Serve) -> None:
    world = fresh()

    assert run(serve, world, "get_risk_limits", {"market": "overseas"})["error"]["code"] == (
        "INVALID_INPUT"
    )
    set_args = {**ARGS, "market": "overseas"}
    assert run(serve, world, "set_risk_limits", set_args)["error"]["code"] == "INVALID_INPUT"


def test_first_time_accepted_in_the_window_applies(serve: Serve) -> None:
    world = fresh()
    window = Window("accept")

    result = run(serve, world, "set_risk_limits", ARGS, window)

    assert result["ok"] is True
    assert result["data"]["outcome"] == "applied"
    assert result["data"]["current"] is None
    assert result["data"]["requested"] == {
        "daily_loss": "100000",
        "max_position": 3,
        "max_order_qty": 2,
    }
    assert "approval_url" not in result["data"]
    assert stored(world) == CURRENT
    assert window.messages == [
        "한도를 바꿉니다. 하루 손실 없음→100000원, 종목별 최대 없음→3계약, "
        "1회 최대 없음→2계약. 승인할까요?"
    ]


def test_first_time_declined_in_the_window_stores_nothing(serve: Serve) -> None:
    world = fresh()

    result = run(serve, world, "set_risk_limits", ARGS, Window("decline"))

    assert result["ok"] is True
    assert result["data"]["outcome"] == "declined"
    assert stored(world) is None


def test_accept_with_false_is_a_decline(serve: Serve) -> None:
    world = fresh()

    result = run(serve, world, "set_risk_limits", ARGS, Window("accept", value=False))

    assert result["data"]["outcome"] == "declined"
    assert stored(world) is None


def test_without_a_window_the_answer_is_a_link_and_nothing_is_stored(serve: Serve) -> None:
    world = fresh()

    result = run(serve, world, "set_risk_limits", ARGS)

    assert result["ok"] is True
    assert result["data"]["outcome"] == "needs_approval"
    assert result["data"]["approval_url"].startswith("http://localhost:8000/limits/approve/")
    assert result["data"]["expires_at"] == "2026-12-01T10:10:00+09:00"
    assert stored(world) is None


def test_the_link_in_the_response_is_not_in_the_audit_log(serve: Serve) -> None:
    world = fresh()

    result = run(serve, world, "set_risk_limits", ARGS)
    token = result["data"]["approval_url"].rsplit("/", 1)[1]
    rows = world.conn.execute("SELECT user_id, action, detail FROM audit_log").fetchall()

    assert [(r[0], r[1]) for r in rows] == [(me(world), "limits.set")]
    assert token not in json.dumps([list(r) for r in rows])


def test_lowering_applies_without_ever_opening_the_window(serve: Serve) -> None:
    world = fresh()
    limits.put(world.conn, me(world), "domestic", CURRENT, world.clock())
    window = Window("decline")

    result = run(serve, world, "set_risk_limits", LOWER, window)

    assert result["data"]["outcome"] == "applied"
    assert window.messages == []
    assert stored(world) == RiskLimits(Decimal("50000"), 3, 2)


def test_lowering_without_a_window_client_also_applies(serve: Serve) -> None:
    world = fresh()
    limits.put(world.conn, me(world), "domestic", CURRENT, world.clock())

    assert run(serve, world, "set_risk_limits", LOWER)["data"]["outcome"] == "applied"


def test_raising_one_value_asks_the_window(serve: Serve) -> None:
    world = fresh()
    limits.put(world.conn, me(world), "domestic", CURRENT, world.clock())
    window = Window("accept")

    result = run(serve, world, "set_risk_limits", HIGHER, window)

    assert result["data"]["outcome"] == "applied"
    assert len(window.messages) == 1
    assert stored(world) == RiskLimits(Decimal("50000"), 5, 2)


def test_raising_declined_keeps_the_old_values(serve: Serve) -> None:
    world = fresh()
    limits.put(world.conn, me(world), "domestic", CURRENT, world.clock())

    result = run(serve, world, "set_risk_limits", HIGHER, Window("decline"))

    assert result["data"]["outcome"] == "declined"
    assert stored(world) == CURRENT


@pytest.mark.parametrize(
    "bad",
    [
        {"daily_loss": "0"},
        {"daily_loss": "0.00"},
        {"daily_loss": "-1"},
        {"daily_loss": "abc"},
        {"daily_loss": ""},
        {"daily_loss": "NaN"},
        {"daily_loss": "Infinity"},
        {"daily_loss": "-Infinity"},
        {"daily_loss": "1e5"},
        {"daily_loss": "1E+5"},
        {"daily_loss": "1e999999999"},
        {"daily_loss": "+5"},
        {"daily_loss": "1_000"},
        {"daily_loss": "\uff11\uff10\uff10"},
        {"daily_loss": "1" * 40},
        {"max_position": 0},
        {"max_position": -1},
        {"max_position": 10**30},
        {"max_order_qty": 0},
        {"max_order_qty": -3},
        {"max_position": True},
        {"max_position": "3"},
        {"max_position": 3.0},
        {"max_order_qty": True},
        {"max_order_qty": "2"},
        {"max_order_qty": 2.0},
    ],
)
def test_bad_values_are_invalid_input_and_never_ask_or_store(
    serve: Serve, bad: dict[str, Any]
) -> None:
    world = fresh()
    window = Window("accept")

    result = run(serve, world, "set_risk_limits", {**ARGS, **bad}, window)

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert window.messages == []
    assert stored(world) is None


def test_surrounding_spaces_and_decimals_are_accepted(serve: Serve) -> None:
    world = fresh()

    result = run(
        serve, world, "set_risk_limits", {**ARGS, "daily_loss": " 100000.50 "}, Window("accept")
    )

    assert result["data"]["outcome"] == "applied"
    assert stored(world) == RiskLimits(Decimal("100000.50"), 3, 2)


def test_there_is_no_token_or_message_argument(serve: Serve) -> None:
    async def names() -> set[str]:
        async with serve(fresh().mcp) as url, Client(url, auth="admin-token") as client:
            tools = await client.list_tools()
        return {
            name
            for t in tools
            if t.name == "set_risk_limits"
            for name in t.input_schema["properties"]
        }

    assert asyncio.run(names()) == {"daily_loss", "max_position", "max_order_qty", "market"}


def test_counts_are_advertised_as_integers(serve: Serve) -> None:
    async def properties() -> dict[str, Any]:
        async with serve(fresh().mcp) as url, Client(url, auth="admin-token") as client:
            tools = await client.list_tools()
        (tool,) = [t for t in tools if t.name == "set_risk_limits"]
        found: dict[str, Any] = tool.input_schema["properties"]
        return found

    found = asyncio.run(properties())
    assert found["max_position"] == {"type": "integer"}
    assert found["max_order_qty"] == {"type": "integer"}


def test_the_eleventh_change_in_a_minute_is_refused_before_anything_is_stored(
    serve: Serve,
) -> None:
    """창이 없는 클라이언트로 센다. 2026-07-28 규격의 승인 창은 도구를 두 번 부르므로 두 번 센다."""
    world = fresh()

    async def eleven() -> list[dict[str, Any]]:
        async with serve(world.mcp) as url, Client(url, auth="admin-token") as client:
            results: list[dict[str, Any]] = []
            for _ in range(11):
                content = (await client.call_tool("set_risk_limits", ARGS)).structured_content
                assert content is not None
                results.append(content)
        return results

    results = asyncio.run(eleven())

    assert [r["data"]["outcome"] for r in results[:10]] == ["needs_approval"] * 10
    assert results[10]["error"]["code"] == "TEMPORARILY_UNAVAILABLE"
    assert results[10]["error"]["retryable"] is True
    issued: tuple[int] = world.conn.execute("SELECT COUNT(*) FROM confirm_tokens").fetchone()
    assert issued[0] == 10
