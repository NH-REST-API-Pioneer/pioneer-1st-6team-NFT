"""틱 저장 도구(set_tick_recording, get_strategy_data)를 MCP로 불러 본다.

서버는 운영과 같은 조립(`attach`)이고, NH REST는 가짜 전송, NH 실시간은 127.0.0.1의 가짜 서버다.
웹소켓 키는 운영과 같은 길(사용자의 국내 키 → NH 클라이언트 → 웹소켓 키 GET)로 받는다.
"""

import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest
from conftest import Serve
from fakes import ADMIN, FAKE_NH_TOKEN, FakeNH, World, browser_login
from fastmcp import Client
from fastmcp.exceptions import ToolError
from mcp.shared.exceptions import MCPError
from realtime.fake_stream import FakeStream, until

from nh_mcp.auth.web import SESSION_COOKIE
from nh_mcp.domain import KST, UserId
from nh_mcp.keys.page import DELETE_PATH
from nh_mcp.nh.client import WEBSOCKET_KEY_PATH, Credentials
from nh_mcp.ops.rate_limit import LIMITS, RECORDING_BUCKET
from nh_mcp.store import recordings, users
from nh_mcp.store.ticks import Block, Sub

SYM = "A016C000"
NEXT = "A0173000"
TICK, BOOK = Sub(SYM, "tick"), Sub(SYM, "book")
FAKE_KEY = "fake-app-key-1234"
FAKE_SECRET = "fake-app-secret-0000"
WS_KEY = "fake-ws-key-from-nh-0001"
META = {"env": "demo", "as_of": "2026-12-01T10:00:00+09:00"}
KEYS_URL = "http://localhost:8000/keys"
MODELS_URL = "http://localhost:8000/models"
NOTE = (
    "지금부터 받는 체결·호가만 저장합니다(NH는 지난 틱을 주지 않습니다). "
    "평일 주간장(08:45~15:45)만 저장하고 30일이 지나면 지웁니다. "
    "연결이 끊긴 동안은 빈 구간으로 남습니다."
)
BARS = "1분봉·일봉은 저장하지 않아도 NH에서 바로 받아 backtest_strategy에 씁니다."
NO_STREAM = {"connected": False, "error": None}
LIVE = {"connected": True, "error": None}


class Rig:
    """틱 저장이 켜진 서버 하나와 가짜 NH(REST·실시간)."""

    def __init__(self, tmp_path: Path, fake: FakeStream | None = None) -> None:
        self.tmp_path = tmp_path
        self.fake = fake or FakeStream()
        self.nh = FakeNH()
        self.nh.reply(WEBSOCKET_KEY_PATH, {"access_key": WS_KEY})
        self.world = World(nh=httpx2.MockTransport(self.nh), data_dir=tmp_path)
        self.world.stream = self.fake.connect
        self.url = ""

    def register(self, sub: str = "g-admin", email: str = ADMIN) -> UserId:
        user = users.get_or_create(self.world.conn, sub, email, self.world.clock())
        asyncio.run(self.world.store.register(user, "domestic", Credentials(FAKE_KEY, FAKE_SECRET)))
        return user

    def again(self) -> "Rig":
        """서버를 새로 띄운다: 새 프로세스가 같은 DB 내용과 같은 ticks.db를 연다."""
        restarted = Rig(self.tmp_path, self.fake)
        self.world.conn.backup(restarted.world.conn)
        return restarted

    @asynccontextmanager
    async def running(self, serve: Serve) -> AsyncGenerator["Rig"]:
        async with (
            self.fake.running(),
            self.world.recorder.running(),
            serve(self.world.mcp) as url,
        ):
            self.url = url
            yield self

    async def call(
        self, tool: str, args: dict[str, Any] | None = None, token: str = "admin-token"
    ) -> dict[str, Any]:
        async with Client(self.url, auth=token) as client:
            content = (await client.call_tool(tool, args or {})).structured_content
        assert content is not None
        return content

    async def record(
        self, symbols: dict[str, list[str]], token: str = "admin-token"
    ) -> dict[str, Any]:
        return await self.call("set_tick_recording", {"symbols": symbols}, token)

    async def data(self, token: str = "admin-token") -> dict[str, Any]:
        return await self.call("get_strategy_data", {}, token)

    def live(self, user: UserId) -> set[Sub]:
        status = self.world.recorder.status(user)
        return set() if status is None else set(status.live)

    async def closed(self, user: UserId) -> None:
        """이 사용자의 NH 실시간 연결이 닫히고 받는 구독이 없을 때까지 기다린다.

        구독 해제(D)를 보내고 바로 닫으므로 가짜 서버가 마지막 해제를 읽기 전에 끊길 수 있다.
        그래서 해제 메시지 대신 연결이 닫혔는지를 본다.
        """
        status = self.world.recorder.status
        await until(lambda: self.fake.last.socket.close_code is not None)
        await until(lambda: (s := status(user)) is not None and not s.connected and not s.live)


