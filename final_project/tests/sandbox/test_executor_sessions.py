"""실행기의 세션 입구(M4b). 가짜 세션으로 입구 검사·상한·상태·만료를 본다. 실제 Docker로 잇는 것은
test_session.py에 있다.
"""

import asyncio
import base64
import re
from collections.abc import AsyncGenerator, Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

import httpx2
import pytest

from nh_mcp.domain import (
    KST,
    MAX_MODEL_BYTES,
    FillEvent,
    JsonValue,
    Output,
    SandboxJob,
    SandboxResult,
    SessionFailed,
    SessionJob,
    StrategyEvent,
    Target,
)
from nh_mcp.sandbox.container import SessionClosedError
from nh_mcp.sandbox.executor import (
    MAX_EVENT_BODY,
    MAX_LIFETIME,
    MAX_SESSION_JOB_BODY,
    MAX_SESSIONS,
    Session,
    make_app,
)
from nh_mcp.sandbox.wire import TOKEN_HEADER, event_to_wire, session_to_json

from .wire import SYMBOL, TERMS, bars

TOKEN = "t" * 40
AUTH = {TOKEN_HEADER: TOKEN}
CODE = "def on_event(event, ctx):\n    return None\n"
START = datetime(2026, 11, 25, 9, 0, tzinfo=KST)
SESSION = SessionJob(
    code=CODE,
    params={"window": 5},
    streams={SYMBOL: frozenset({"minute"})},
    max_position=1,
    terms=TERMS,
    expires_at=START + timedelta(days=1),
)
BAR = bars(1)[0]
FILL = FillEvent(SYMBOL, START, "sell", 2, Decimal("350.05"))

type Body = dict[str, JsonValue]


class Now:
    """멈춰 둔 시계. 테스트가 앞으로 돌린다."""

    def __init__(self) -> None:
        self.at = START

    def __call__(self) -> datetime:
        return self.at


class FakeSession:
    """넣은 이벤트를 적고 정해 둔 답을 준다. gate를 주면 풀릴 때까지 붙잡는다."""

    def __init__(self, reply: Output | SessionFailed = None) -> None:
        self.reply: Output | SessionFailed = reply
        self.gate: asyncio.Event | None = None
        self.fed: list[StrategyEvent] = []
        self.closed = 0
        self.raises: Exception | None = None

    async def feed(self, event: StrategyEvent) -> Output | SessionFailed:
        self.fed.append(event)
        if self.gate is not None:
            await self.gate.wait()
        if self.raises is not None:
            raise self.raises
        return self.reply

    async def close(self) -> None:
        self.closed += 1


class FakeOpen:
    """연 일을 적고 FakeSession(또는 정해 둔 실패)을 준다."""

    def __init__(self, failed: SessionFailed | None = None) -> None:
        self.failed = failed
        self.gate: asyncio.Event | None = None
        self.raises: Exception | None = None
        self.jobs: list[SessionJob] = []
        self.sessions: list[FakeSession] = []

    async def __call__(self, job: SessionJob) -> Session | SessionFailed:
        self.jobs.append(job)
        if self.gate is not None:
            await self.gate.wait()
        if self.raises is not None:
            raise self.raises
        if self.failed is not None:
            return self.failed
        session = FakeSession()
        self.sessions.append(session)
        return session


async def no_runs(work: SandboxJob) -> SandboxResult:
    raise AssertionError("세션 테스트는 백테스트를 돌리지 않는다")


@asynccontextmanager
async def executor(
    opener: FakeOpen, clock: Now | None = None, sweep_seconds: float = 3600.0
) -> AsyncGenerator[httpx2.AsyncClient]:
    """실행기 앱(수명 주기 포함)과 그 앱에 붙은 HTTP 클라이언트."""
    app = make_app(TOKEN, no_runs, opener, clock or Now(), sweep_seconds)
    transport = httpx2.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(transport=transport, base_url="http://executor") as http,
    ):
        yield http


def run(scenario: Callable[[], Coroutine[object, object, None]]) -> None:
    async def bounded() -> None:
        # 회귀(상한·409가 빠져 붙잡힌 요청을 기다리는 것 등)가 멈춰 서지 않고 실패로 끝나게 한다.
        async with asyncio.timeout(10):
            await scenario()

    asyncio.run(bounded())


