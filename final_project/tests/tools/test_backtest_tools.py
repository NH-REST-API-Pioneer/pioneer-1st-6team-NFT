"""백테스트 도구: backtest_strategy와 예제 리소스. MCP 클라이언트로 실제 HTTP 서버를 부른다.

NH는 가짜(녹화한 product-detail, 일봉 행은 명세 필드로 만든 것), 실행기도 가짜다.
시계는 2026-12-01 10:00 KST.
"""

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2
import pytest
from conftest import Serve
from fakes import ADMIN, T0, FakeNH, World
from fastmcp import Client

from nh_mcp.markets.domestic import CHART_DAILY
from nh_mcp.markets.domestic_products import PRODUCT_DETAIL
from nh_mcp.nh.client import Credentials
from nh_mcp.ops.rate_limit import BACKTEST_BUCKET, LIMITS
from nh_mcp.services.backtest import MAX_CODE_BYTES, MAX_PARAMS_BYTES, code_hash
from nh_mcp.store import users
from nh_mcp.strategies.examples import MEAN_REVERSION, MEAN_REVERSION_PARAMS

RECORDED = Path(__file__).resolve().parents[1] / "fixtures" / "nh" / "domestic"
SYMBOL = "A016C000"
EXAMPLE_URI = "strategy://examples/mean-reversion"
CODE = "def on_bar(bar, history, params):\n    return 'none'\n"
PERIOD = {"interval": "day", "start": "2026-11-02", "end": "2026-11-30"}
# 2026-11-02(월)부터 하루씩 (시가, 종가).
PRICES = [("780.00", "781.00"), ("782.00", "779.50"), ("779.00", "783.25"), ("784.00", "786.00")]


def recorded(name: str) -> dict[str, Any]:
    body: dict[str, Any] = json.loads((RECORDED / f"{name}.json").read_text("utf-8"))["body"]
    return body


def chart_rows() -> dict[str, object]:
    rows = [
        {
            "occurs1_ref_dt": f"202611{2 + i:02d}",
            "occurs1_open_pric": o,
            "occurs1_high_pric": max(o, c, key=Decimal),
            "occurs1_low_pric": min(o, c, key=Decimal),
            "occurs1_close_pric": c,
            "occurs1_cum_trd_qty": "1000",
        }
        for i, (o, c) in enumerate(PRICES)
    ]
    return {"nrec": f"{len(rows):04d}", "nxt_key": "", "Occurs1": rows[::-1]}


def lab(register: bool = True, orders_enabled: bool = False) -> World:
    nh = FakeNH()
    nh.reply(PRODUCT_DETAIL, recorded("product-detail"))
    nh.reply(CHART_DAILY, chart_rows())
    world = World(nh=httpx2.MockTransport(nh), orders_enabled=orders_enabled)
    if register:
        user = users.get_or_create(world.conn, "g-admin", ADMIN, T0)
        credentials = Credentials("fake-app-key-0000", "fake-app-secret-0000")
        asyncio.run(world.store.register(user, "domestic", credentials))
    return world


def args(**changes: object) -> dict[str, Any]:
    base: dict[str, Any] = {
        "symbol": SYMBOL,
        "strategy": {"code": CODE, "params": {"window": 3}},
        "period": PERIOD,
    }
    return base | changes