def kst(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 12, 1, hour, minute, second, tzinfo=KST)


SUBSCRIBED = [
    {"symbol": SYM, "stream": "book", "state": "subscribed"},
    {"symbol": SYM, "stream": "tick", "state": "subscribed"},
]


def test_both_tools_are_in_the_basic_bundle_and_say_what_they_change(serve: Serve) -> None:
    async def scenario() -> dict[str, Any]:
        # 주문 스위치가 꺼진 서버(기본 묶음)에도 있다.
        async with serve(World().mcp) as url, Client(url, auth="admin-token") as client:
            return {tool.name: tool for tool in await client.list_tools()}

    tools = asyncio.run(scenario())
    setter, reader = tools["set_tick_recording"].annotations, tools["get_strategy_data"].annotations
    assert setter is not None and reader is not None
    assert setter.read_only_hint is False
    assert setter.destructive_hint is False
    assert setter.idempotent_hint is True
    assert reader.read_only_hint is True
    assert set(tools["set_tick_recording"].input_schema["properties"]) == {"symbols", "market"}
    assert set(tools["get_strategy_data"].input_schema["properties"]) == {"market"}


@pytest.mark.parametrize("token", [None, "made-up-token"])
def test_tools_need_login(serve: Serve, token: str | None) -> None:
    async def scenario() -> None:
        async with serve(World().mcp) as url:
            with pytest.raises(MCPError):
                async with Client(url, auth=token) as client:
                    await client.call_tool("get_strategy_data", {})

    asyncio.run(scenario())


def test_logged_in_but_not_allowed_cannot_use_them(serve: Serve) -> None:
    world = World()

    async def scenario() -> list[str]:
        async with serve(world.mcp) as url, Client(url, auth="outsider-token") as client:
            names = [tool.name for tool in await client.list_tools()]
            for tool, args in (
                ("set_tick_recording", {"symbols": {SYM: ["tick"]}}),
                ("get_strategy_data", {}),
            ):
                with pytest.raises(ToolError):
                    await client.call_tool(tool, args)
            return names

    assert asyncio.run(scenario()) == []
    assert recordings.load_all(world.conn) == {}


def test_turning_on_subscribes_and_the_same_set_again_sends_nothing(
    serve: Serve, tmp_path: Path
) -> None:
    rig = Rig(tmp_path)
    user = rig.register()

    async def scenario() -> tuple[dict[str, Any], dict[str, Any], int, int]:
        async with rig.running(serve):
            first = await rig.record({SYM: ["tick", "book"]})
            sent = len(rig.fake.last.received)
            second = await rig.record({SYM: ["book", "tick", "tick"]})
            await asyncio.sleep(0.05)
            return first, second, sent, len(rig.fake.last.received)

    first, second, sent, sent_after = asyncio.run(scenario())

    assert first == {
        "ok": True,
        "data": {
            "recording": {SYM: ["book", "tick"]},
            "subscriptions": SUBSCRIBED,
            "used": 2,
            "limit": 20,
            "connection": LIVE,
            "note": NOTE,
        },
        "meta": META,
    }
    assert second == first
    assert sent_after == sent
    assert len(rig.fake.conns) == 1
    assert rig.fake.last.access_key == WS_KEY
    assert rig.fake.last.sent("A") == [("KB", SYM), ("KA", SYM)]
    assert recordings.load(rig.world.conn, user) == {TICK, BOOK}


def test_the_value_is_the_whole_set_and_empty_stops_everything(
    serve: Serve, tmp_path: Path
) -> None:
    rig = Rig(tmp_path)
    user = rig.register()

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        async with rig.running(serve):
            await rig.record({SYM: ["tick", "book"]})
            narrowed = await rig.record({SYM: ["tick"]})
            await until(lambda: rig.fake.last.sent("D") == [("KB", SYM)])
            stopped = await rig.record({})
            await rig.closed(user)
            return narrowed, stopped

    narrowed, stopped = asyncio.run(scenario())

    assert narrowed["data"]["recording"] == {SYM: ["tick"]}
    assert narrowed["data"]["used"] == 1
    assert stopped["data"]["recording"] == {}
    assert stopped["data"]["subscriptions"] == []
    assert stopped["data"]["used"] == 0
    assert recordings.load(rig.world.conn, user) == frozenset()