async def open_one(http: httpx2.AsyncClient, payload: Body | None = None) -> str:
    resp = await http.post("/sessions", json=payload or session_to_json(SESSION), headers=AUTH)
    assert resp.status_code == 200, resp.text
    session_id: str = resp.json()["session_id"]
    return session_id


async def post_event(http: httpx2.AsyncClient, session_id: str, wire: object) -> httpx2.Response:
    return await http.post(f"/sessions/{session_id}/events", json=wire, headers=AUTH)


async def feed(
    http: httpx2.AsyncClient, session_id: str, event: StrategyEvent = BAR
) -> httpx2.Response:
    return await post_event(http, session_id, event_to_wire(event))


def test_open_round_trips_the_job_and_gives_an_unguessable_id() -> None:
    opener = FakeOpen()
    with_model = replace(SESSION, model=b"\x00model\xff" * 1000)

    async def scenario() -> None:
        async with executor(opener) as http:
            first = await open_one(http, session_to_json(with_model))
            second = await open_one(http)
        assert opener.jobs == [with_model, SESSION]
        assert opener.jobs[0].model == with_model.model
        assert first != second
        assert all(re.fullmatch(r"[A-Za-z0-9_-]{43}", sid) for sid in (first, second))

    run(scenario)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/sessions"),
        ("POST", "/sessions/{id}/events"),
        ("DELETE", "/sessions/{id}"),
        ("GET", "/sessions/{id}"),
    ],
)
@pytest.mark.parametrize("token", [None, "", "wrong", "t" * 39])
def test_every_session_route_needs_the_token(method: str, path: str, token: str | None) -> None:
    opener = FakeOpen()

    async def scenario() -> None:
        async with executor(opener) as http:
            session_id = await open_one(http)
            headers = {} if token is None else {TOKEN_HEADER: token}
            payload = session_to_json(SESSION) if path == "/sessions" else event_to_wire(BAR)
            resp = await http.request(
                method,
                path.format(id=session_id),
                json=payload if method == "POST" else None,
                headers=headers,
            )
            assert resp.status_code == 401
            assert resp.json() == {"status": "unauthorized"}
            # 아무것도 하지 않았다. 세션은 그대로 있다.
            [session] = opener.sessions
            assert (len(opener.jobs), session.fed, session.closed) == (1, [], 0)
            state = await http.get(f"/sessions/{session_id}", headers=AUTH)
            assert state.json() == {"state": "idle"}

    run(scenario)


def bad_open_bodies() -> list[object]:
    good = session_to_json(SESSION)
    missing = [{k: v for k, v in good.items() if k != key} for key in good]
    return [
        *missing,
        {**good, "events": []},
        {**good, "image": "x"},
        {**good, "limits": {}},
        {**good, "expires_at": "2026-11-26T09:00:00"},  # 시간대 없음
        {**good, "expires_at": "2026-11-26T00:00:00+00:00"},  # 같은 시각이지만 KST가 아니다
        {**good, "expires_at": "2026-11-26T09:00:00.500000+09:00"},
        {**good, "expires_at": 1},
        {**good, "expires_at": None},
        {**good, "expires_at": "2026-11-25T09:00:00+09:00"},  # 지금(만료됨)
        {**good, "expires_at": "2026-11-25T08:59:59+09:00"},  # 지났다
        {**good, "code": 1},
        {**good, "max_position": -1},
        {**good, "max_position": True},
        {**good, "streams": {}},
        {**good, "terms": {}},
        {**good, "model": ""},
        {**good, "model": "!!!!"},
        [good],
        "text",
    ]


@pytest.mark.parametrize("payload", bad_open_bodies())
def test_bad_open_bodies_are_refused(payload: object) -> None:
    opener = FakeOpen()

    async def scenario() -> None:
        async with executor(opener) as http:
            resp = await http.post("/sessions", json=payload, headers=AUTH)
        assert resp.status_code == 400
        assert opener.jobs == []

    run(scenario)


def test_unparseable_open_body_is_refused() -> None:
    opener = FakeOpen()

    async def scenario() -> None:
        async with executor(opener) as http:
            resp = await http.post("/sessions", content=b"{not json", headers=AUTH)
        assert resp.status_code == 400
        assert opener.jobs == []

    run(scenario)


