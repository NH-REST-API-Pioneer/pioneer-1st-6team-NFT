import asyncio
import inspect
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

import httpx2
import pytest

from nh_mcp.domain import JsonValue
from nh_mcp.nh.client import (
    TOKEN_PATH,
    Credentials,
    NHClient,
    NoResponse,
    Rejected,
)

# 실제 키가 아니다. 이 값이 메시지·repr·로그에 나오면 안 된다.
CANARY_KEY = "fake-app-key-0000"
CANARY_SECRET = "fake-app-secret-0000"
CANARY_TOKEN = "fake-token-0000"
PRICE_PATH = "/trade/v1/domestic/price"
T0 = datetime(2026, 12, 1, 0, 0, tzinfo=UTC)

type Reply = tuple[int, JsonValue]


class FakeNH:
    """모의 서버 흉내. 토큰 발급과 API 응답을 순서대로 돌려주고 받은 요청을 기록한다."""

    def __init__(self, replies: list[Reply], token_replies: list[Reply] | None = None) -> None:
        self.replies = replies
        self.token_replies = token_replies or []
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if request.url.path == TOKEN_PATH:
            status, body = (
                self.token_replies.pop(0)
                if self.token_replies
                else (200, {"access_token": CANARY_TOKEN, "expires_in": "86400"})
            )
        else:
            status, body = self.replies.pop(0)
        return httpx2.Response(status, content=json.dumps(body).encode())

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


def make(fake: FakeNH | Callable[[httpx2.Request], httpx2.Response], clock: Clock) -> NHClient:
    return NHClient(
        Credentials(CANARY_KEY, CANARY_SECRET), clock, transport=httpx2.MockTransport(fake)
    )


def test_issues_token_once_and_sends_it() -> None:
    fake = FakeNH([(200, {"last_pric": "350.25"}), (200, {"last_pric": "350.30"})])

    async def scenario() -> None:
        client = make(fake, Clock())
        assert await client.post(PRICE_PATH, {"sym": "A0166000"}) == {"last_pric": "350.25"}
        await client.post(PRICE_PATH, {"sym": "A0166000"})
        await client.aclose()

    asyncio.run(scenario())
    assert fake.paths() == [TOKEN_PATH, PRICE_PATH, PRICE_PATH]
    token_request, api_request = fake.requests[0], fake.requests[1]
    assert token_request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert parse_qs(token_request.content.decode()) == {
        "grant_type": ["client_credentials"],
        "appkey": [CANARY_KEY],
        "appsecret": [CANARY_SECRET],
    }
    assert api_request.headers["authorization"] == f"Bearer {CANARY_TOKEN}"
    assert json.loads(api_request.content) == {"sym": "A0166000"}


def test_calls_only_the_demo_server() -> None:
    fake = FakeNH([(200, {})])

    async def scenario() -> None:
        client = make(fake, Clock())
        await client.post(PRICE_PATH, {})
        await client.aclose()

    asyncio.run(scenario())
    assert {str(r.url.host) for r in fake.requests} == {"apidemo.futures.co.kr"}
    assert "base_url" not in inspect.signature(NHClient).parameters


def test_token_is_renewed_before_it_expires() -> None:
    fake = FakeNH([(200, {}), (200, {})])
    clock = Clock()

    async def scenario() -> None:
        client = make(fake, clock)
        await client.post(PRICE_PATH, {})
        clock.now = T0 + timedelta(seconds=86400) - timedelta(minutes=4)
        await client.post(PRICE_PATH, {})
        await client.aclose()

    asyncio.run(scenario())
    assert fake.paths() == [TOKEN_PATH, PRICE_PATH, TOKEN_PATH, PRICE_PATH]


def test_http_200_with_error_code_is_a_failure() -> None:
    fake = FakeNH([(200, {"errorCode": "ETR029", "errorMessage": "종목 정보를 찾을 수 없습니다."})])

    async def scenario() -> None:
        client = make(fake, Clock())
        try:
            await client.post(PRICE_PATH, {"sym": "XXXX"})
        finally:
            await client.aclose()

    with pytest.raises(Rejected) as caught:
        asyncio.run(scenario())
    assert (caught.value.code, caught.value.http_status) == ("ETR029", 200)


def test_known_ok_code_is_not_a_failure() -> None:
    fake = FakeNH([(200, {"errorCode": "0", "last_pric": "1"})])

    async def scenario() -> dict[str, JsonValue]:
        client = make(fake, Clock())
        body = await client.post(PRICE_PATH, {})
        await client.aclose()
        return body

    assert asyncio.run(scenario())["last_pric"] == "1"