def test_more_than_twenty_subscriptions_are_refused_before_anything_changes(
    serve: Serve, tmp_path: Path
) -> None:
    rig = Rig(tmp_path)
    user = rig.register()
    symbols = {f"B{n:03d}C000": ["tick", "book"] for n in range(10)} | {SYM: ["tick"]}

    async def scenario() -> dict[str, Any]:
        async with rig.running(serve):
            return await rig.record(symbols)

    result = asyncio.run(scenario())

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    message = result["error"]["message"]
    assert "20건까지" in message and "21건" in message
    assert "자동 실행 전략" in message
    assert recordings.load(rig.world.conn, user) == frozenset()
    assert rig.fake.conns == []


@pytest.mark.parametrize(
    ("args", "named"),
    [
        ({"symbols": {"A999X999": ["tick"]}}, "A999X999"),
        # 종목 마스터에 있지만 선물이 아니다(콜옵션).
        ({"symbols": {"B016C180": ["tick"]}}, "B016C180"),
        ({"symbols": {SYM: ["minute"]}}, "1분봉"),
        ({"symbols": {SYM: []}}, SYM),
        ({"symbols": {"": ["tick"]}}, "search_symbol"),
        ({"symbols": {SYM: ["tick"]}, "market": "overseas"}, "domestic"),
    ],
)
def test_unknown_symbols_non_futures_and_other_markets_are_refused(
    serve: Serve, tmp_path: Path, args: dict[str, Any], named: str
) -> None:
    rig = Rig(tmp_path)
    user = rig.register()

    async def scenario() -> dict[str, Any]:
        async with rig.running(serve):
            return await rig.call("set_tick_recording", args)

    result = asyncio.run(scenario())

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"
    assert named in result["error"]["message"]
    assert recordings.load(rig.world.conn, user) == frozenset()
    assert rig.fake.conns == []


def test_without_a_key_turning_on_is_key_not_registered_but_stopping_works(
    serve: Serve, tmp_path: Path
) -> None:
    rig = Rig(tmp_path)

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        async with rig.running(serve):
            return await rig.record({SYM: ["tick"]}), await rig.record({})

    refused, stopped = asyncio.run(scenario())

    assert refused["ok"] is False
    assert refused["error"]["code"] == "KEY_NOT_REGISTERED"
    assert KEYS_URL in refused["error"]["message"]
    assert stopped["ok"] is True
    assert stopped["data"]["recording"] == {}
    assert recordings.load_all(rig.world.conn) == {}
    assert rig.fake.conns == []


