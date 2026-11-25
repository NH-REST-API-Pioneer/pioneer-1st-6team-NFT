"""백테스트 도구: backtest_strategy와 예제 리소스. MCP 클라이언트로 실제 HTTP 서버를 부른다.

NH는 가짜(녹화한 product-detail, 일봉 행은 명세 필드로 만든 것), 실행기도 가짜다.
시계는 2026-12-01 10:00 KST.
"""

import asyncio
import base64
import hashlib
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest
from conftest import Serve
from fakes import ADMIN, T0, FakeNH, World, by_bar, scripted
from fastmcp import Client

from nh_mcp.domain import KST, Target, UserId
from nh_mcp.markets.domestic import CHART_DAILY
from nh_mcp.markets.domestic_products import PRODUCT_DETAIL
from nh_mcp.nh.client import Credentials
from nh_mcp.ops.rate_limit import BACKTEST_BUCKET, LIMITS
from nh_mcp.services.backtest import MAX_CODE_BYTES, MAX_PARAMS_BYTES, MIXED, NO_MODEL
from nh_mcp.store import ticks, users
from nh_mcp.store.ticks import Block, Sub
from nh_mcp.strategies.examples import (
    MEAN_REVERSION,
    MEAN_REVERSION_PARAMS,
    PAIR_SPREAD,
    PAIR_SPREAD_PARAMS,
    SCALE_IN_LIMIT,
    TICK_MOMENTUM,
    TICK_MOMENTUM_PARAMS,
)
from nh_mcp.tools.backtest import BAD_MODEL, MODELS_URL_SLOT

RECORDED = Path(__file__).resolve().parents[1] / "fixtures" / "nh" / "domestic"
SYMBOL = "A016C000"  # 코스피200 F 202612: 틱 0.05, 승수 250000
MINI = "A056C000"  # 미니코스피 F 202612: 틱 0.02, 승수 50000
EXAMPLE_URI = "strategy://examples/mean-reversion"
PAIR_URI = "strategy://examples/pair-spread"
TICK_URI = "strategy://examples/tick-momentum"
SCALE_URI = "strategy://examples/scale-in-limit"
CODE = "def on_event(event, ctx):\n    return None\n"
DATA: dict[str, Any] = {"streams": {SYMBOL: ["day"]}, "start": "2026-11-02", "end": "2026-11-30"}
# 2026-11-02(월)부터 하루씩 (시가, 종가).
PRICES = [("780.00", "781.00"), ("782.00", "779.50"), ("779.00", "783.25"), ("784.00", "786.00")]


def recorded(name: str) -> dict[str, Any]:
    body: dict[str, Any] = json.loads((RECORDED / f"{name}.json").read_text("utf-8"))["body"]
    return body


def day_rows(bars: list[tuple[str, str, str, str]]) -> dict[str, object]:
    """2026-11-02부터 하루씩 (시가, 고가, 저가, 종가). NH처럼 최근 것부터 준다."""
    rows = [
        {
            "occurs1_ref_dt": f"202611{2 + i:02d}",
            "occurs1_open_pric": o,
            "occurs1_high_pric": h,
            "occurs1_low_pric": low,
            "occurs1_close_pric": c,
            "occurs1_cum_trd_qty": "1000",
        }
        for i, (o, h, low, c) in enumerate(bars)
    ]
    return {"nrec": f"{len(rows):04d}", "nxt_key": "", "Occurs1": rows[::-1]}


def chart_rows() -> dict[str, object]:
    return day_rows([(o, max(o, c, key=Decimal), min(o, c, key=Decimal), c) for o, c in PRICES])


class Lab:
    def __init__(
        self, register: bool = True, orders_enabled: bool = False, data_dir: Path | None = None
    ) -> None:
        self.nh = FakeNH()
        self.nh.reply(PRODUCT_DETAIL, recorded("product-detail"))
        self.nh.reply(CHART_DAILY, chart_rows())
        self.world = World(
            nh=httpx2.MockTransport(self.nh), orders_enabled=orders_enabled, data_dir=data_dir
        )
        if register:
            user = users.get_or_create(self.world.conn, "g-admin", ADMIN, T0)
            credentials = Credentials("fake-app-key-0000", "fake-app-secret-0000")
            asyncio.run(self.world.store.register(user, "domestic", credentials))


def lab(register: bool = True, orders_enabled: bool = False) -> World:
    return Lab(register, orders_enabled).world


