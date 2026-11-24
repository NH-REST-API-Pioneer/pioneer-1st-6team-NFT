"""여러 테스트가 같이 쓰는 가짜: Google, 시계, 웹 로그인과 키 화면이 붙은 서버."""

import gzip
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx2
from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

from nh_mcp.auth.access import AccessPolicy, make_gate
from nh_mcp.auth.web import CALLBACK_PATH, LOGIN_PATH, TOKEN_URL, TOKENINFO_URL
from nh_mcp.config import Settings
from nh_mcp.keys.service import Verify
from nh_mcp.keys.vault import Vault
from nh_mcp.nh.client import Credentials
from nh_mcp.server import Wiring, attach, build_server
from nh_mcp.store import db

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
}


class World:
    """가짜 Google·NH에 붙은 서버 하나: MCP 도구, 웹 로그인, 키 화면."""

    def __init__(
        self, verify: Verify = accept_any_key, nh: httpx2.AsyncBaseTransport | None = None
    ) -> None:
        self.master = FakeMaster()
        self.conn = db.connect(":memory:")
        self.google = FakeGoogle()
        self.clock = Clock()
        self.vault = Vault(SETTINGS.key_encryption_key)
        admins = frozenset({ADMIN, "second@example.com"})
        gate = make_gate(AccessPolicy(False, admins), self.conn, self.clock)
        self.mcp: FastMCP = build_server(StaticTokenVerifier(MCP_TOKENS), gate, clock=self.clock)
        wiring = Wiring(
            SETTINGS,
            self.conn,
            self.clock,
            verify,
            google=httpx2.MockTransport(self.google),
            nh=nh,
            master=httpx2.MockTransport(self.master),
        )
        parts = attach(self.mcp, wiring)
        self.login, self.store, self.pool = parts.login, parts.store, parts.pool

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
        self.replies: dict[str, list[tuple[int, Mapping[str, object]]]] = {}
        self.requests: list[tuple[str, dict[str, object]]] = []
        self.down = False

    def reply(self, path: str, body: Mapping[str, object], status: int = 200) -> None:
        self.replies.setdefault(path, []).append((status, body))

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/auth-service/v1/token":
            token = {"access_token": FAKE_NH_TOKEN, "expires_in": "86400"}
            return httpx2.Response(200, content=json.dumps(token).encode())
        payload: dict[str, object] = json.loads(request.content or b"{}")
        self.requests.append((request.url.path, payload))
        if self.down:
            raise httpx2.ReadTimeout("시간 초과", request=request)
        queue = self.replies.get(request.url.path, [(200, {})])
        status, body = queue.pop(0) if len(queue) > 1 else queue[0]
        return httpx2.Response(status, content=json.dumps(dict(body), ensure_ascii=False).encode())

    def paths(self) -> list[str]:
        return [path for path, _ in self.requests]