def test_users_cannot_see_or_change_each_others_recordings(serve: Serve, tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    me = rig.register()
    other = rig.register("g-admin2", "second@example.com")

    async def scenario() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        async with rig.running(serve):
            await rig.record({SYM: ["tick"]})
            rig.world.recorder.store.write(
                me,
                [Block(TICK, kst(10, 0), [(1, 36000, 72615, 2, 72610, 72615, 1, 10)])],
                [],
                kst(10, 1),
            )
            theirs = await rig.data("second-admin-token")
            stopped = await rig.record({}, "second-admin-token")
            await asyncio.sleep(0.05)
            mine = await rig.data()
            return theirs, stopped, mine

    theirs, stopped, mine = asyncio.run(scenario())

    assert theirs["data"]["recorded"] == []
    assert theirs["data"]["subscriptions"] == {"used": 0, "limit": 20}
    assert stopped["data"]["recording"] == {}
    assert recordings.load(rig.world.conn, me) == {TICK}
    assert recordings.load(rig.world.conn, other) == frozenset()
    assert [
        (r["symbol"], r["stream"], r["recording"], r["rows"]) for r in mine["data"]["recorded"]
    ] == [(SYM, "tick", "subscribed", 1)]
    # 다른 사용자는 연결을 하나도 열지 않았다.
    assert len(rig.fake.conns) == 1


def test_disconnect_account_stops_recording_and_forgets_the_set(
    serve: Serve, tmp_path: Path
) -> None:
    rig = Rig(tmp_path)
    user = rig.register()

    async def scenario() -> dict[str, Any]:
        async with rig.running(serve):
            await rig.record({SYM: ["tick", "book"]})
            await rig.call("disconnect_account")
            await rig.closed(user)
            return await rig.data()

    after = asyncio.run(scenario())

    assert recordings.load(rig.world.conn, user) == frozenset()
    # 일부러 멈춘 것은 빈 구간이 아니다. 저장한 것도 빈 구간도 없으니 아무 흐름도 없다.
    assert after["data"]["recorded"] == []
    assert after["data"]["subscriptions"] == {"used": 0, "limit": 20}
    assert after["data"]["connection"] == NO_STREAM
    # 새 키를 넣고 서버를 다시 띄워도 저절로 다시 켜지지 않는다.
    restarted = rig.again()
    restarted.register()

    async def later() -> None:
        async with restarted.running(serve):
            await asyncio.sleep(0.1)

    asyncio.run(later())
    assert len(rig.fake.conns) == 1


def test_deleting_keys_on_the_key_page_stops_recording_too(serve: Serve, tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    user = rig.register()

    async def scenario() -> int:
        async with rig.running(serve), httpx2.AsyncClient() as http:
            await rig.record({SYM: ["tick"]})
            base = rig.url.removesuffix("/mcp")
            await browser_login(http, base, "admin-code")
            web_user = rig.world.login.user_from_cookie(http.cookies.get(SESSION_COOKIE) or "")
            assert web_user is not None
            deleted = await http.post(base + DELETE_PATH, data={"csrf": web_user.csrf})
            await rig.closed(user)
            return deleted.status_code

    assert asyncio.run(scenario()) == 303
    assert rig.world.store.summaries(user) == []
    assert recordings.load(rig.world.conn, user) == frozenset()


def test_restarting_the_server_resumes_saved_recordings(serve: Serve, tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    user = rig.register()

    async def first() -> None:
        async with rig.running(serve):
            await rig.record({SYM: ["tick", "book"]})

    async def second(again: Rig) -> dict[str, Any]:
        async with again.running(serve):
            await until(lambda: again.live(user) == {TICK, BOOK})
            return await again.data()

    asyncio.run(first())
    again = rig.again()
    after = asyncio.run(second(again))

    assert len(rig.fake.conns) == 2
    assert rig.fake.last.sent("A") == [("KB", SYM), ("KA", SYM)]
    recorded = after["data"]["recorded"]
    assert [(r["stream"], r["recording"]) for r in recorded] == [
        ("book", "subscribed"),
        ("tick", "subscribed"),
    ]
    # 내려가 있던 동안은 빈 구간이고, 다시 구독되며 닫혔다(시계는 멈춰 있어 시작과 끝이 같다).
    for row in recorded:
        assert row["gaps"] == {
            "total": 1,
            "recent": [
                {
                    "start": "2026-12-01T10:00:00+09:00",
                    "end": "2026-12-01T10:00:00+09:00",
                    "reason": "server_stop",
                }
            ],
        }


def test_strategy_data_shows_ranges_gaps_and_models(serve: Serve, tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    user = rig.register()

    async def scenario() -> dict[str, Any]:
        async with rig.running(serve):
            await rig.record({SYM: ["tick"]})
            store = rig.world.recorder.store
            rows_a = [(n, 36000 + n, 72615, 1, 72610, 72615, 1, n) for n in range(1, 4)]
            rows_b = [(n, 36060 + n, 72620, 1, 72615, 72620, 2, n) for n in range(4, 6)]
            store.write(
                user,
                [Block(TICK, kst(10, 0), rows_a), Block(TICK, kst(10, 5), rows_b)],
                [],
                kst(10, 6),
            )
            # 저장을 멈춘 지난 흐름: 다음 종목 호가, 닫힌 빈 구간 하나와 열린 빈 구간 하나.
            old = Sub(NEXT, "book")
            store.write(user, [Block(old, kst(9, 0), [(6, 32400, *([None] * 64))])], [], kst(9, 1))
            store.open_gap(user, old, kst(9, 10), "disconnect")
            store.close_gap(user, old, kst(9, 12, 30))
            store.open_gap(user, old, kst(9, 20), "crash")
            draft = rig.world.models.draft(user)
            draft.write(b"model-bytes")
            await rig.world.models.commit(user, draft, "lgbm.txt")
            return await rig.data()

    result = asyncio.run(scenario())

    [model] = result["data"]["models"]
    assert model == {
        "name": "lgbm.txt",
        "sha256": model["sha256"],
        "size": 11,
        "uploaded_at": "2026-12-01T10:00:00+09:00",
    }
    assert len(model["sha256"]) == 64
    assert result == {
        "ok": True,
        "data": {
            "recorded": [
                {
                    "symbol": SYM,
                    "stream": "tick",
                    "recording": "subscribed",
                    "start": "2026-12-01T10:00:00+09:00",
                    "end": "2026-12-01T10:06:00+09:00",
                    "rows": 5,
                    "gaps": {"total": 0, "recent": []},
                },
                {
                    "symbol": NEXT,
                    "stream": "book",
                    "recording": "off",
                    "start": "2026-12-01T09:00:00+09:00",
                    "end": "2026-12-01T09:01:00+09:00",
                    "rows": 1,
                    "gaps": {
                        "total": 2,
                        "recent": [
                            {
                                "start": "2026-12-01T09:10:00+09:00",
                                "end": "2026-12-01T09:12:30+09:00",
                                "reason": "disconnect",
                            },
                            {
                                "start": "2026-12-01T09:20:00+09:00",
                                "end": "진행 중",
                                "reason": "crash",
                            },
                        ],
                    },
                },
            ],
            "recorded_total": 2,
            "subscriptions": {"used": 1, "limit": 20},
            "connection": LIVE,
            "models": [model],
            "models_url": MODELS_URL,
            "bars": BARS,
        },
        "meta": META,
    }


def test_strategy_data_is_bounded(serve: Serve, tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    user = rig.register()

    async def scenario() -> dict[str, Any]:
        async with rig.running(serve):
            await rig.record({SYM: ["tick"]})
            store = rig.world.recorder.store
            for n in range(25):
                store.open_gap(user, TICK, kst(9, n), "disconnect")
                store.close_gap(user, TICK, kst(9, n, 30))
            # 지난 흐름 45개, 하나에 한 행. 나중 것일수록 늦게 저장했다.
            seq = 100
            for n in range(45):
                seq += 1
                old = Sub(f"B{n:03d}C000", "tick")
                at = kst(8, 0) + timedelta(minutes=n)
                store.write(user, [Block(old, at, [(seq, 0, 1, 1, 1, 1, 1, 1)])], [], at)
            return await rig.data()

    data = asyncio.run(scenario())["data"]

    assert data["recorded_total"] == 46
    listed = data["recorded"]
    assert len(listed) == 40
    # 지금 저장하는 것이 먼저, 그다음은 최근에 저장한 것부터.
    assert listed[0]["symbol"] == SYM
    assert [r["symbol"] for r in listed[1:3]] == ["B044C000", "B043C000"]
    assert listed[0]["gaps"]["total"] == 25
    recent = listed[0]["gaps"]["recent"]
    assert len(recent) == 20
    assert recent[0]["start"] == "2026-12-01T09:05:00+09:00"
    assert recent[-1]["start"] == "2026-12-01T09:24:00+09:00"


def test_nh_refusal_is_shown_without_secrets(serve: Serve, tmp_path: Path) -> None:
    fake = FakeStream()
    fake.refuse.add(("KB", SYM))
    # NH가 거절 사유에 키나 토큰을 되돌려 보내는 최악의 경우까지 본다.
    fake.refusal = f"거절 {FAKE_NH_TOKEN} {WS_KEY}"
    rig = Rig(tmp_path, fake)
    rig.register()

    async def scenario() -> list[dict[str, Any]]:
        async with rig.running(serve):
            turned = await rig.record({SYM: ["tick", "book"]})
            return [turned, await rig.data(), await rig.call("get_connection_status")]

    answers = asyncio.run(scenario())
    turned = answers[0]["data"]

    assert turned["subscriptions"] == [
        {"symbol": SYM, "stream": "book", "state": "error", "message": "거절 *** ***"},
        {"symbol": SYM, "stream": "tick", "state": "subscribed"},
    ]
    book = answers[1]["data"]["recorded"][0]
    assert (book["stream"], book["recording"], book["message"]) == ("book", "error", "거절 *** ***")
    assert book["gaps"]["recent"][0]["reason"] == "subscribe_failed"
    text = json.dumps(answers, ensure_ascii=False)
    for secret in (FAKE_KEY, FAKE_SECRET, FAKE_NH_TOKEN, WS_KEY):
        assert secret not in text


def test_changing_recording_is_rate_limited_before_anything_changes(
    serve: Serve, tmp_path: Path
) -> None:
    # 설정을 바꿀 때마다 NH에 웹소켓 키를 새로 받으므로 켰다 껐다를 막는다.
    rig = Rig(tmp_path)
    user = rig.register()
    limit = LIMITS[RECORDING_BUCKET]
    assert limit == 10

    async def scenario() -> list[dict[str, Any]]:
        async with rig.running(serve):
            stopped = [await rig.record({}) for _ in range(limit)]
            return [*stopped, await rig.record({SYM: ["tick"]})]

    results = asyncio.run(scenario())

    assert [r["ok"] for r in results[:limit]] == [True] * limit
    refused = results[limit]
    assert refused["error"]["code"] == "TEMPORARILY_UNAVAILABLE"
    assert refused["error"]["retryable"] is True
    assert recordings.load(rig.world.conn, user) == frozenset()
    assert rig.fake.conns == []