def args(**changes: object) -> dict[str, Any]:
    base: dict[str, Any] = {"strategy": {"code": CODE, "params": {"window": 3}}, "data": DATA}
    return base | changes


def data(**changes: object) -> dict[str, Any]:
    return DATA | changes


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
        "AI가 짠 전략 코드를 격리 환경에서 과거 봉·틱으로 돌려 성과를 잰다. 주문은 나가지 않는다."
    )
    for needed in (EXAMPLE_URI, PAIR_URI, "preview_strategy", "on_event(event, ctx)", "init(ctx)"):
        assert needed in tool.description, needed
    # 틱 실행: 받는 흐름과 섞는 규칙, 이벤트 모양, 데이터가 어디서 오는지, 상한, 빈 구간.
    for needed in (
        TICK_URI,
        '{"type": "tick", "symbol", "at", "price", "qty", "bid", "ask"}',
        '{"type": "book", "symbol", "at", "bids": [[가격,',
        "set_tick_recording",
        "같이 넣을 수 없다",
        "90,000개까지",
        "25,000개만",
        "gaps",
        "first_event_at",
        "호가 이벤트로는 체결되지",
    ):
        assert needed in tool.description, needed
    assert "{max_" not in tool.description
    # 모델 파일을 쓰는 법과 올리는 화면의 주소(설정의 공개 주소에서 만든 것).
    for needed in ("ctx.model_path", "strategy.model", "http://localhost:8000/models 화면"):
        assert needed in tool.description, needed
    assert MODELS_URL_SLOT not in tool.description
    for section in ("쓸 때:", "쓰지 않을 때:", "다음:"):
        assert section in tool.description, section
    schema = tool.input_schema
    assert set(schema["properties"]) == {"strategy", "data", "settings", "market"}
    assert schema["required"] == ["strategy", "data"]
    assert set(schema["properties"]["data"]["properties"]) == {"streams", "start", "end"}
    assert set(schema["properties"]["strategy"]["properties"]) == {"code", "params", "model"}
    assert schema["properties"]["strategy"]["required"] == ["code"]
    settings = schema["properties"]["settings"]["anyOf"][0]
    assert set(settings["properties"]) == {"max_position", "fee_per_fill"}
    # 모르는 칸(옛 settings.quantity 같은 것)은 버리지 않고 거절한다고 스키마에도 적힌다.
    assert settings["additionalProperties"] is False
    assert schema["properties"]["data"]["additionalProperties"] is False


# 두 종목 일봉. (시가, 고가, 저가, 종가), 2026-11-02부터 하루씩.
KOSPI_BARS = [
    ("780.00", "781.00", "779.00", "780.50"),
    ("782.00", "783.00", "781.00", "782.50"),
    ("784.00", "786.00", "783.50", "785.00"),
    ("785.50", "787.00", "784.00", "786.00"),
]
MINI_BARS = [
    ("780.00", "781.00", "779.00", "780.20"),
    ("781.00", "782.00", "780.00", "781.50"),
    ("783.00", "784.00", "782.00", "783.40"),
    ("784.00", "785.00", "783.00", "784.60"),
]


def day(d: int) -> str:
    return f"2026-11-{d:02d}T00:00:00+09:00"


# 봉마다 전략이 내는 목표. 나머지 봉에는 None.
TARGETS = {
    # 11-03 시가 782.00 + 0.05에 2계약 매수
    (SYMBOL, "day", day(2)): {SYMBOL: Target(2)},
    # 781.50 지정가 매도 1계약. 11-03 고가 782.00이 넘어서서 체결
    (MINI, "day", day(2)): {MINI: Target(-1, Decimal("781.50"))},
    # 11-05 시가 785.50 - 0.05에 정리
    (SYMBOL, "day", day(4)): {SYMBOL: Target(0)},
    # 마지막 봉의 목표는 체결할 봉이 없다: 시장가 1계약, 790.00 지정가 -2계약
    (SYMBOL, "day", day(5)): {SYMBOL: Target(1)},
    (MINI, "day", day(5)): {MINI: Target(-2, Decimal("790.00"))},
}

