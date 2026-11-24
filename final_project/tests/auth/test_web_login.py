import asyncio
import base64
import hashlib
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
from conftest import Serve
from fakes import ADMIN, CLIENT_ID, T0, World, browser_login

from nh_mcp.auth.web import (
    AUTHORIZE_URL,
    CALLBACK_PATH,
    LOGIN_COOKIE,
    LOGIN_PATH,
    LOGOUT_PATH,
    SESSION_COOKIE,
)
from nh_mcp.store import users


class Visit:
    """브라우저 한 번의 방문 결과."""

    def __init__(self) -> None:
        self.started: httpx2.Response | None = None
        self.callback: httpx2.Response | None = None
        self.cookies: dict[str, str] = {}


async def visit(serve: Serve, world: World, code: str | None, state: str | None = None) -> Visit:
    result = Visit()
    async with serve(world.mcp) as url, httpx2.AsyncClient() as http:
        base = url.removesuffix("/mcp")
        if code is None:
            result.started = await http.get(base + LOGIN_PATH)
        else:
            result.started, result.callback = await browser_login(http, base, code, state)
        result.cookies = dict(http.cookies.items())
    return result


def location_query(response: httpx2.Response | None) -> dict[str, list[str]]:
    assert response is not None
    return parse_qs(urlparse(response.headers["location"]).query)


def test_login_redirects_to_google_with_pkce(serve: Serve) -> None:
    result = asyncio.run(visit(serve, World(), code=None))

    assert result.started is not None and result.started.status_code == 302
    assert result.started.headers["location"].startswith(AUTHORIZE_URL + "?")
    query = location_query(result.started)
    assert query["client_id"] == [CLIENT_ID]
    assert query["redirect_uri"] == ["http://localhost:8000" + CALLBACK_PATH]
    assert query["response_type"] == ["code"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"][0]
    set_cookie = result.started.headers["set-cookie"].lower()
    assert LOGIN_COOKIE in set_cookie and "httponly" in set_cookie and "samesite=lax" in set_cookie


def test_admin_login_gives_a_session(serve: Serve) -> None:
    world = World()
    result = asyncio.run(visit(serve, world, "admin-code"))

    assert result.callback is not None and result.callback.status_code == 303
    assert result.callback.headers["location"] == "/keys"
    user = world.login.user_from_cookie(result.cookies[SESSION_COOKIE])
    assert user is not None and user.email == ADMIN
    # Google에 보낸 code_verifier가 처음 보낸 challenge와 맞다(PKCE).
    verifier = world.google.token_forms[0]["code_verifier"][0]
    digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode()
    assert digest.rstrip("=") == location_query(result.started)["code_challenge"][0]
    assert world.user_count() == 1


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("outsider-code", 403),
        ("unverified-code", 403),
        # 다른 앱이 받은 토큰은 계정 거부가 아니라 확인 실패로 본다.
        ("other-app-code", 502),
        ("unknown-code", 502),
    ],
)
def test_not_allowed_or_unverifiable_login_gets_no_session(
    serve: Serve, code: str, status: int
) -> None:
    world = World()
    result = asyncio.run(visit(serve, world, code))

    assert result.callback is not None and result.callback.status_code == status
    assert SESSION_COOKIE not in result.cookies
    assert world.user_count() == 0


def test_wrong_state_is_refused(serve: Serve) -> None:
    world = World()
    result = asyncio.run(visit(serve, world, "admin-code", state="forged-state"))

    assert result.callback is not None and result.callback.status_code == 400
    assert SESSION_COOKIE not in result.cookies
    assert world.google.token_forms == []


def test_session_expires_and_rejects_tampering_and_suspension(serve: Serve) -> None:
    world = World()
    session = asyncio.run(visit(serve, world, "admin-code")).cookies[SESSION_COOKIE]
    user = world.login.user_from_cookie(session)
    assert user is not None

    tampered = session[:-2] + ("A" if session[-2] != "A" else "B") + session[-1]
    assert world.login.user_from_cookie(tampered) is None

    users.set_suspended(world.conn, user.user_id, True)
    assert world.login.user_from_cookie(session) is None
    users.set_suspended(world.conn, user.user_id, False)

    world.clock.now = T0 + timedelta(minutes=31)
    assert world.login.user_from_cookie(session) is None


def test_login_cookie_cannot_stand_in_for_a_session(serve: Serve) -> None:
    world = World()
    pending = asyncio.run(visit(serve, world, code=None)).cookies[LOGIN_COOKIE]
    assert world.login.user_from_cookie(pending) is None


def test_logout_needs_the_session_csrf(serve: Serve) -> None:
    world = World()

    async def scenario() -> tuple[int, int, bool]:
        async with serve(world.mcp) as url, httpx2.AsyncClient() as http:
            base = url.removesuffix("/mcp")
            await browser_login(http, base, "admin-code")
            user = world.login.user_from_cookie(http.cookies.get(SESSION_COOKIE) or "")
            assert user is not None
            forged = await http.post(base + LOGOUT_PATH, data={"csrf": "forged"})
            real = await http.post(base + LOGOUT_PATH, data={"csrf": user.csrf})
            return forged.status_code, real.status_code, SESSION_COOKIE in http.cookies

    assert asyncio.run(scenario()) == (400, 303, False)
