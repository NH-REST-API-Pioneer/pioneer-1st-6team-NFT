"""여러 테스트가 같이 쓰는 가짜: Google, 시계, 웹 로그인과 키 화면이 붙은 서버."""

import asyncio
import gzip
import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlparse

import httpx2
from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

from nh_mcp.auth.access import AccessPolicy, make_gate
from nh_mcp.auth.web import CALLBACK_PATH, LOGIN_PATH, TOKEN_URL, TOKENINFO_URL
from nh_mcp.config import Settings
from nh_mcp.domain import (
    KST,
    BarEvent,
    MarketEvent,
    Output,
    SandboxJob,
    SymbolTerms,
    Target,
    UserId,
)
from nh_mcp.keys.service import Verify
from nh_mcp.keys.vault import Vault
from nh_mcp.nh.client import Credentials
from nh_mcp.realtime.session import Connect, Socket
from nh_mcp.sandbox.wire import job_from_json, output_to_wire
from nh_mcp.server import Wiring, attach, build_server
from nh_mcp.services.runs import Standing
from nh_mcp.store import db, features
from nh_mcp.strategies.backtest import Simulator, decision_time

CLIENT_ID = "fake-client-id"
ADMIN = "admin@example.com"
T0 = datetime(2026, 12, 1, 1, 0, tzinfo=UTC)

SETTINGS = Settings(
    public_base_url="http://localhost:8000",
    google_client_id=CLIENT_ID,
    google_client_secret="fake-google-secret-0000",
    admin_emails=frozenset({ADMIN}),
    open_signup=False,
    db_path=Path("unused.db"),
    key_encryption_key=bytes(32),
    sandbox_url="http://executor.test",
    sandbox_token="fake-sandbox-token-" + "0" * 16,
)


def account(sub: str, email: str, verified: str = "true", aud: str = CLIENT_ID) -> dict[str, str]:
    return {"aud": aud, "sub": sub, "email": email, "email_verified": verified}


# Google 흉내: 인가 코드마다 돌려줄 tokeninfo.
GOOGLE_ACCOUNTS = {
    "admin-code": account("g-admin", ADMIN),
    "outsider-code": account("g-out", "outsider@example.com"),
    "unverified-code": account("g-unv", ADMIN, verified="false"),
    "other-app-code": account("g-admin", ADMIN, aud="someone-else"),
}


