import asyncio

import httpx2
import pytest
from conftest import Serve
from fakes import World, browser_login

from nh_mcp.auth.web import LOGIN_PATH, SESSION_COOKIE
from nh_mcp.domain import UserId
from nh_mcp.keys.page import DELETE_PATH, REGISTER_PATH
from nh_mcp.keys.service import Verify
from nh_mcp.keys.vault import Slot
from nh_mcp.nh.client import Credentials, NoResponse, Rejected
from nh_mcp.store import nh_keys

FAKE_KEY = "fake-app-key-1234"
FAKE_SECRET = "fake-app-secret-0000"


async def rejects(_: Credentials) -> None:
    raise Rejected("EAU025", "잘못된 appkey", 400)


async def no_answer(_: Credentials) -> None:
    raise NoResponse("응답 없음")


class Session:
    """로그인한 브라우저 하나로 화면을 쓰고 응답을 모은다."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.responses: dict[str, httpx2.Response] = {}

    async def run(self, serve: Serve, steps: list[tuple[str, str, dict[str, str]]]) -> None:
        async with serve(self.world.mcp) as url, httpx2.AsyncClient() as http:
            base = url.removesuffix("/mcp")
            await browser_login(http, base, "admin-code")
            user = self.world.login.user_from_cookie(http.cookies.get(SESSION_COOKIE) or "")
            assert user is not None
            for name, path, form in steps:
                if form:
                    data = {"csrf": user.csrf, **form}
                    self.responses[name] = await http.post(base + path, data=data)
                else:
                    self.responses[name] = await http.get(base + path)

    def user_id(self) -> int:
        row: tuple[int] = self.world.conn.execute("SELECT id FROM users").fetchone()
        return row[0]


def register_form(market: str = "domestic") -> dict[str, str]:
    return {"market": market, "app_key": FAKE_KEY, "app_secret": FAKE_SECRET}


def test_page_needs_login(serve: Serve) -> None:
    world = World()

    async def scenario() -> httpx2.Response:
        async with serve(world.mcp) as url, httpx2.AsyncClient() as http:
            return await http.get(url.removesuffix("/mcp") + "/keys")

    response = asyncio.run(scenario())
    assert response.status_code == 303
    assert response.headers["location"] == LOGIN_PATH


def test_register_shows_only_last4_and_stores_encrypted(serve: Serve) -> None:
    session = Session(World())
    asyncio.run(
        session.run(
            serve,
            [
                ("register", REGISTER_PATH, register_form()),
                ("page", "/keys?result=registered", {}),
            ],
        )
    )

    assert session.responses["register"].status_code == 303
    assert session.responses["register"].headers["location"] == "/keys?result=registered"
    page = session.responses["page"]
    assert page.status_code == 200
    assert "키를 등록했습니다." in page.text and "····1234" in page.text
    assert FAKE_KEY not in page.text and FAKE_SECRET not in page.text
    assert page.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]

    user_id = UserId(session.user_id())
    saved = nh_keys.load(session.world.conn, user_id, "domestic")
    assert saved is not None
    slot = Slot(user_id, "domestic", "app_key")
    assert session.world.vault.unseal(saved.sealed_app_key, slot) == FAKE_KEY


@pytest.mark.parametrize(("verify", "reason"), [(rejects, "rejected"), (no_answer, "unverified")])
def test_key_failing_the_demo_check_is_not_saved(serve: Serve, verify: Verify, reason: str) -> None:
    session = Session(World(verify=verify))
    asyncio.run(session.run(serve, [("register", REGISTER_PATH, register_form())]))

    assert session.responses["register"].headers["location"] == f"/keys?result={reason}"
    assert session.world.conn.execute("SELECT COUNT(*) FROM nh_keys").fetchone()[0] == 0


def test_forged_csrf_is_refused(serve: Serve) -> None:
    world = World()

    async def scenario() -> int:
        async with serve(world.mcp) as url, httpx2.AsyncClient() as http:
            base = url.removesuffix("/mcp")
            await browser_login(http, base, "admin-code")
            data = {"csrf": "forged", **register_form()}
            return (await http.post(base + REGISTER_PATH, data=data)).status_code

    assert asyncio.run(scenario()) == 403
    assert world.conn.execute("SELECT COUNT(*) FROM nh_keys").fetchone()[0] == 0


def test_unknown_market_is_refused(serve: Serve) -> None:
    session = Session(World())
    asyncio.run(session.run(serve, [("register", REGISTER_PATH, register_form("real"))]))

    assert session.responses["register"].status_code == 400
    assert session.world.conn.execute("SELECT COUNT(*) FROM nh_keys").fetchone()[0] == 0


def test_delete_removes_the_keys(serve: Serve) -> None:
    session = Session(World())
    asyncio.run(
        session.run(
            serve,
            [
                ("register", REGISTER_PATH, register_form()),
                ("delete", DELETE_PATH, {"confirm": "yes"}),
                ("page", "/keys?result=deleted", {}),
            ],
        )
    )

    assert session.responses["delete"].headers["location"] == "/keys?result=deleted"
    assert "등록된 키가 없습니다." in session.responses["page"].text
    assert session.world.conn.execute("SELECT COUNT(*) FROM nh_keys").fetchone()[0] == 0
