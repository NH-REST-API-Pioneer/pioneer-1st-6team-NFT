"""한도 승인 링크 화면: 로그인, 승인·거절, CSRF, 남의 링크, 돌아올 주소."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlencode, urlparse

import httpx2
import pytest
from conftest import Serve
from fakes import SETTINGS, World, browser_login

from nh_mcp.auth.web import CALLBACK_PATH, LOGIN_PATH, SESSION_COOKIE, safe_return_to
from nh_mcp.domain import OptionLimits, RiskLimits, UserId
from nh_mcp.services.limits import APPROVE_PATH, LimitService
from nh_mcp.store import confirm_tokens, limits, users
from nh_mcp.store.confirm_tokens import NewToken
from nh_mcp.tools.limits_page import APPLIED, CHANGED, GONE, REJECTED

CURRENT = RiskLimits(Decimal("100000"), 3, 2)
HIGHER = RiskLimits(Decimal("250000"), 7, 4)
WITH_OPTIONS = RiskLimits(Decimal("100000"), 3, 2, OptionLimits(Decimal("500000"), 10, 1))

type Step = Callable[[httpx2.AsyncClient, str, str], Awaitable[None]]


def me(world: World) -> UserId:
    return users.get_or_create(world.conn, "g-admin", "admin@example.com", world.clock())


def service(world: World) -> LimitService:
    return LimitService(world.conn, world.clock, SETTINGS.public_base_url + APPROVE_PATH)


def link(world: World, requested: RiskLimits = HIGHER, owner: UserId | None = None) -> str:
    """승인 링크 하나를 발급하고 토큰을 돌려준다. 승인 창이 없는 클라이언트를 흉내 낸다."""
    user = owner if owner is not None else me(world)
    svc = service(world)
    plan = svc.plan(user, "domestic", requested)
    result = svc.commit(user, "domestic", plan, "unsupported")
    assert result.approval_url is not None
    return result.approval_url.rsplit("/", 1)[1]


def stored(world: World) -> RiskLimits | None:
    return limits.get(world.conn, me(world), "domestic")


def usable(world: World, token: str) -> bool:
    return service(world).pending(me(world), token) is not None


def approve_audit(world: World) -> list[str]:
    rows = world.conn.execute("SELECT detail FROM audit_log WHERE action = 'limits.approve'")
    return [str(json.loads(r[0])["outcome"]) for r in rows.fetchall()]


def browser(serve: Serve, world: World, step: Step, *, login: bool = True) -> None:
    async def scenario() -> None:
        async with serve(world.mcp) as url, httpx2.AsyncClient() as http:
            base = url.removesuffix("/mcp")
            if login:
                await browser_login(http, base, "admin-code")
            await step(http, base, csrf(world, http))

    asyncio.run(scenario())


def csrf(world: World, http: httpx2.AsyncClient) -> str:
    user = world.login.user_from_cookie(http.cookies.get(SESSION_COOKIE) or "")
    return user.csrf if user else ""


def path(token: str) -> str:
    return f"{APPROVE_PATH}/{token}"


def test_logged_out_visitor_goes_to_login_and_comes_back(serve: Serve) -> None:
    world = World()
    token = link(world)
    seen: dict[str, str] = {}

    async def step(http: httpx2.AsyncClient, base: str, _: str) -> None:
        first = await http.get(base + path(token))
        seen["first"] = first.headers["location"]
        started = await http.get(base + seen["first"])
        # 시험에서는 Google 화면을 건너뛰고 로그인 쿠키의 state로 callback을 부른다.
        state = parse_qs(urlparse(started.headers["location"]).query)["state"][0]
        done = await http.get(base + CALLBACK_PATH, params={"code": "admin-code", "state": state})
        seen["back"] = done.headers["location"]

    browser(serve, world, step, login=False)

    assert seen["first"] == f"{LOGIN_PATH}?{urlencode({'return_to': path(token)})}"
    assert seen["back"] == path(token)


def test_owner_sees_current_and_new_values_and_get_changes_nothing(serve: Serve) -> None:
    world = World()
    limits.put(world.conn, me(world), "domestic", CURRENT, world.clock())
    token = link(world)
    pages: list[httpx2.Response] = []

    async def step(http: httpx2.AsyncClient, base: str, _: str) -> None:
        pages.append(await http.get(base + path(token)))
        pages.append(await http.get(base + path(token)))

    browser(serve, world, step)

    body = pages[0].text
    assert pages[0].status_code == 200
    for shown in ("100000원", "250000원", "3계약", "7계약", "2계약", "4계약", "admin@example.com"):
        assert shown in body
    assert token not in body
    assert stored(world) == CURRENT
    assert usable(world, token)
    assert pages[1].status_code == 200


def test_page_shows_option_limits_in_korean(serve: Serve) -> None:
    world = World()
    limits.put(world.conn, me(world), "domestic", CURRENT, world.clock())
    token = link(world, WITH_OPTIONS)
    pages: list[httpx2.Response] = []

    async def step(http: httpx2.AsyncClient, base: str, _: str) -> None:
        pages.append(await http.get(base + path(token)))

    browser(serve, world, step)

    body = pages[0].text
    for row in (
        "<tr><td>옵션 하루 프리미엄</td><td>없음</td><td>500000원</td></tr>",
        "<tr><td>옵션 전체 최대 계약</td><td>없음</td><td>10계약</td></tr>",
        "<tr><td>옵션 매도 최대 계약</td><td>없음</td><td>1계약</td></tr>",
    ):
        assert row in body
    assert stored(world) == CURRENT


def test_first_time_page_shows_none_as_current(serve: Serve) -> None:
    world = World()
    token = link(world)
    pages: list[httpx2.Response] = []

    async def step(http: httpx2.AsyncClient, base: str, _: str) -> None:
        pages.append(await http.get(base + path(token)))

    browser(serve, world, step)

    assert "없음" in pages[0].text


def test_approve_applies_once_and_the_same_link_is_then_gone(serve: Serve) -> None:
    world = World()
    token = link(world)
    answers: list[httpx2.Response] = []

    async def step(http: httpx2.AsyncClient, base: str, csrf_token: str) -> None:
        form = {"csrf": csrf_token, "decision": "approve"}
        answers.append(await http.post(base + path(token), data=form))
        answers.append(await http.post(base + path(token), data=form))
        answers.append(await http.get(base + path(token)))

    browser(serve, world, step)

    assert APPLIED in answers[0].text
    assert GONE in answers[1].text and answers[1].status_code == 200
    assert GONE in answers[2].text
    assert stored(world) == HIGHER
    assert approve_audit(world) == ["applied"]


@pytest.mark.parametrize("form", [{}, {"csrf": "forged"}])
def test_missing_or_wrong_csrf_applies_nothing(serve: Serve, form: dict[str, str]) -> None:
    world = World()
    token = link(world)
    answers: list[httpx2.Response] = []

    async def step(http: httpx2.AsyncClient, base: str, _: str) -> None:
        answers.append(await http.post(base + path(token), data={**form, "decision": "approve"}))

    browser(serve, world, step)

    assert answers[0].status_code == 403
    assert stored(world) is None
    assert usable(world, token)


def test_post_without_login_applies_nothing(serve: Serve) -> None:
    world = World()
    token = link(world)
    answers: list[httpx2.Response] = []

    async def step(http: httpx2.AsyncClient, base: str, _: str) -> None:
        answers.append(await http.post(base + path(token), data={"decision": "approve"}))

    browser(serve, world, step, login=False)

    assert answers[0].status_code == 403
    assert stored(world) is None
    assert usable(world, token)


@pytest.mark.parametrize("decision", ["", "APPROVE", "yes", "approve "])
def test_unknown_decision_is_400_and_consumes_nothing(serve: Serve, decision: str) -> None:
    world = World()
    token = link(world)
    answers: list[httpx2.Response] = []

    async def step(http: httpx2.AsyncClient, base: str, csrf_token: str) -> None:
        form = {"csrf": csrf_token, "decision": decision}
        answers.append(await http.post(base + path(token), data=form))

    browser(serve, world, step)

    assert answers[0].status_code == 400
    assert stored(world) is None
    assert usable(world, token)


def test_someone_elses_link_looks_like_a_missing_one(serve: Serve) -> None:
    world = World()
    other = users.get_or_create(world.conn, "g-someone", "someone@example.com", world.clock())
    token = link(world, owner=other)
    answers: list[httpx2.Response] = []

    async def step(http: httpx2.AsyncClient, base: str, csrf_token: str) -> None:
        answers.append(await http.get(base + path(token)))
        for decision in ("approve", "reject"):
            form = {"csrf": csrf_token, "decision": decision}
            answers.append(await http.post(base + path(token), data=form))

    browser(serve, world, step)

    assert [a.status_code for a in answers] == [200, 200, 200]
    assert all(GONE in a.text for a in answers)
    assert limits.get(world.conn, other, "domestic") is None
    assert service(world).pending(other, token) is not None


def test_unknown_malformed_and_non_limits_tokens_say_the_same_thing(serve: Serve) -> None:
    world = World()
    order_token, _ = confirm_tokens.issue(
        world.conn, NewToken(me(world), "order", "{}"), world.clock(), timedelta(minutes=5)
    )
    answers: list[httpx2.Response] = []

    async def step(http: httpx2.AsyncClient, base: str, csrf_token: str) -> None:
        for token in ("no-such-token", order_token, "a.b", "x" * 300):
            answers.append(await http.get(base + path(token)))
        form = {"csrf": csrf_token, "decision": "approve"}
        answers.append(await http.post(base + path(order_token), data=form))

    browser(serve, world, step)

    assert all(a.status_code == 200 and GONE in a.text for a in answers)
    assert stored(world) is None


def test_expired_link_is_gone(serve: Serve) -> None:
    world = World()
    token = link(world)
    world.clock.now += timedelta(minutes=11)
    answers: list[httpx2.Response] = []

    async def step(http: httpx2.AsyncClient, base: str, csrf_token: str) -> None:
        answers.append(await http.get(base + path(token)))
        form = {"csrf": csrf_token, "decision": "approve"}
        answers.append(await http.post(base + path(token), data=form))

    browser(serve, world, step)

    assert all(GONE in a.text for a in answers)
    assert stored(world) is None


def test_reject_burns_the_token_and_applies_nothing(serve: Serve) -> None:
    world = World()
    token = link(world)
    answers: list[httpx2.Response] = []

    async def step(http: httpx2.AsyncClient, base: str, csrf_token: str) -> None:
        reject = {"csrf": csrf_token, "decision": "reject"}
        approve = {"csrf": csrf_token, "decision": "approve"}
        answers.append(await http.post(base + path(token), data=reject))
        answers.append(await http.post(base + path(token), data=approve))

    browser(serve, world, step)

    assert REJECTED in answers[0].text
    assert GONE in answers[1].text
    assert stored(world) is None
    assert not usable(world, token)
    assert approve_audit(world) == ["rejected"]


def test_values_changed_in_the_meantime_are_not_applied(serve: Serve) -> None:
    world = World()
    token = link(world)
    meanwhile = RiskLimits(Decimal("1"), 1, 1)
    limits.put(world.conn, me(world), "domestic", meanwhile, world.clock())
    answers: list[httpx2.Response] = []

    async def step(http: httpx2.AsyncClient, base: str, csrf_token: str) -> None:
        form = {"csrf": csrf_token, "decision": "approve"}
        answers.append(await http.post(base + path(token), data=form))

    browser(serve, world, step)

    assert CHANGED in answers[0].text
    assert stored(world) == meanwhile
    assert not usable(world, token)


def test_pages_carry_the_security_headers(serve: Serve) -> None:
    world = World()
    token = link(world)
    answers: list[httpx2.Response] = []

    async def step(http: httpx2.AsyncClient, base: str, csrf_token: str) -> None:
        answers.append(await http.get(base + path(token)))
        answers.append(await http.get(base + path("nope")))
        answers.append(await http.post(base + path(token), data={"decision": "approve"}))
        form = {"csrf": csrf_token, "decision": "reject"}
        answers.append(await http.post(base + path(token), data=form))

    browser(serve, world, step)

    for response in answers:
        assert response.headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert response.headers["referrer-policy"] == "same-origin"


def test_return_to_accepts_only_same_server_allowed_paths() -> None:
    assert safe_return_to("/keys") == "/keys"
    assert safe_return_to("/models") == "/models"
    assert safe_return_to("/limits/approve/abc_DEF-123") == "/limits/approve/abc_DEF-123"
    for bad in (
        "",
        "https://evil.example/",
        "//evil.example",
        "///evil.example/limits/approve/x",
        "/\\evil.example",
        "\\\\evil.example",
        "/limits/approve/",
        "/limits/approve/x/y",
        "/limits/approve/x?next=https://evil.example",
        "/limits/approve/..%2f..%2fkeys",
        "/limits/approve/x\n",
        "/keys/login",
        "/keys/",
        "/other",
        "limits/approve/abc",
    ):
        assert safe_return_to(bad) == "/keys", bad


@pytest.mark.parametrize("target", ["https://evil.example/", "//evil.example", "/other"])
def test_login_with_a_foreign_return_to_lands_on_keys(serve: Serve, target: str) -> None:
    world = World()
    landed: list[str] = []

    async def step(http: httpx2.AsyncClient, base: str, _: str) -> None:
        started = await http.get(base + LOGIN_PATH, params={"return_to": target})
        state = parse_qs(urlparse(started.headers["location"]).query)["state"][0]
        done = await http.get(base + CALLBACK_PATH, params={"code": "admin-code", "state": state})
        landed.append(done.headers["location"])

    browser(serve, world, step, login=False)

    assert landed == ["/keys"]