# 손 계산(테스트 대상 코드를 부르지 않고 셈한 값):
# - KOSPI 거래: (785.45 - 782.05) * 2계약 * 250000 = 1,700,000, 수수료 체결 2건 * 2계약 * 1000
#   = 4000 → 1,696,000.00
# - MINI는 781.50에 숏 1계약이 남는다. 평가 (784.60 - 781.50) * -50000 = -155,000.00, 수수료 1000
# - 자본(이벤트마다, 순서 K02 M02 K03 M03 K04 M04 K05 M05): 0, 0, 223000, 222000, 1472000,
#   1377000, 1600000, 1540000 → 최대 낙폭 1472000 - 1377000 = 95,000.00
EXPECTED: dict[str, Any] = {
    "code_hash": hashlib.sha256(CODE.encode("utf-8")).hexdigest(),
    "model_hash": None,
    "period": {"start": "2026-11-02", "end": "2026-11-30"},
    "streams": {SYMBOL: ["day"], MINI: ["day"]},
    "settings": {"max_position": 2, "fee_per_fill": "1000"},
    "totals": {
        "trades": 1,
        "fills": 3,
        "win_rate": "1.0000",
        "net_pnl": "1696000.00",
        "max_drawdown": "95000.00",
        "fees": "5000",
    },
    "symbols": {
        SYMBOL: {"trades": 1, "fills": 2, "net_pnl": "1696000.00", "fees": "4000"},
        MINI: {"trades": 0, "fills": 1, "net_pnl": "0", "fees": "1000"},
    },
    "trades": [
        {
            "symbol": SYMBOL,
            "side": "buy",
            "quantity": 2,
            "entry_at": day(3),
            "entry_price": "782.05",
            "exit_at": day(5),
            "exit_price": "785.45",
            "pnl": "1696000.00",
        }
    ],
    "open_positions": [
        {
            "symbol": MINI,
            "side": "sell",
            "quantity": 1,
            "entry_at": day(3),
            "entry_price": "781.50",
            "mark_price": "784.60",
            "unrealized_pnl": "-155000.00",
            "realized_pnl": "0.00",
            "fees": "1000",
        }
    ],
    "pending_orders": [
        {"symbol": SYMBOL, "position": 1, "price": None, "decided_at": day(6)},
        {"symbol": MINI, "position": -2, "price": "790.00", "decided_at": day(6)},
    ],
    "coverage": {
        symbol: {
            "day": {"first_bar_at": day(2), "last_bar_at": day(5), "bars": 4, "truncated": False}
        }
        for symbol in (SYMBOL, MINI)
    },
    "events": 8,
    "truncated": False,
    "warnings": [],
}


def test_two_symbol_response_is_the_hand_computed_one(serve: Serve) -> None:
    built = Lab()
    built.nh.reply_for(CHART_DAILY, SYMBOL, day_rows(KOSPI_BARS))
    built.nh.reply_for(CHART_DAILY, MINI, day_rows(MINI_BARS))
    built.world.sandbox.reply(by_bar(TARGETS))
    streams = {MINI: ["day"], SYMBOL: ["day"]}
    settings = {"max_position": 2, "fee_per_fill": "1000"}

    result = run(serve, built.world, args(data=data(streams=streams), settings=settings))

    assert result["ok"] is True, result
    assert result["data"] == EXPECTED
    assert result["meta"]["env"] == "demo"


def test_one_symbol_defaults_and_pending_order(serve: Serve) -> None:
    world = lab()
    world.sandbox.reply(scripted(["buy", "none", "exit", "sell"]))

    body = run(serve, world, args())["data"]

    assert body["settings"] == {"max_position": 1, "fee_per_fill": "0"}
    # 11-03 시가 782.00 + 0.05에 사서 11-05 시가 784.00 - 0.05에 판다. 1계약, 수수료 0.
    assert body["totals"]["trades"] == 1 and body["totals"]["fills"] == 2
    assert Decimal(body["totals"]["net_pnl"]) == (Decimal("783.95") - Decimal("782.05")) * 250000
    # 마지막 봉의 sell은 체결할 봉이 없어 걸린 채 끝난다.
    assert body["pending_orders"] == [
        {"symbol": SYMBOL, "position": -1, "price": None, "decided_at": day(6)}
    ]
    assert body["open_positions"] == []
    assert body["coverage"] == {
        SYMBOL: {
            "day": {"first_bar_at": day(2), "last_bar_at": day(5), "bars": 4, "truncated": False}
        }
    }