def test_expiry_is_capped_at_seven_days_from_now() -> None:
    opener = FakeOpen()
    far = replace(SESSION, expires_at=START + timedelta(days=30))
    near = replace(SESSION, expires_at=START + timedelta(days=6))

    async def scenario() -> None:
        async with executor(opener) as http:
            await open_one(http, session_to_json(far))
            await open_one(http, session_to_json(near))
        assert [j.expires_at for j in opener.jobs] == [START + MAX_LIFETIME, near.expires_at]
        assert timedelta(days=7) == MAX_LIFETIME

    run(scenario)


def test_open_body_caps_leave_room_only_for_the_model() -> None:
    opener = FakeOpen()
    padded = {**session_to_json(SESSION), "code": "x" * MAX_SESSION_JOB_BODY}
    largest = replace(SESSION, model=bytes(MAX_MODEL_BYTES))

    async def scenario() -> None:
        async with executor(opener) as http:
            assert (await http.post("/sessions", json=padded, headers=AUTH)).status_code == 413
            with_model = {**padded, "model": base64.b64encode(b"m" * 3000).decode()}
            assert (await http.post("/sessions", json=with_model, headers=AUTH)).status_code == 413
            assert opener.jobs == []
            await open_one(http, session_to_json(largest))
        assert opener.jobs[0].model == largest.model

    run(scenario)


def test_the_41st_open_is_busy_until_one_closes() -> None:
    opener = FakeOpen()

    async def scenario() -> None:
        async with executor(opener) as http:
            ids = [await open_one(http) for _ in range(MAX_SESSIONS)]
            busy = await http.post("/sessions", json=session_to_json(SESSION), headers=AUTH)
            assert (busy.status_code, busy.json()) == (503, {"status": "busy"})
            assert len(opener.jobs) == MAX_SESSIONS
            await http.delete(f"/sessions/{ids[0]}", headers=AUTH)
            await open_one(http)
        assert MAX_SESSIONS == 40

    run(scenario)


def test_opens_in_progress_count_toward_the_cap() -> None:
    opener, gate = FakeOpen(), asyncio.Event()
    opener.gate = gate

    async def scenario() -> None:
        async with executor(opener) as http:
            payload = session_to_json(SESSION)
            held = [
                asyncio.create_task(http.post("/sessions", json=payload, headers=AUTH))
                for _ in range(MAX_SESSIONS)
            ]
            while len(opener.jobs) < MAX_SESSIONS:
                await asyncio.sleep(0)
            busy = await http.post("/sessions", json=payload, headers=AUTH)
            assert busy.status_code == 503
            gate.set()
            assert [r.status_code for r in await asyncio.gather(*held)] == [200] * MAX_SESSIONS

    run(scenario)


FAILED = SessionFailed("invalid_code", "on_event 함수가 없습니다")


def test_failed_open_is_a_reply_and_takes_no_slot() -> None:
    opener = FakeOpen(FAILED)

    async def scenario() -> None:
        async with executor(opener) as http:
            for _ in range(MAX_SESSIONS + 1):
                resp = await http.post("/sessions", json=session_to_json(SESSION), headers=AUTH)
                assert resp.status_code == 200
                assert resp.json() == {
                    "status": "invalid_code",
                    "detail": "on_event 함수가 없습니다",
                }

    run(scenario)


def test_open_error_is_500_and_frees_the_slot() -> None:
    opener = FakeOpen()
    opener.raises = RuntimeError("docker 없음")

    async def scenario() -> None:
        async with executor(opener) as http:
            for _ in range(MAX_SESSIONS + 1):
                resp = await http.post("/sessions", json=session_to_json(SESSION), headers=AUTH)
                assert (resp.status_code, resp.json()) == (500, {"status": "error"})
                assert "docker" not in resp.text

    run(scenario)


def test_feed_passes_market_and_fill_events_and_returns_the_output() -> None:
    opener = FakeOpen()

    async def scenario() -> None:
        async with executor(opener) as http:
            session_id = await open_one(http)
            [session] = opener.sessions
            assert (await feed(http, session_id, BAR)).json() == {"status": "ok", "output": None}
            session.reply = {SYMBOL: Target(-1, Decimal("350.05")), "B": Target(0)}
            resp = await feed(http, session_id, FILL)
            assert resp.json() == {
                "status": "ok",
                "output": {
                    SYMBOL: {"position": -1, "price": "350.05"},
                    "B": {"position": 0, "price": None},
                },
            }
        assert session.fed == [BAR, FILL]

    run(scenario)


