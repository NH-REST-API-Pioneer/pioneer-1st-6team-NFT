"""preview_strategy 도구: 주문 스위치, 입력 검사, 백테스트 뒤 미리보기. 실제 HTTP 서버를 부른다.

NH는 가짜(녹화한 product-detail, 일봉 행은 명세 필드로 만든 것), 실행기도 가짜다.
시계는 2026-12-01 10:00 KST. 한도는 하루 손실 500,000원, 최대 3계약, 1회 2계약.
"""

import asyncio
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2
import pytest
from conftest import Serve
from fakes import ADMIN, T0, FakeNH, World
from fastmcp import Client
from mcp_types import Tool

from nh_mcp.domain import KST, RiskLimits
from nh_mcp.markets.domestic import CHART_DAILY
from nh_mcp.markets.domestic_products import PRODUCT_DETAIL
from nh_mcp.nh.client import Credentials
from nh_mcp.ops.rate_limit import LIMITS as RATE_LIMITS
from nh_mcp.ops.rate_limit import PREVIEW_BUCKET
from nh_mcp.services.backtest import code_hash
from nh_mcp.store import limits, users

RECORDED = Path(__file__).resolve().parents[1] / "fixtures" / "nh" / "domestic"
SYMBOL = "A016C000"
CODE = "def on_bar(bar, history, params):\n    return 'none'\n"
PERIOD = {"interval": "day", "start": "2026-11-02", "end": "2026-11-30"}
PRICES = [("780.00", "781.00"), ("782.00", "779.50"), ("779.00", "783.25"), ("784.00", "786.00")]
EXPIRES = (T0 + timedelta(days=2)).astimezone(KST).isoformat(timespec="seconds")
SCOPE: dict[str, Any] = {
    "quantity": 1,
    "max_position": 2,
    "daily_orders": 10,
    "daily_loss": "300000",
    "expires_at": EXPIRES,
    "on_loss": "liquidate",
    "mode": "auto",
}


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


def lab(orders_enabled: bool = True, with_limits: bool = True) -> World:
    nh = FakeNH()
    nh.reply(PRODUCT_DETAIL, recorded("product-detail"))
    nh.reply(CHART_DAILY, chart_rows())
    world = World(nh=httpx2.MockTransport(nh), orders_enabled=orders_enabled)
    user = users.get_or_create(world.conn, "g-admin", ADMIN, T0)
    credentials = Credentials("fake-app-key-0000", "fake-app-secret-0000")
    asyncio.run(world.store.register(user, "domestic", credentials))
    if with_limits:
        limits.put(world.conn, user, "domestic", RiskLimits(Decimal("500000"), 3, 2), T0)
    return world


def strategy(**changes: object) -> dict[str, Any]:
    return {"code": CODE, "params": {"window": 3}, "interval": "day"} | changes


def preview_args(**changes: object) -> dict[str, Any]:
    base: dict[str, Any] = {"symbol": SYMBOL, "strategy": strategy(), "scope": SCOPE}
    return base | changes


def backtest_args() -> dict[str, Any]:
    return {
        "symbol": SYMBOL,
        "strategy": {"code": CODE, "params": {"window": 3}},
        "period": PERIOD,
    }


type Call = tuple[str, dict[str, Any]]