BIG_PARAMS = {"blob": "x" * MAX_PARAMS_BYTES}
SIX_SYMBOLS = {f"A016C00{i}": ["day"] for i in range(6)}


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
        args(data=data(streams={})),
        args(data=data(streams={SYMBOL: []})),
        args(data=data(streams={SYMBOL: ["week"]})),
        args(data=data(streams={SYMBOL: ["day", "1m"]})),
        args(data=data(streams={"": ["day"]})),
        args(data=data(streams={"A" * 33: ["day"]})),
        args(data=data(streams=SIX_SYMBOLS)),
        args(data=data(start="2026-11-30", end="2026-11-01")),
        args(data=data(end="2026-12-02")),
        args(data=data(start="2026/11/01")),
        args(data=data(start="20261101")),
        args(settings={"max_position": 0}),
        args(settings={"max_position": 101}),
        args(settings={"max_position": True}),
        args(settings={"max_position": "1"}),
        args(settings={"max_position": 1.5}),
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


@pytest.mark.parametrize("max_position", [1, 100])
def test_max_position_edges_pass(serve: Serve, max_position: int) -> None:
    world = lab()

    result = run(serve, world, args(settings={"max_position": max_position}))

    assert result["ok"] is True, result
    [job] = world.sandbox.jobs
    assert job["max_position"] == max_position


def test_too_many_symbols_names_the_cap(serve: Serve) -> None:
    world = lab()

    result = run(serve, world, args(data=data(streams=SIX_SYMBOLS)))

    assert "5개까지" in result["error"]["message"]


@pytest.mark.parametrize(
    "streams", [{SYMBOL: ["minute", "tick"]}, {SYMBOL: ["book"], MINI: ["minute", "day"]}]
)
def test_minute_with_tick_or_book_is_refused_with_the_reason(
    serve: Serve, streams: dict[str, list[str]]
) -> None:
    built = Lab()

    result = run(serve, built.world, args(data=data(streams=streams)))

    assert result["error"]["code"] == "INVALID_INPUT"
    assert result["error"]["message"] == MIXED
    assert built.nh.requests == []
    assert built.world.sandbox.jobs == []


TICK_DAY = "2026-11-25"


def kst(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 11, 25, hour, minute, second, tzinfo=KST)


def recorded_ticks(path: Path, user: int) -> None:
    """수신기가 저장한 것처럼 ticks.db에 체결 2개·호가 1개와 끝난 빈 구간 하나를 쓴다."""
    store = ticks.TickStore(ticks.connect(path))
    seconds = 10 * 3600
    trade_rows = [
        (1, seconds, 72600, 1, 72595, 72600, 1, 5),
        (3, seconds + 2, 72605, 2, None, None, 1, 7),
    ]
    book_row = (2, seconds + 1, 4, 1, 3, 1, *([72605, 4, 1, 72600, 3, 1] + [0] * 54))
    tick_sub, book_sub = Sub(SYMBOL, "tick"), Sub(SYMBOL, "book")
    store.write(
        UserId(user),
        [Block(tick_sub, kst(10, 0), trade_rows), Block(book_sub, kst(10, 0), [book_row])],
        [],
        kst(10, 1),
    )
    store.open_gap(UserId(user), tick_sub, kst(10, 30), "disconnect")
    store.close_gap(UserId(user), tick_sub, kst(10, 31))
    store.close()


def test_a_tick_run_through_mcp_reads_my_recording(serve: Serve, tmp_path: Path) -> None:
    built = Lab(data_dir=tmp_path)
    user = users.get_or_create(built.world.conn, "g-admin", ADMIN, T0)
    recorded_ticks(tmp_path / "ticks.db", user)
    arguments = args(
        strategy={"code": TICK_MOMENTUM, "params": TICK_MOMENTUM_PARAMS},
        data=data(streams={SYMBOL: ["tick", "book"]}, start=TICK_DAY, end=TICK_DAY),
    )

    async def scenario() -> dict[str, Any]:
        async with built.world.recorder.running():
            [result] = await _calls(serve, built.world, [arguments])
            return result

    result = asyncio.run(scenario())

    assert result["ok"] is True, result
    body = result["data"]
    assert body["streams"] == {SYMBOL: ["book", "tick"]}
    assert (body["events"], body["truncated"]) == (3, False)
    assert body["coverage"] == {
        SYMBOL: {
            "book": {
                "first_event_at": "2026-11-25T10:00:01+09:00",
                "last_event_at": "2026-11-25T10:00:01+09:00",
                "events": 1,
                "truncated": False,
                "gaps": {"total": 0, "recent": []},
            },
            "tick": {
                "first_event_at": "2026-11-25T10:00:00+09:00",
                "last_event_at": "2026-11-25T10:00:02+09:00",
                "events": 2,
                "truncated": False,
                "gaps": {
                    "total": 1,
                    "recent": [
                        {
                            "start": "2026-11-25T10:30:00+09:00",
                            "end": "2026-11-25T10:31:00+09:00",
                            "reason": "disconnect",
                        }
                    ],
                },
            },
        }
    }
    assert len(body["warnings"]) == 1 and "빈 구간이 1개" in body["warnings"][0]
    [job] = built.world.sandbox.jobs
    assert [e["type"] for e in cast(list[dict[str, Any]], job["events"])] == [
        "tick",
        "book",
        "tick",
    ]
    assert (job["code"], job["params"]) == (TICK_MOMENTUM, TICK_MOMENTUM_PARAMS)