def bad_events() -> list[object]:
    bar, fill = event_to_wire(BAR), event_to_wire(FILL)
    tick: Body = {
        "type": "tick",
        "symbol": SYMBOL,
        "at": bar["at"],
        "price": "350",
        "qty": 1,
        "bid": None,
        "ask": None,
    }
    return [
        {**bar, "extra": 1},
        {**bar, "symbol": "B"},  # 선언하지 않은 종목
        {**bar, "interval": "day"},  # 선언하지 않은 흐름
        tick,  # 선언하지 않은 흐름
        {**bar, "close": 350.5},
        {**fill, "side": "hold"},
        {**fill, "qty": 0},
        {**fill, "qty": -1},
        {**fill, "qty": True},
        {**fill, "qty": "1"},
        {**fill, "price": "0"},
        {**fill, "price": "-350"},
        {**fill, "price": 350.05},
        {**fill, "price": "1e3"},
        {**fill, "symbol": "B"},
        {**fill, "at": "2026-11-25T09:00:00"},
        {**fill, "extra": 1},
        {k: v for k, v in fill.items() if k != "side"},
        {"event": bar},
        [bar],
        "bar",
        None,
    ]


@pytest.mark.parametrize("event", bad_events())
def test_bad_events_are_refused_and_the_session_lives(event: object) -> None:
    opener = FakeOpen()

    async def scenario() -> None:
        async with executor(opener) as http:
            session_id = await open_one(http)
            assert (await post_event(http, session_id, event)).status_code == 400
            [session] = opener.sessions
            assert session.fed == []
            assert (await feed(http, session_id, BAR)).status_code == 200

    run(scenario)


def test_feed_body_over_the_cap_is_refused() -> None:
    opener = FakeOpen()

    async def scenario() -> None:
        async with executor(opener) as http:
            session_id = await open_one(http)
            resp = await http.post(
                f"/sessions/{session_id}/events",
                content=b"x" * (MAX_EVENT_BODY + 1),
                headers=AUTH,
            )
            assert resp.status_code == 413
            assert opener.sessions[0].fed == []

    run(scenario)


def test_a_second_feed_while_one_is_in_flight_is_409() -> None:
    opener = FakeOpen()

    async def scenario() -> None:
        async with executor(opener) as http:
            session_id = await open_one(http)
            [session] = opener.sessions
            session.gate = asyncio.Event()
            first = asyncio.create_task(feed(http, session_id))
            while not session.fed:
                await asyncio.sleep(0)
            state = await http.get(f"/sessions/{session_id}", headers=AUTH)
            assert state.json() == {"state": "feeding"}
            second = await feed(http, session_id)
            assert (second.status_code, second.json()) == (409, {"status": "busy"})
            session.gate.set()
            assert (await first).status_code == 200
            assert (await feed(http, session_id)).status_code == 200
            assert len(session.fed) == 2

    run(scenario)


def test_a_failed_feed_ends_the_session() -> None:
    opener = FakeOpen()

    async def scenario() -> None:
        async with executor(opener) as http:
            session_id = await open_one(http)
            [session] = opener.sessions
            session.reply = SessionFailed("timeout", "2초 안에 답하지 않았습니다")
            resp = await feed(http, session_id)
            assert resp.json() == {"status": "timeout", "detail": "2초 안에 답하지 않았습니다"}
            assert session.closed == 1
            assert (await feed(http, session_id)).status_code == 404
            assert (await http.get(f"/sessions/{session_id}", headers=AUTH)).status_code == 404
            assert len(session.fed) == 1

    run(scenario)


def test_a_feed_error_is_500_and_ends_the_session() -> None:
    opener = FakeOpen()

    async def scenario() -> None:
        async with executor(opener) as http:
            session_id = await open_one(http)
            [session] = opener.sessions
            session.raises = RuntimeError("docker inspect 실패")
            resp = await feed(http, session_id)
            assert (resp.status_code, resp.json()) == (500, {"status": "error"})
            assert session.closed == 1
            assert (await feed(http, session_id)).status_code == 404

    run(scenario)