async def _calls(serve: Serve, world: World, calls: list[Call]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    async with serve(world.mcp) as url, Client(url, auth="admin-token") as client:
        for tool, arguments in calls:
            content = (await client.call_tool(tool, arguments)).structured_content
            assert content is not None
            results.append(content)
    return results


def preview(serve: Serve, world: World, arguments: dict[str, Any]) -> dict[str, Any]:
    [result] = asyncio.run(_calls(serve, world, [("preview_strategy", arguments)]))
    return result


async def _listed(serve: Serve, world: World) -> list[Tool]:
    async with serve(world.mcp) as url, Client(url, auth="admin-token") as client:
        return await client.list_tools()


def test_preview_strategy_is_absent_with_orders_off(serve: Serve) -> None:
    names = {t.name for t in asyncio.run(_listed(serve, lab(orders_enabled=False)))}

    assert "preview_strategy" not in names
    assert "backtest_strategy" in names


def test_preview_strategy_is_listed_with_orders_on(serve: Serve) -> None:
    tools = {t.name: t for t in asyncio.run(_listed(serve, lab()))}

    tool = tools["preview_strategy"]
    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is False
    assert tool.annotations.destructive_hint is False
    assert tool.description is not None
    assert "start_strategy가 아직 없다" in tool.description
    assert "backtest_strategy" in tool.description
    assert set(tool.input_schema["properties"]) == {"symbol", "strategy", "scope", "market"}


def test_end_to_end_backtest_then_preview(serve: Serve) -> None:
    world = lab()

    backtest, result = asyncio.run(
        _calls(
            serve,
            world,
            [("backtest_strategy", backtest_args()), ("preview_strategy", preview_args())],
        )
    )

    assert backtest["ok"] is True
    assert result["ok"] is True, result
    data = result["data"]
    assert set(data) == {"confirm_token", "code_hash", "summary", "expires_at"}
    assert data["code_hash"] == code_hash(CODE) == backtest["data"]["code_hash"]
    assert data["summary"] == (
        "코스피200 F 202612 · 일봉 · 1회 1계약 · 최대 2계약 · 하루 10회 · "
        f"일일 손실 300,000원 · {EXPIRES[:10]} {EXPIRES[11:16]}까지 · "
        "손실 한도에서 정리 · 자동 모드"
    )
    assert data["expires_at"] == (T0 + timedelta(minutes=2)).astimezone(KST).isoformat(
        timespec="seconds"
    )
    assert data["expires_at"].endswith("+09:00")
    [(purpose,)] = world.conn.execute("SELECT purpose FROM confirm_tokens").fetchall()
    assert purpose == "strategy"
    # 감사 로그와 응답 밖에는 토큰 원문이 없다.
    logged = world.conn.execute("SELECT detail FROM audit_log WHERE action = 'strategy.preview'")
    assert data["confirm_token"] not in "".join(str(r[0]) for r in logged.fetchall())


def test_without_backtest_the_preview_is_blocked(serve: Serve) -> None:
    result = preview(serve, lab(), preview_args())

    assert result["ok"] is False
    assert result["error"]["code"] == "RISK_BLOCKED"
    assert result["error"]["rule"] == "not_backtested"


def test_scope_over_limits_is_blocked(serve: Serve) -> None:
    result = preview(serve, lab(), preview_args(scope=SCOPE | {"daily_loss": "500001"}))

    assert result["error"]["code"] == "RISK_BLOCKED"
    assert result["error"]["rule"] == "scope_exceeds_limits"
    assert "daily_loss" in result["error"]["message"]


def test_limits_not_set(serve: Serve) -> None:
    result = preview(serve, lab(with_limits=False), preview_args())

    assert result["error"]["code"] == "LIMITS_NOT_SET"


def test_unknown_symbol_is_invalid(serve: Serve) -> None:
    result = preview(serve, lab(), preview_args(symbol="NOSUCH"))

    assert result["error"]["code"] == "INVALID_INPUT"


@pytest.mark.parametrize(
    "arguments",
    [
        preview_args(scope=SCOPE | {"on_loss": "panic"}),
        preview_args(scope=SCOPE | {"mode": "manual"}),
        preview_args(scope=SCOPE | {"daily_loss": "3e5"}),
        preview_args(scope=SCOPE | {"daily_loss": "-1"}),
        preview_args(scope=SCOPE | {"daily_loss": "0"}),
        preview_args(scope=SCOPE | {"daily_loss": "300,000"}),
        preview_args(scope=SCOPE | {"expires_at": "2026-12-03T10:00:00"}),
        preview_args(scope=SCOPE | {"expires_at": "2026-12-03"}),
        preview_args(scope=SCOPE | {"expires_at": "내일"}),
        preview_args(scope=SCOPE | {"quantity": 0}),
        preview_args(scope=SCOPE | {"quantity": "1"}),
        preview_args(scope=SCOPE | {"quantity": True}),
        preview_args(scope=SCOPE | {"max_position": 0}),
        preview_args(scope=SCOPE | {"daily_orders": 0}),
        preview_args(scope=SCOPE | {"daily_orders": 101}),
        preview_args(scope=SCOPE | {"daily_orders": 1.5}),
        preview_args(strategy=strategy(interval="week")),
        preview_args(strategy=strategy(code="")),
        preview_args(strategy=strategy(params={"band": 0.02})),
        preview_args(market="overseas"),
    ],
)
def test_bad_input_is_invalid(serve: Serve, arguments: dict[str, Any]) -> None:
    world = lab()

    result = preview(serve, world, arguments)

    assert result["ok"] is False, result
    assert result["error"]["code"] == "INVALID_INPUT"
    assert world.conn.execute("SELECT COUNT(*) FROM confirm_tokens").fetchone() == (0,)


def test_expiry_out_of_range_is_invalid_input(serve: Serve) -> None:
    past = (T0 - timedelta(hours=1)).isoformat()
    late = (T0 + timedelta(days=8)).isoformat()

    results = asyncio.run(
        _calls(
            serve,
            lab(),
            [
                ("preview_strategy", preview_args(scope=SCOPE | {"expires_at": past})),
                ("preview_strategy", preview_args(scope=SCOPE | {"expires_at": late})),
            ],
        )
    )

    assert [r["error"]["code"] for r in results] == ["INVALID_INPUT", "INVALID_INPUT"]


def test_utc_z_suffix_is_accepted(serve: Serve) -> None:
    world = lab()
    expires = (T0 + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    backtest, result = asyncio.run(
        _calls(
            serve,
            world,
            [
                ("backtest_strategy", backtest_args()),
                ("preview_strategy", preview_args(scope=SCOPE | {"expires_at": expires})),
            ],
        )
    )

    assert backtest["ok"] is True and result["ok"] is True, result


def test_rate_limited_before_anything_else(serve: Serve) -> None:
    limit = RATE_LIMITS[PREVIEW_BUCKET]
    bad = preview_args(market="overseas")

    results = asyncio.run(_calls(serve, lab(), [("preview_strategy", bad)] * (limit + 1)))

    assert [r["error"]["code"] for r in results[:limit]] == ["INVALID_INPUT"] * limit
    assert results[limit]["error"]["code"] == "TEMPORARILY_UNAVAILABLE"
    assert results[limit]["error"]["retryable"] is True