def test_a_tick_run_with_nothing_recorded_tells_how_to_record(serve: Serve, tmp_path: Path) -> None:
    built = Lab(data_dir=tmp_path)
    arguments = args(data=data(streams={SYMBOL: ["tick"]}, start=TICK_DAY, end=TICK_DAY))

    async def scenario() -> dict[str, Any]:
        async with built.world.recorder.running():
            [result] = await _calls(serve, built.world, [arguments])
            return result

    result = asyncio.run(scenario())

    assert result["error"]["code"] == "INVALID_INPUT"
    assert f"{SYMBOL} 체결(tick)" in result["error"]["message"]
    assert "set_tick_recording" in result["error"]["message"]
    assert built.world.sandbox.jobs == []


def test_unknown_symbol_is_blocked_before_nh(serve: Serve) -> None:
    built = Lab()

    result = run(serve, built.world, args(data=data(streams={SYMBOL: ["day"], "NOSUCH": ["day"]})))

    assert result["error"]["code"] == "INVALID_INPUT"
    assert "search_symbol" in result["error"]["message"]
    assert built.nh.requests == []
    assert built.world.sandbox.jobs == []


async def _raw_call(serve: Serve, world: World, arguments: dict[str, Any]) -> tuple[bool, str]:
    async with serve(world.mcp) as url, Client(url, auth="admin-token") as client:
        result = await client.call_tool("backtest_strategy", arguments, raise_on_error=False)
        return result.is_error, str(result.content)


@pytest.mark.parametrize(
    "arguments",
    [
        args(settings={"quantity": 2}),
        args(data=data(interval="day")),
        args(strategy={"code": CODE, "streams": {SYMBOL: ["day"]}}),
        args(symbol=SYMBOL),
        {"strategy": {"code": CODE}, "period": {"interval": "day", "start": "2026-11-02"}},
    ],
)
def test_old_or_unknown_fields_are_refused_not_dropped(
    serve: Serve, arguments: dict[str, Any]
) -> None:
    world = lab()

    is_error, content = asyncio.run(_raw_call(serve, world, arguments))

    assert is_error is True, content
    assert world.sandbox.jobs == []


def test_float_params_tell_to_use_strings(serve: Serve) -> None:
    result = run(serve, lab(), args(strategy={"code": CODE, "params": {"band": 0.02}}))

    assert result["error"]["code"] == "INVALID_INPUT"
    assert '"0.02"' in result["error"]["message"]


def test_end_today_is_allowed(serve: Serve) -> None:
    result = run(serve, lab(), args(data=data(end="2026-12-01")))

    assert result["ok"] is True


def test_strategy_failure_maps_to_strategy_failed(serve: Serve) -> None:
    world = lab()
    world.sandbox.reply({"status": "memory", "outputs": [], "index": 2, "detail": ""})

    result = run(serve, world, args())

    assert result["ok"] is False
    assert result["error"]["code"] == "STRATEGY_FAILED"
    assert result["error"]["rule"] == "memory"
    assert "data" not in result


def test_exception_detail_is_marked_as_strategy_output(serve: Serve) -> None:
    world = lab()
    detail = "ZeroDivisionError: division by zero"
    world.sandbox.reply({"status": "exception", "outputs": [], "index": 1, "detail": detail})

    result = run(serve, world, args())

    assert result["error"]["code"] == "STRATEGY_FAILED"
    assert result["error"]["rule"] == "exception"
    assert result["error"]["message"].startswith("전략 코드가 낸 오류: ")
    assert detail in result["error"]["message"]