def test_http_error_status_is_a_failure() -> None:
    fake = FakeNH([(500, {"errorMessage": "서버 오류"})])

    async def scenario() -> None:
        client = make(fake, Clock())
        try:
            await client.post(PRICE_PATH, {})
        finally:
            await client.aclose()

    with pytest.raises(Rejected) as caught:
        asyncio.run(scenario())
    assert caught.value.http_status == 500


@pytest.mark.parametrize(
    "rejection", [(401, {"errorMessage": "만료"}), (200, {"errorCode": "EGW052"})]
)
def test_rejected_token_is_renewed_and_retried_once(rejection: Reply) -> None:
    fake = FakeNH([rejection, (200, {"ok": "yes"})])

    async def scenario() -> dict[str, JsonValue]:
        client = make(fake, Clock())
        body = await client.post(PRICE_PATH, {})
        await client.aclose()
        return body

    assert asyncio.run(scenario()) == {"ok": "yes"}
    assert fake.paths() == [TOKEN_PATH, PRICE_PATH, TOKEN_PATH, PRICE_PATH]


def test_token_rejected_twice_gives_up() -> None:
    fake = FakeNH([(401, {}), (401, {})])

    async def scenario() -> None:
        client = make(fake, Clock())
        try:
            await client.post(PRICE_PATH, {})
        finally:
            await client.aclose()

    with pytest.raises(Rejected):
        asyncio.run(scenario())
    assert fake.paths() == [TOKEN_PATH, PRICE_PATH, TOKEN_PATH, PRICE_PATH]


def test_no_response_is_not_resent() -> None:
    sent: list[str] = []

    def timing_out(request: httpx2.Request) -> httpx2.Response:
        sent.append(request.url.path)
        if request.url.path == TOKEN_PATH:
            return httpx2.Response(
                200, content=json.dumps({"access_token": CANARY_TOKEN, "expires_in": "86400"})
            )
        raise httpx2.ReadTimeout("시간 초과", request=request)

    async def scenario() -> None:
        client = make(timing_out, Clock())
        try:
            await client.post("/trade/v1/domestic/order", {"sym": "A0166000"})
        finally:
            await client.aclose()

    with pytest.raises(NoResponse):
        asyncio.run(scenario())
    assert sent == [TOKEN_PATH, "/trade/v1/domestic/order"]


def test_invalid_key_on_token_issue_is_rejected_with_its_code() -> None:
    fake = FakeNH(
        [], token_replies=[(400, {"errorCode": "EAU025", "errorMessage": "잘못된 appkey"})]
    )

    async def scenario() -> None:
        client = make(fake, Clock())
        try:
            await client.post(PRICE_PATH, {})
        finally:
            await client.aclose()

    with pytest.raises(Rejected) as caught:
        asyncio.run(scenario())
    assert caught.value.code == "EAU025"


def test_floats_in_response_stay_strings() -> None:
    def raw(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == TOKEN_PATH:
            return httpx2.Response(
                200, content=json.dumps({"access_token": CANARY_TOKEN, "expires_in": 86400})
            )
        return httpx2.Response(200, content=b'{"last_pric": 350.25, "qty": 3}')

    async def scenario() -> dict[str, JsonValue]:
        client = make(raw, Clock())
        body = await client.post(PRICE_PATH, {})
        await client.aclose()
        return body

    assert asyncio.run(scenario()) == {"last_pric": "350.25", "qty": 3}


def test_secrets_never_appear_in_errors_reprs_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    # NH가 오류 메시지에 키와 토큰을 되돌려 보내는 최악의 경우까지 본다.
    echo = f"{CANARY_KEY} {CANARY_SECRET} {CANARY_TOKEN}"
    fake = FakeNH([(200, {"errorCode": "ETR001", "errorMessage": echo})])
    caught: list[BaseException] = []

    async def scenario() -> NHClient:
        client = make(fake, Clock())
        try:
            await client.post(PRICE_PATH, {})
        except Rejected as exc:
            caught.append(exc)
        await client.aclose()
        return client

    with caplog.at_level(logging.DEBUG):
        client = asyncio.run(scenario())

    shown = " ".join(
        [
            str(caught[0]),
            repr(caught[0]),
            repr(client),
            repr(Credentials(CANARY_KEY, CANARY_SECRET)),
        ]
        + [record.getMessage() for record in caplog.records]
    )
    for secret in (CANARY_KEY, CANARY_SECRET, CANARY_TOKEN):
        assert secret not in shown