def test_delete_closes_and_then_everything_is_gone() -> None:
    opener = FakeOpen()

    async def scenario() -> None:
        async with executor(opener) as http:
            session_id = await open_one(http)
            [session] = opener.sessions
            resp = await http.delete(f"/sessions/{session_id}", headers=AUTH)
            assert (resp.status_code, resp.json()) == (200, {"status": "closed"})
            assert session.closed == 1
            gone = [
                await feed(http, session_id),
                await http.delete(f"/sessions/{session_id}", headers=AUTH),
                await http.get(f"/sessions/{session_id}", headers=AUTH),
            ]
            assert [(r.status_code, r.json()) for r in gone] == [(404, {"status": "gone"})] * 3
            assert (session.fed, session.closed) == ([], 1)

    run(scenario)


@pytest.mark.parametrize("closed_raises", [False, True], ids=["returns", "raises-closed"])
def test_delete_while_feeding_makes_that_feed_gone(closed_raises: bool) -> None:
    opener = FakeOpen()

    async def scenario() -> None:
        async with executor(opener) as http:
            session_id = await open_one(http)
            [session] = opener.sessions
            session.gate = asyncio.Event()
            session.reply = {SYMBOL: Target(1)}
            if closed_raises:
                # 실제 세션은 넣는 동안 닫히면 SessionClosedError를 낸다.
                session.raises = SessionClosedError()
            feeding = asyncio.create_task(feed(http, session_id))
            while not session.fed:
                await asyncio.sleep(0)
            assert (await http.delete(f"/sessions/{session_id}", headers=AUTH)).status_code == 200
            session.gate.set()
            resp = await feeding
            assert (resp.status_code, resp.json()) == (404, {"status": "gone"})
            assert session.closed == 1

    run(scenario)


@pytest.mark.parametrize("method", ["POST", "DELETE", "GET"])
def test_unknown_session_is_404(method: str) -> None:
    opener = FakeOpen()

    async def scenario() -> None:
        async with executor(opener) as http:
            await open_one(http)
            path = "/sessions/" + "a" * 43 + ("/events" if method == "POST" else "")
            body = event_to_wire(BAR) if method == "POST" else None
            resp = await http.request(method, path, json=body, headers=AUTH)
            assert resp.status_code == 404
            assert opener.sessions[0].fed == []

    run(scenario)


def test_feed_after_expiry_is_gone_and_closes_the_session() -> None:
    opener, clock = FakeOpen(), Now()

    async def scenario() -> None:
        async with executor(opener, clock) as http:
            session_id = await open_one(http)
            [session] = opener.sessions
            clock.at = SESSION.expires_at - timedelta(seconds=1)
            assert (await feed(http, session_id)).status_code == 200
            clock.at = SESSION.expires_at
            assert (await http.get(f"/sessions/{session_id}", headers=AUTH)).status_code == 404
            assert (await feed(http, session_id)).status_code == 404
            assert (len(session.fed), session.closed) == (1, 1)

    run(scenario)


def test_sweep_closes_expired_sessions_in_the_background() -> None:
    opener, clock = FakeOpen(), Now()
    later = replace(SESSION, expires_at=START + timedelta(days=2))

    async def scenario() -> None:
        async with executor(opener, clock, sweep_seconds=0.01) as http:
            soon_id = await open_one(http)
            later_id = await open_one(http, session_to_json(later))
            soon, kept = opener.sessions
            clock.at = SESSION.expires_at + timedelta(seconds=1)
            for _ in range(500):
                if soon.closed:
                    break
                await asyncio.sleep(0.01)
            assert (soon.closed, kept.closed) == (1, 0)
            assert (await http.get(f"/sessions/{soon_id}", headers=AUTH)).status_code == 404
            assert (await http.get(f"/sessions/{later_id}", headers=AUTH)).status_code == 200

    run(scenario)


def test_shutdown_closes_every_session() -> None:
    opener = FakeOpen()

    async def scenario() -> None:
        async with executor(opener) as http:
            for _ in range(3):
                await open_one(http)
            assert [s.closed for s in opener.sessions] == [0, 0, 0]
        assert [s.closed for s in opener.sessions] == [1, 1, 1]

    run(scenario)