class FakeGoogle:
    def __init__(self) -> None:
        self.token_forms: list[dict[str, list[str]]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        if str(request.url) == TOKEN_URL:
            form = parse_qs(request.content.decode())
            self.token_forms.append(form)
            code = form["code"][0]
            if code not in GOOGLE_ACCOUNTS:
                return httpx2.Response(400, content=b'{"error": "invalid_grant"}')
            body = {"access_token": f"at-{code}"}
            return httpx2.Response(200, content=json.dumps(body).encode())
        if str(request.url) == TOKENINFO_URL:
            code = request.headers["authorization"].removeprefix("Bearer at-")
            return httpx2.Response(200, content=json.dumps(GOOGLE_ACCOUNTS[code]).encode())
        return httpx2.Response(404)


MASTER_SAMPLES = Path(__file__).parent / "fixtures" / "master"


class FakeMaster:
    """개발자센터 흉내. 실제 종목 마스터에서 뽑은 견본을 gzip으로 돌려준다."""

    def __init__(self) -> None:
        self.requested: list[str] = []
        self.down = False

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requested.append(str(request.url))
        if self.down:
            raise httpx2.ConnectError("개발자센터에 닿지 못함", request=request)
        name = request.url.path.rsplit("/", 1)[-1].removesuffix(".mst.gz")
        text = (MASTER_SAMPLES / f"{name}.mst").read_text(encoding="utf-8")
        return httpx2.Response(200, content=gzip.compress(text.encode()))


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


async def accept_any_key(_: Credentials) -> None:
    return None


def mcp_token(sub: str, email: str) -> dict[str, object]:
    return {"client_id": "test", "scopes": [], "sub": sub, "email": email, "email_verified": True}


# MCP 요청에 쓰는 토큰. 테스트만 미리 정한 토큰으로 사용자를 흉내 낸다.
MCP_TOKENS = {
    "admin-token": mcp_token("g-admin", ADMIN),
    "second-admin-token": mcp_token("g-admin2", "second@example.com"),
    # 로그인은 됐지만 허용 목록에 없는 사용자. 게이트가 막아야 한다.
    "outsider-token": mcp_token("g-out", "outsider@example.com"),
}


def scripted_outputs(
    events: Sequence[MarketEvent], targets: Sequence[Output], terms: Mapping[str, SymbolTerms]
) -> list[Output]:
    """시장 이벤트마다 정해 둔 출력을 내고 체결 이벤트에는 None을 내는 전략의 출력 목록.

    실행기의 대화처럼 체결 계산을 돌려, 체결이 난 자리에 그 체결 이벤트의 출력(None)을 끼운다.
    """
    sim = Simulator(terms)
    outputs: list[Output] = []
    for event, target in zip(events, targets, strict=True):
        outputs.extend(None for _ in sim.on_market(event))
        sim.on_output(target, decision_time(event))
        outputs.append(target)
    return outputs


def signal_targets(symbol: str, signals: Sequence[str], quantity: int) -> list[Output]:
    """M3 신호를 목표로: buy=quantity 매수, sell=quantity 매도, exit=정리, none=그대로."""
    positions = {"buy": quantity, "sell": -quantity, "exit": 0}
    return [None if s == "none" else {symbol: Target(positions[s])} for s in signals]


type Answer = Mapping[str, object] | int | Reply


type Reply = Callable[[dict[str, object]], Mapping[str, object]]


def answering(pick: Callable[[SandboxJob], Sequence[Output]]) -> Reply:
    """시장 이벤트마다 pick이 정한 목표를 내고 체결 이벤트에는 None을 내는 전략의 성공 답."""

    def answer(body: dict[str, object]) -> Mapping[str, object]:
        job = job_from_json(body)
        assert job is not None, body
        outputs = scripted_outputs(job.events, pick(job), job.terms)
        return {
            "status": "ok",
            "outputs": [output_to_wire(o) for o in outputs],
            "index": None,
            "detail": "",
        }

    return answer


def scripted(signals: Sequence[str]) -> Reply:
    """한 종목 봉마다 신호대로 목표를 내는 전략의 성공 답. 수량은 본문의 max_position이다."""

    def pick(job: SandboxJob) -> list[Output]:
        [symbol] = job.streams
        return signal_targets(symbol, signals, job.max_position)

    return answering(pick)


def by_bar(targets: Mapping[tuple[str, str, str], Output]) -> Reply:
    """(종목, 간격, 봉 시작 시각 ISO)마다 정해 둔 목표를 내는 전략의 성공 답. 없는 봉에는 None."""

    def pick(job: SandboxJob) -> list[Output]:
        out: list[Output] = []
        for event in job.events:
            assert isinstance(event, BarEvent), event
            at = event.bar.at.astimezone(KST).isoformat(timespec="seconds")
            out.append(targets.get((event.symbol, event.interval, at)))
        return out

    return answering(pick)


class FakeSandbox:
    """격리 실행기 흉내. 정해 둔 답을 차례로 돌려주고 마지막 것은 되풀이한다.

    답은 JSON, 상태 코드, 또는 받은 본문으로 JSON을 만드는 함수(`scripted`)다. 정해 둔 답이
    없으면 이벤트마다 None을 돌려준다. 받은 본문은 `jobs`에 남는다.
    `hold`를 걸어 두면 그 이벤트가 켜질 때까지 답하지 않는다(취소·동시 실행 테스트).
    """

    def __init__(self) -> None:
        self.jobs: list[dict[str, object]] = []
        self.replies: list[Answer] = []
        self.down = False
        self.hold: asyncio.Event | None = None

    def reply(self, answer: Answer) -> None:
        self.replies.append(answer)

    async def __call__(self, request: httpx2.Request) -> httpx2.Response:
        job: dict[str, object] = json.loads(request.content)
        self.jobs.append(job)
        if self.hold is not None:
            await self.hold.wait()
        if self.down:
            raise httpx2.ConnectError("실행기에 닿지 못함", request=request)
        if self.replies:
            answer = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        else:
            events = job["events"]
            count = len(cast(list[object], events)) if isinstance(events, list) else 0
            answer = {"status": "ok", "outputs": [None] * count, "index": None, "detail": ""}
        if isinstance(answer, int):
            return httpx2.Response(answer)
        if callable(answer):
            answer = answer(job)
        return httpx2.Response(200, content=json.dumps(dict(answer)).encode())


def no_stream(_url: str) -> AbstractAsyncContextManager[Socket]:
    """실시간 연결을 쓰지 않는 테스트의 연결 함수. 실제 NH로 나가지 않고 실패한다."""
    raise ConnectionRefusedError("이 테스트는 NH 실시간 연결을 쓰지 않습니다.")


class World:
    """가짜 Google·NH에 붙은 서버 하나: MCP 도구, 웹 로그인, 키 화면.

    시세 수신기(`recorder`)는 만들기만 한다. 틱 저장을 보는 테스트가 `data_dir`(ticks.db 자리)을
    주고, `stream`을 가짜 실시간 서버의 연결 함수로 바꾼 뒤 `recorder.running()`으로 켠다.
    """

    def __init__(
        self,
        verify: Verify = accept_any_key,
        nh: httpx2.AsyncBaseTransport | None = None,
        orders_enabled: bool = False,
        sandbox: FakeSandbox | None = None,
        data_dir: Path | None = None,
    ) -> None:
        self.master = FakeMaster()
        self.sandbox = sandbox or FakeSandbox()
        self.stream: Connect = no_stream
        self.conn = db.connect(":memory:")
        self.google = FakeGoogle()
        self.clock = Clock()
        self.vault = Vault(SETTINGS.key_encryption_key)
        admins = frozenset({ADMIN, "second@example.com"})
        gate = make_gate(AccessPolicy(False, admins), self.conn, self.clock)
        self.mcp: FastMCP = build_server(StaticTokenVerifier(MCP_TOKENS), gate, clock=self.clock)
        # 모델 파일은 DB 경로 옆 models/에 쌓인다. 모델을 올리는 테스트만 tmp 폴더를 준다. 안 주면
        # 만들 수 없는 경로라, 실수로 올려도 저장소 안에 쓰지 않고 실패한다.
        db_path = (data_dir or Path("/nonexistent")) / "unused.db"
        wiring = Wiring(
            replace(SETTINGS, orders_enabled=orders_enabled, db_path=db_path),
            self.conn,
            self.clock,
            verify,
            google=httpx2.MockTransport(self.google),
            nh=nh,
            master=httpx2.MockTransport(self.master),
            sandbox=httpx2.MockTransport(self.sandbox),
            stream=self._connect,
        )
        parts = attach(self.mcp, wiring)
        self.login, self.store, self.pool = parts.login, parts.store, parts.pool
        self.models = parts.models
        self.recorder = parts.recorder
        self.runner = parts.runner
        self.strategies = parts.strategies

    def _connect(self, url: str) -> AbstractAsyncContextManager[Socket]:
        # 서버를 만든 뒤에 바꾼 stream도 쓰이게 부를 때 찾는다.
        return self.stream(url)

    def user_count(self) -> int:
        row: tuple[int] = self.conn.execute("SELECT COUNT(*) FROM users").fetchone()
        return row[0]


async def browser_login(
    http: httpx2.AsyncClient, base: str, code: str, state: str | None = None
) -> tuple[httpx2.Response, httpx2.Response]:
    """로그인을 시작하고 Google에서 돌아온 것처럼 callback을 부른다. (시작 응답, callback 응답)"""
    started = await http.get(base + LOGIN_PATH)
    real_state = parse_qs(urlparse(started.headers["location"]).query)["state"][0]
    params = {"code": code, "state": state if state is not None else real_state}
    return started, await http.get(base + CALLBACK_PATH, params=params)


FAKE_NH_TOKEN = "fake-nh-token-0000"


class FakeNH:
    """NH 모의 서버 흉내. 경로마다 정해 둔 응답을 차례로 돌려주고, 마지막 것은 되풀이한다."""

    def __init__(self) -> None:
        # 본문이 bytes면 그대로(빈 본문 등), 아니면 JSON으로 보낸다.
        self.replies: dict[str, list[tuple[int, Mapping[str, object] | bytes]]] = {}
        # (경로, 요청의 sym) → 그 종목에만 주는 답. 없으면 replies의 답이다.
        self.by_symbol: dict[tuple[str, str], Mapping[str, object]] = {}
        self.requests: list[tuple[str, dict[str, object]]] = []
        self.down = False
        # 이 경로들만 응답 없이 끊는다(요청은 기록된다).
        self.down_paths: set[str] = set()

    def reply(self, path: str, body: Mapping[str, object] | bytes, status: int = 200) -> None:
        self.replies.setdefault(path, []).append((status, body))

    def reply_for(self, path: str, sym: str, body: Mapping[str, object]) -> None:
        """이 경로에 이 종목(sym)으로 온 요청에만 늘 같은 답을 준다."""
        self.by_symbol[(path, sym)] = body

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/auth-service/v1/token":
            token = {"access_token": FAKE_NH_TOKEN, "expires_in": "86400"}
            return httpx2.Response(200, content=json.dumps(token).encode())
        payload: dict[str, object] = json.loads(request.content or b"{}")
        self.requests.append((request.url.path, payload))
        if self.down or request.url.path in self.down_paths:
            raise httpx2.ReadTimeout("시간 초과", request=request)
        only = self.by_symbol.get((request.url.path, str(payload.get("sym", ""))))
        if only is not None:
            return httpx2.Response(200, content=json.dumps(dict(only), ensure_ascii=False).encode())
        queue = self.replies.get(request.url.path, [(200, {})])
        status, body = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(body, bytes):
            return httpx2.Response(status, content=body)
        return httpx2.Response(status, content=json.dumps(dict(body), ensure_ascii=False).encode())

    def paths(self) -> list[str]:
        return [path for path, _ in self.requests]


class NoRuns:
    """켜지 않는 자동 실행기 자리(`StrategyRuns`). 미리보기만 보는 테스트가 쓴다."""

    async def start(self, user_id: UserId, approval: str, code: str, model: bytes | None) -> str:
        raise AssertionError("이 테스트는 전략을 켜지 않습니다.")

    async def stop(self, user_id: UserId, run_id: str) -> bool:
        return False

    def standing(self, user_id: UserId, run_id: str) -> Standing | None:
        return None


def auto_run_on(conn: sqlite3.Connection, *users: UserId) -> None:
    """사용자가 등록 화면에서 "자동 실행"을 켠 것처럼."""
    for user in users:
        features.turn_on(conn, user, "auto_run", T0)
