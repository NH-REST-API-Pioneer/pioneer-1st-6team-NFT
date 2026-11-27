"""등록 화면의 "옵션"·"옵션 매도" 스위치: 로그인·CSRF·POST로만 바뀌고, "옵션"을 켜면 MCP 도구
목록에 get_option_chain이 나타나고, "옵션 매도"는 "옵션"이 켜져 있어야 켜지고, "옵션"을 끄면 같이
꺼진다. 주문 스위치가 꺼진 서버에도 있다(옵션 조회는 기본 서버에 있다)."""

import asyncio

import httpx2
from conftest import Serve
from fakes import ADMIN, T0, World, browser_login
from fastmcp import Client

from nh_mcp.auth.web import SESSION_COOKIE
from nh_mcp.keys.page import OPTION_SELL_PATH, OPTIONS_PATH
from nh_mcp.store import features, users


async def tool_names(url: str) -> set[str]:
    async with Client(url, auth="admin-token") as client:
        return {t.name for t in await client.list_tools()}


def test_selling_needs_options_on_and_turning_options_off_clears_it(serve: Serve) -> None:
    world = World()
    user = users.get_or_create(world.conn, "g-admin", ADMIN, T0)
    steps = [
        (OPTION_SELL_PATH, "on"),
        (OPTIONS_PATH, "on"),
        (OPTION_SELL_PATH, "on"),
        (OPTIONS_PATH, "off"),
    ]
    seen: list[tuple[str, tuple[bool, bool], bool]] = []
    pages: list[str] = []

    async def scenario() -> None:
        async with serve(world.mcp) as url, httpx2.AsyncClient() as http:
            base = url.removesuffix("/mcp")
            await browser_login(http, base, "admin-code")
            web = world.login.user_from_cookie(http.cookies.get(SESSION_COOKIE) or "")
            assert web is not None
            pages.append((await http.get(base + "/keys")).text)
            for path, wanted in steps:
                moved = await http.post(base + path, data={"csrf": web.csrf, "enabled": wanted})
                location = moved.headers["location"]
                pages.append((await http.get(base + location)).text)
                state = (
                    features.enabled(world.conn, user, "options"),
                    features.enabled(world.conn, user, "option_sell"),
                )
                seen.append((location, state, "get_option_chain" in await tool_names(url)))

    asyncio.run(scenario())

    assert OPTIONS_PATH in pages[0] and OPTION_SELL_PATH in pages[0]
    assert seen == [
        ("/keys?result=option_sell_needs_options", (False, False), False),
        ("/keys?result=options_on", (True, False), True),
        ("/keys?result=option_sell_on", (True, True), True),
        ("/keys?result=options_off", (False, False), False),
    ]
    assert "먼저 켜야 켤 수 있습니다" in pages[1]
    assert "옵션 매도도 함께 꺼졌습니다" in pages[4]


def test_turning_selling_off_leaves_options_on(serve: Serve) -> None:
    world = World()
    user = users.get_or_create(world.conn, "g-admin", ADMIN, T0)
    features.turn_on(world.conn, user, "options", T0)
    features.turn_on(world.conn, user, "option_sell", T0)

    async def scenario() -> str:
        async with serve(world.mcp) as url, httpx2.AsyncClient() as http:
            base = url.removesuffix("/mcp")
            await browser_login(http, base, "admin-code")
            web = world.login.user_from_cookie(http.cookies.get(SESSION_COOKIE) or "")
            assert web is not None
            form = {"csrf": web.csrf, "enabled": "off"}
            return (await http.post(base + OPTION_SELL_PATH, data=form)).headers["location"]

    assert asyncio.run(scenario()) == "/keys?result=option_sell_off"
    assert features.enabled(world.conn, user, "options")
    assert not features.enabled(world.conn, user, "option_sell")


def test_the_switches_need_login_csrf_and_post(serve: Serve) -> None:
    world = World()

    async def scenario() -> dict[str, list[int]]:
        codes: dict[str, list[int]] = {}
        async with serve(world.mcp) as url, httpx2.AsyncClient() as http:
            base = url.removesuffix("/mcp")
            for path in (OPTIONS_PATH, OPTION_SELL_PATH):
                codes[path] = [(await http.post(base + path, data={"enabled": "on"})).status_code]
            await browser_login(http, base, "admin-code")
            web = world.login.user_from_cookie(http.cookies.get(SESSION_COOKIE) or "")
            assert web is not None
            for path in (OPTIONS_PATH, OPTION_SELL_PATH):
                forged = {"csrf": "forged", "enabled": "on"}
                codes[path].append((await http.post(base + path, data=forged)).status_code)
                codes[path].append((await http.get(base + path)).status_code)
                odd = {"csrf": web.csrf, "enabled": "yes"}
                codes[path].append((await http.post(base + path, data=odd)).status_code)
        return codes

    codes = asyncio.run(scenario())

    assert codes == {OPTIONS_PATH: [403, 403, 405, 400], OPTION_SELL_PATH: [403, 403, 405, 400]}
    user = users.find_by_google_sub(world.conn, "g-admin")
    assert user is not None
    assert not features.enabled(world.conn, user, "options")
    assert not features.enabled(world.conn, user, "option_sell")
