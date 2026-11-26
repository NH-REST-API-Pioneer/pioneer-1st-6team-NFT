"""틱 저장과 실행 중인 전략이 구독 20건을 같이 쓴다. 가짜 NH(REST·실시간)에 붙인 서비스를 부른다."""

import asyncio
from pathlib import Path

import httpx2
from fakes import FakeNH, World
from realtime.fake_stream import FakeStream, until

from nh_mcp.domain import JsonValue
from nh_mcp.markets.domestic import DomesticMarket
from nh_mcp.markets.domestic_master import DomesticMaster
from nh_mcp.nh.client import WEBSOCKET_KEY_PATH, Credentials
from nh_mcp.realtime.recorder import MAX_SUBSCRIPTIONS
from nh_mcp.services.queries import QueryService
from nh_mcp.services.recording import RecordingInvalid, RecordingService
from nh_mcp.store import recordings, users
from nh_mcp.store.ticks import Sub

SYM, NEXT = "A016C000", "A0173000"
TICK, BOOK = Sub(SYM, "tick"), Sub(SYM, "book")


def test_the_limit_counts_recording_and_strategies_together(tmp_path: Path) -> None:
    nh = FakeNH()
    nh.reply(WEBSOCKET_KEY_PATH, {"access_key": "fake-ws-key-0001"})
    world = World(nh=httpx2.MockTransport(nh), data_dir=tmp_path)
    fake = FakeStream()
    world.stream = fake.connect
    user = users.get_or_create(world.conn, "g-admin", "admin@example.com", world.clock())
    asyncio.run(world.store.register(user, "domestic", Credentials("fake-key", "fake-secret")))
    master = DomesticMaster(world.clock, transport=httpx2.MockTransport(world.master))
    queries = QueryService(DomesticMarket(world.pool, master, world.clock))
    service = RecordingService(world.conn, world.recorder, queries, world.models, "")
    # 전략이 SYM 체결과 다른 18건을 받는다: 19건.
    owned = {TICK} | {Sub(f"B{n:03d}C000", "tick") for n in range(MAX_SUBSCRIPTIONS - 2)}

    async def scenario() -> tuple[dict[str, JsonValue], str]:
        async with fake.running(), world.recorder.running():
            await world.recorder.subscribe(user, "run-1", frozenset(owned))
            await until(lambda: (s := world.recorder.status(user)) is not None and s.connected)
            # SYM 체결은 이미 전략이 받으니 호가만 늘어 20건이다.
            fits = await service.set(user, frozenset({TICK, BOOK}))
            try:
                await service.set(user, frozenset({TICK, BOOK, Sub(NEXT, "tick")}))
            except RecordingInvalid as exc:
                return fits, exc.message
            raise AssertionError("21건이 받아들여졌습니다.")

    fits, refused = asyncio.run(scenario())

    assert fits["used"] == MAX_SUBSCRIPTIONS
    assert "틱 저장 3건" in refused and "전략의 구독 19건" in refused and "21건" in refused
    assert recordings.load(world.conn, user) == {TICK, BOOK}
