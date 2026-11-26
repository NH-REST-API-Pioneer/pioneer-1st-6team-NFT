"""등록 화면의 "자동 실행" 스위치: 로그인·CSRF·POST로만 바뀌고, 켜면 MCP 도구 목록에 묶음의 도구가
나타나고, 끄면 그 사용자의 전략을 멈추고 도구가 사라진다."""

import asyncio
from datetime import timedelta
from decimal import Decimal

import httpx2
from conftest import Serve
from fakes import ADMIN, T0, World, browser_login
from fastmcp import Client

from nh_mcp.auth.web import SESSION_COOKIE
from nh_mcp.domain import StrategyScope, Streams, UserId
from nh_mcp.keys.page import AUTO_RUN_PATH
from nh_mcp.store import features, strategy_runs, users
from nh_mcp.store.strategy_runs import Change, StrategyRun

AUTO_RUN_TOOLS = {"preview_strategy", "start_strategy", "stop_strategy", "get_strategy_status"}
SCOPE = StrategyScope(1, 1, 10, Decimal("100000"), T0 + timedelta(days=2), "hold", "auto")


def paused_run(world: World, user: UserId) -> None:
    streams: Streams = {"A016C000": frozenset({"minute"})}
    run = StrategyRun("paused-1", user, "domestic", streams, "c" * 64, {}, None, SCOPE, T0)
    strategy_runs.insert(world.conn, run)
    change = Change("starting", "paused", "server_restart")
    strategy_runs.set_state(world.conn, user, "paused-1", change, T0)


async def tool_names(url: str) -> set[str]:
    async with Client(url, auth="admin-token") as client:
        return {t.name for t in await client.list_tools()}


def test_switching_on_shows_the_tools_and_off_stops_runs_and_hides_them(serve: Serve) -> None:
    world = World(orders_enabled=True)
    user = users.get_or_create(world.conn, "g-admin", ADMIN, T0)
    paused_run(world, user)
    pages: dict[str, str] = {}
    tools: dict[str, set[str]] = {}

    async def scenario() -> None:
        async with serve(world.mcp) as url, httpx2.AsyncClient() as http:
            base = url.removesuffix("/mcp")
            await browser_login(http, base, "admin-code")
            web = world.login.user_from_cookie(http.cookies.get(SESSION_COOKIE) or "")
            assert web is not None
            pages["off"] = (await http.get(base + "/keys")).text
            tools["off"] = await tool_names(url)
            for wanted in ("on", "off"):
                form = {"csrf": web.csrf, "enabled": wanted}
                moved = await http.post(base + AUTO_RUN_PATH, data=form)
                pages[f"{wanted}_location"] = moved.headers["location"]
                pages[f"{wanted}_page"] = (await http.get(base + moved.headers["location"])).text
                tools[wanted + "_now"] = await tool_names(url)

    asyncio.run(scenario())

    assert "자동 실행" in pages["off"] and "꺼져 있습니다" in pages["off"]
    assert not tools["off"] & AUTO_RUN_TOOLS
    assert pages["on_location"] == "/keys?result=auto_run_on"
    assert "켜져 있습니다" in pages["on_page"]
    assert tools["on_now"] >= AUTO_RUN_TOOLS
    assert pages["off_location"] == "/keys?result=auto_run_stopped"
    assert "돌던 전략을 모두 멈췄습니다" in pages["off_page"]
    assert not tools["off_now"] & AUTO_RUN_TOOLS
    [row] = strategy_runs.list_for(world.conn, user)
    assert (row.state, row.reason) == ("stopped", "user")
    assert not features.enabled(world.conn, user, "auto_run")


def test_the_switch_needs_login_csrf_and_post(serve: Serve) -> None:
    world = World(orders_enabled=True)

    async def scenario() -> list[int]:
        async with serve(world.mcp) as url, httpx2.AsyncClient() as http:
            target = url.removesuffix("/mcp") + AUTO_RUN_PATH
            codes = [(await http.post(target, data={"enabled": "on"})).status_code]
            await browser_login(http, url.removesuffix("/mcp"), "admin-code")
            forged = {"csrf": "forged", "enabled": "on"}
            codes.append((await http.post(target, data=forged)).status_code)
            codes.append((await http.get(target, params={"enabled": "on"})).status_code)
            web = world.login.user_from_cookie(http.cookies.get(SESSION_COOKIE) or "")
            assert web is not None
            odd = {"csrf": web.csrf, "enabled": "yes"}
            codes.append((await http.post(target, data=odd)).status_code)
            return codes

    assert asyncio.run(scenario()) == [403, 403, 405, 400]
    user = users.find_by_google_sub(world.conn, "g-admin")
    assert user is not None
    assert not features.enabled(world.conn, user, "auto_run")


def test_without_orders_the_page_has_no_switch_and_the_tools_do_not_exist(serve: Serve) -> None:
    world = World(orders_enabled=False)
    user = users.get_or_create(world.conn, "g-admin", ADMIN, T0)
    features.turn_on(world.conn, user, "auto_run", T0)

    async def scenario() -> tuple[str, set[str], int]:
        async with serve(world.mcp) as url, httpx2.AsyncClient() as http:
            base = url.removesuffix("/mcp")
            await browser_login(http, base, "admin-code")
            web = world.login.user_from_cookie(http.cookies.get(SESSION_COOKIE) or "")
            assert web is not None
            page = (await http.get(base + "/keys")).text
            form = {"csrf": web.csrf, "enabled": "off"}
            status = (await http.post(base + AUTO_RUN_PATH, data=form)).status_code
            return page, await tool_names(url), status

    page, names, status = asyncio.run(scenario())

    assert AUTO_RUN_PATH not in page
    assert not names & AUTO_RUN_TOOLS
    assert status == 403