async def _calls(serve: Serve, world: World, calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    async with serve(world.mcp) as url, Client(url, auth="admin-token") as client:
        for arguments in calls:
            content = (await client.call_tool("backtest_strategy", arguments)).structured_content
            assert content is not None
            results.append(content)
    return results


def run(serve: Serve, world: World, arguments: dict[str, Any]) -> dict[str, Any]:
    [result] = asyncio.run(_calls(serve, world, [arguments]))
    return result


def ok_reply(signals: list[str]) -> dict[str, object]:
    return {"status": "ok", "signals": signals, "bar_index": None, "detail": ""}


def test_backtest_tool_is_registered_with_orders_off(serve: Serve) -> None:
    world = lab(orders_enabled=False)

    async def listed() -> list[Any]:
        async with serve(world.mcp) as url, Client(url, auth="admin-token") as client:
            return await client.list_tools()

    tools = {tool.name: tool for tool in asyncio.run(listed())}
    tool = tools["backtest_strategy"]
    assert tool.annotations is not None and tool.annotations.read_only_hint is True
    assert tool.description is not None
    first = tool.description.splitlines()[0]
    assert first == (
        "AI가 짠 전략 코드를 격리 환경에서 과거 봉으로 돌려 성과를 계산한다. 주문은 나가지 않는다."
    )
    assert EXAMPLE_URI in tool.description
    assert "preview_strategy" in tool.description


def test_returns_metrics_as_strings(serve: Serve) -> None:
    world = lab()
    world.sandbox.reply(ok_reply(["buy", "none", "exit", "sell"]))
    settings = {"quantity": 2, "fee_per_fill": "1500"}

    result = run(serve, world, args(settings=settings))

    assert result["ok"] is True
    data = result["data"]
    assert set(data) == {
        "trades",
        "win_rate",
        "net_pnl",
        "max_drawdown",
        "fees",
        "fills",
        "open_position",
        "last_signal_ignored",
        "code_hash",
        "period",
        "first_bar_at",
        "last_bar_at",
        "bars",
        "truncated",
        "warnings",
    }
    # 11-03 시가 782.00 + 0.05에 사서 11-05 시가 784.00 - 0.05에 판다. 2계약, 체결 2건에 3000원씩.
    assert data["trades"] == 1 and data["fills"] == 2
    assert isinstance(data["net_pnl"], str)
    assert Decimal(data["net_pnl"]) == (Decimal("783.95") - Decimal("782.05")) * 250000 * 2 - 6000
    assert Decimal(data["fees"]) == 6000
    assert data["win_rate"] == "1.0000"
    assert isinstance(data["max_drawdown"], str)
    # 마지막 봉의 sell은 체결할 봉이 없다.
    assert data["last_signal_ignored"] is True
    assert data["open_position"] is None
    assert data["code_hash"] == code_hash(CODE)
    assert data["period"] == PERIOD
    assert data["first_bar_at"] == "2026-11-02T00:00:00+09:00"
    assert data["last_bar_at"] == "2026-11-05T00:00:00+09:00"
    assert (data["bars"], data["truncated"], data["warnings"]) == (4, False, [])


def test_open_position_amounts_are_strings(serve: Serve) -> None:
    world = lab()
    world.sandbox.reply(ok_reply(["buy", "none", "none", "none"]))

    data = run(serve, world, args(settings={"fee_per_fill": "1000"}))["data"]

    position = data["open_position"]
    unrealized = position.pop("unrealized_pnl")
    assert position == {
        "side": "buy",
        "quantity": 1,
        "entry_at": "2026-11-03T00:00:00+09:00",
        "entry_price": "782.05",
        "mark_price": "786.00",
        "entry_fee": "1000",
    }
    assert isinstance(unrealized, str)
    assert Decimal(unrealized) == (Decimal("786.00") - Decimal("782.05")) * 250000
    # 진입 수수료는 fees에 들어 있고 net_pnl에는 없다.
    assert (data["fees"], data["net_pnl"], data["trades"]) == ("1000", "0", 0)
    assert data["win_rate"] is None


BIG_PARAMS = {"blob": "x" * MAX_PARAMS_BYTES}


@pytest.mark.parametrize(
    "arguments",
    [
        args(strategy={"code": "x" * (MAX_CODE_BYTES + 1)}),
        args(strategy={"code": "가" * (MAX_CODE_BYTES // 3 + 1)}),
        args(strategy={"code": ""}),
        args(strategy={"code": CODE, "params": BIG_PARAMS}),
        args(strategy={"code": CODE, "params": {"band": 0.02}}),
        args(strategy={"code": CODE, "params": {"bands": [1, 0.5]}}),
        args(strategy={"code": CODE, "params": {"nested": {"x": 1e3}}}),
        args(period=PERIOD | {"interval": "week"}),
        args(period=PERIOD | {"start": "2026-11-30", "end": "2026-11-01"}),
        args(period=PERIOD | {"end": "2026-12-02"}),
        args(period=PERIOD | {"start": "2026/11/01"}),
        args(period=PERIOD | {"start": "20261101"}),
        args(settings={"quantity": 0}),
        args(settings={"quantity": 101}),
        args(settings={"quantity": True}),
        args(settings={"quantity": "1"}),
        args(settings={"fee_per_fill": "-1"}),
        args(settings={"fee_per_fill": "1e3"}),
        args(settings={"fee_per_fill": "NaN"}),
        args(settings={"fee_per_fill": "\uff11\uff10"}),
        args(market="overseas"),
    ],
)
def test_input_limits(serve: Serve, arguments: dict[str, Any]) -> None:
    world = lab()

    result = run(serve, world, arguments)

    assert result["ok"] is False, result
    assert result["error"]["code"] == "INVALID_INPUT"
    assert world.sandbox.jobs == []


def test_float_params_tell_to_use_strings(serve: Serve) -> None:
    result = run(serve, lab(), args(strategy={"code": CODE, "params": {"band": 0.02}}))

    assert result["error"]["code"] == "INVALID_INPUT"
    assert '"0.02"' in result["error"]["message"]


def test_end_today_is_allowed(serve: Serve) -> None:
    result = run(serve, lab(), args(period=PERIOD | {"end": "2026-12-01"}))

    assert result["ok"] is True


def test_strategy_failure_maps_to_strategy_failed(serve: Serve) -> None:
    world = lab()
    world.sandbox.reply({"status": "memory", "signals": [], "bar_index": 2, "detail": ""})

    result = run(serve, world, args())

    assert result["ok"] is False
    assert result["error"]["code"] == "STRATEGY_FAILED"
    assert result["error"]["rule"] == "memory"
    assert "data" not in result


def test_exception_detail_is_marked_as_strategy_output(serve: Serve) -> None:
    world = lab()
    detail = "ZeroDivisionError: division by zero"
    world.sandbox.reply({"status": "exception", "signals": [], "bar_index": 1, "detail": detail})

    result = run(serve, world, args())

    assert result["error"]["code"] == "STRATEGY_FAILED"
    assert result["error"]["rule"] == "exception"
    assert result["error"]["message"].startswith("전략 코드가 낸 오류: ")
    assert detail in result["error"]["message"]


def test_bad_signal_is_invalid_input(serve: Serve) -> None:
    world = lab()
    world.sandbox.reply(
        {"status": "invalid_signal", "signals": [], "bar_index": 0, "detail": "int"}
    )

    result = run(serve, world, args())

    assert result["error"]["code"] == "INVALID_INPUT"


def test_runner_unavailable_is_retryable(serve: Serve) -> None:
    world = lab()
    world.sandbox.reply(503)

    result = run(serve, world, args())

    assert result["error"]["code"] == "TEMPORARILY_UNAVAILABLE"
    assert result["error"]["retryable"] is True


def test_key_missing(serve: Serve) -> None:
    world = lab(register=False)

    result = run(serve, world, args())

    assert result["error"]["code"] == "KEY_NOT_REGISTERED"
    assert world.sandbox.jobs == []


def test_rate_limited_before_anything_runs(serve: Serve) -> None:
    world = lab()
    limit = LIMITS[BACKTEST_BUCKET]
    assert limit == 10
    bad = args(market="overseas")

    results = asyncio.run(_calls(serve, world, [bad] * (limit + 1)))

    assert [r["error"]["code"] for r in results[:limit]] == ["INVALID_INPUT"] * limit
    assert results[limit]["error"]["code"] == "TEMPORARILY_UNAVAILABLE"
    assert results[limit]["error"]["retryable"] is True


def test_example_runs_with_its_params(serve: Serve) -> None:
    world = lab()

    result = run(
        serve, world, args(strategy={"code": MEAN_REVERSION, "params": MEAN_REVERSION_PARAMS})
    )

    assert result["ok"] is True
    [job] = world.sandbox.jobs
    assert (job["code"], job["params"]) == (MEAN_REVERSION, MEAN_REVERSION_PARAMS)


def test_example_resource_is_listed(serve: Serve) -> None:
    world = lab()

    async def scenario() -> tuple[list[Any], list[Any]]:
        async with serve(world.mcp) as url, Client(url, auth="admin-token") as client:
            return await client.list_resources(), await client.read_resource(EXAMPLE_URI)

    resources, contents = asyncio.run(scenario())

    [example] = [r for r in resources if str(r.uri) == EXAMPLE_URI]
    assert example.mime_type == "text/x-python"
    [content] = contents
    assert content.text == MEAN_REVERSION


def test_example_resource_needs_an_allowed_login(serve: Serve) -> None:
    world = lab()

    async def scenario() -> tuple[list[Any], bool]:
        async with serve(world.mcp) as url, Client(url, auth="outsider-token") as client:
            listed = await client.list_resources()
            try:
                await client.read_resource(EXAMPLE_URI)
            except Exception:
                return listed, False
            return listed, True

    listed, readable = asyncio.run(scenario())

    assert [str(r.uri) for r in listed] == []
    assert readable is False
