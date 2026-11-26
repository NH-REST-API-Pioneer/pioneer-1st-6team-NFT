"""자동 실행기가 쓰는 서비스 입구: 승인 내용 읽기, 키 지우기 전에 전략 멈추기, 실시간 표시."""

import asyncio
import json
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import httpx2
import pytest
from fakes import T0, NoRuns, World, auto_run_on

from nh_mcp.domain import (
    JsonValue,
    Market,
    RiskLimits,
    StrategyScope,
    Streams,
    TickEvent,
    UserId,
)
from nh_mcp.markets.domestic import DomesticMarket
from nh_mcp.markets.domestic_master import DomesticMaster
from nh_mcp.nh.client import Credentials
from nh_mcp.realtime.feed import FeedGap, FeedResumed, Subscription
from nh_mcp.services.backtest import run_key
from nh_mcp.services.connection import ConnectionService
from nh_mcp.services.queries import QueryService
from nh_mcp.services.recording import RecordingService
from nh_mcp.services.runs import (
    Approved,
    FeedBack,
    FeedEnded,
    FeedLost,
    LiveFeed,
    LiveItem,
    parse_approval,
)
from nh_mcp.services.strategy import StrategyRequest, StrategyService
from nh_mcp.store import confirm_tokens, limits, nh_keys, users
from nh_mcp.store.backtests import SUCCESS, NewRun, record
from nh_mcp.store.ticks import Sub

SYMBOL = "A016C000"
CODE = "def on_event(event, ctx):\n    return None\n"
PARAMS: dict[str, JsonValue] = {"window": 3, "band": "0.02"}
SCOPE = StrategyScope(1, 2, 10, Decimal("300000"), T0 + timedelta(days=2), "liquidate", "auto")
STREAMS: Streams = {SYMBOL: frozenset({"minute", "day"})}


def issued_payload() -> str:
    """실제 preview_strategy가 토큰에 담은 승인 내용."""
    world = World()
    user = users.get_or_create(world.conn, "g-admin", "admin@example.com", T0)
    limits.put(world.conn, user, "domestic", RiskLimits(Decimal("500000"), 3, 2), T0)
    key = run_key(CODE, PARAMS, STREAMS, None)
    period = (date(2026, 11, 2), date(2026, 11, 30))
    record(world.conn, NewRun(user, key, CODE, "{}", period, None, None, 5, SUCCESS, None), T0)
    master = DomesticMaster(world.clock, transport=httpx2.MockTransport(world.master))
    domestic = DomesticMarket(world.pool, master, world.clock)
    auto_run_on(world.conn, user)
    service = StrategyService(world.conn, world.clock, domestic, world.models, NoRuns())
    preview = asyncio.run(service.preview(user, StrategyRequest(CODE, PARAMS, STREAMS, SCOPE)))
    row = confirm_tokens.find(world.conn, preview.confirm_token)
    assert row is not None
    return row.payload


def test_reads_back_what_preview_strategy_issued() -> None:
    approved = parse_approval(issued_payload())

    assert approved == Approved(
        streams=STREAMS,
        code_hash=run_key(CODE, PARAMS, STREAMS, None).code_hash,
        params=PARAMS,
        model_hash=None,
        scope=SCOPE,
    )


def changed(edits: dict[str, object]) -> str:
    values: dict[str, Any] = json.loads(issued_payload())
    for key, value in edits.items():
        if key.startswith("scope_"):
            values["scope"][key.removeprefix("scope_")] = value
        else:
            values[key] = value
    return json.dumps(values)


@pytest.mark.parametrize(
    "edits",
    [
        {"market": "overseas"},
        {"streams": {SYMBOL: ["minute", "minutes"]}},
        {"streams": {SYMBOL: ["tick", "minute"]}},
        {"params": '{"band": "0.02", "window": 3}'},
        {"params": '{"band":0.02,"window":3}'},
        {"model_hash": "not-a-hash"},
        {"code_hash": None},
        {"scope_quantity": "1"},
        {"scope_daily_loss": "-1"},
        {"scope_expires_at": "2026-12-03T10:00:00"},
        {"scope_mode": "yolo"},
    ],
)
def test_refuses_any_changed_shape(edits: dict[str, object]) -> None:
    assert parse_approval(changed(edits)) is None


def test_disconnect_stops_strategies_while_the_key_is_still_there() -> None:
    world = World()
    user = users.get_or_create(world.conn, "g-admin", "admin@example.com", T0)
    asyncio.run(world.store.register(user, "domestic", Credentials("app-key-1", "secret-1")))
    master = DomesticMaster(world.clock, transport=httpx2.MockTransport(world.master))
    queries = QueryService(DomesticMarket(world.pool, master, world.clock))
    recording = RecordingService(world.conn, world.recorder, queries, world.models, "url")
    seen: list[tuple[UserId, list[Market]]] = []

    async def stop_strategies(who: UserId) -> None:
        seen.append((who, nh_keys.registered_markets(world.conn, who)))

    service = ConnectionService(world.store, world.pool, recording, "url", stop_strategies)
    asyncio.run(service.disconnect(user))

    assert seen == [(user, ["domestic"])]
    assert nh_keys.registered_markets(world.conn, user) == []


def test_live_feed_turns_the_recorder_marks_into_runner_items() -> None:
    sub = Sub(SYMBOL, "tick")
    handle = Subscription(UserId(1), "run-1", frozenset({sub}))
    tick = TickEvent(SYMBOL, T0, Decimal("780.00"), 1, None, None)
    handle.push(FeedGap(sub, T0, "disconnect"))
    handle.push(FeedResumed(sub, T0 + timedelta(seconds=5)))
    handle.push(tick)

    async def read() -> list[LiveItem]:
        live = LiveFeed(handle)
        items = [await live.next() for _ in range(3)]
        handle.end("slow_consumer")
        return [*items, await live.next()]

    assert asyncio.run(read()) == [
        FeedLost(SYMBOL, "tick", T0),
        FeedBack(SYMBOL, "tick", T0 + timedelta(seconds=5)),
        tick,
        FeedEnded("slow_consumer"),
    ]