def test_bad_output_is_invalid_input(serve: Serve) -> None:
    world = lab()
    world.sandbox.reply({"status": "invalid_output", "outputs": [], "index": 0, "detail": "int"})

    result = run(serve, world, args())

    assert result["error"]["code"] == "INVALID_INPUT"
    assert '"position"' in result["error"]["message"]


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


def test_mean_reversion_example_runs_with_its_params(serve: Serve) -> None:
    world = lab()

    result = run(
        serve, world, args(strategy={"code": MEAN_REVERSION, "params": MEAN_REVERSION_PARAMS})
    )

    assert result["ok"] is True
    [job] = world.sandbox.jobs
    assert (job["code"], job["params"]) == (MEAN_REVERSION, MEAN_REVERSION_PARAMS)


def test_pair_example_runs_with_its_params_on_two_symbols(serve: Serve) -> None:
    world = lab()
    a, b = str(PAIR_SPREAD_PARAMS["a"]), str(PAIR_SPREAD_PARAMS["b"])
    arguments = args(
        strategy={"code": PAIR_SPREAD, "params": PAIR_SPREAD_PARAMS},
        data=data(streams={a: ["day"], b: ["day"]}),
    )

    result = run(serve, world, arguments)

    assert result["ok"] is True, result
    [job] = world.sandbox.jobs
    assert (job["code"], job["params"]) == (PAIR_SPREAD, PAIR_SPREAD_PARAMS)
    assert job["streams"] == {a: ["day"], b: ["day"]}


def test_example_resources_are_listed(serve: Serve) -> None:
    world = lab()

    async def scenario() -> tuple[list[Any], list[Any], list[Any], list[Any], list[Any]]:
        async with serve(world.mcp) as url, Client(url, auth="admin-token") as client:
            return (
                await client.list_resources(),
                await client.read_resource(EXAMPLE_URI),
                await client.read_resource(PAIR_URI),
                await client.read_resource(TICK_URI),
                await client.read_resource(SCALE_URI),
            )

    resources, mean, pair, tick, scale = asyncio.run(scenario())

    listed = {str(r.uri): r for r in resources}
    uris = (EXAMPLE_URI, PAIR_URI, TICK_URI, SCALE_URI)
    assert set(uris) <= set(listed)
    assert {listed[u].mime_type for u in uris} == {"text/x-python"}
    assert [c.text for c in mean] == [MEAN_REVERSION]
    assert [c.text for c in pair] == [PAIR_SPREAD]
    assert [c.text for c in tick] == [TICK_MOMENTUM]
    assert [c.text for c in scale] == [SCALE_IN_LIMIT]


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


@pytest.mark.parametrize("model", ["A" * 64, "a" * 63, "g" * 64, " " + "a" * 63, ""])
def test_model_must_be_a_lowercase_sha256(serve: Serve, model: str) -> None:
    world = lab()
    code = {"code": CODE, "model": model}
    result = run(serve, world, args(strategy=code))
    assert result["ok"] is False and result["error"]["code"] == "INVALID_INPUT"
    assert result["error"]["message"] == BAD_MODEL
    assert world.sandbox.jobs == []


def test_unknown_model_is_invalid_input(serve: Serve, tmp_path: Path) -> None:
    world = Lab(data_dir=tmp_path).world
    result = run(serve, world, args(strategy={"code": CODE, "model": "0" * 64}))
    assert result["ok"] is False and result["error"]["code"] == "INVALID_INPUT"
    assert result["error"]["message"] == NO_MODEL
    assert result["error"]["message"].startswith("그런 모델 파일이 없습니다")
    assert world.sandbox.jobs == []


def test_uploaded_model_is_sent_and_named_in_the_answer(serve: Serve, tmp_path: Path) -> None:
    world = Lab(data_dir=tmp_path).world
    user = users.get_or_create(world.conn, "g-admin", ADMIN, T0)
    draft = world.models.draft(user)
    draft.write(b"trained elsewhere")
    asyncio.run(world.models.commit(user, draft, "model.joblib"))
    sha256 = hashlib.sha256(b"trained elsewhere").hexdigest()

    result = run(serve, world, args(strategy={"code": CODE, "model": sha256}))

    assert result["ok"] is True, result
    assert result["data"]["model_hash"] == sha256
    [job] = world.sandbox.jobs
    assert base64.b64decode(cast(str, job["model"])) == b"trained elsewhere"
