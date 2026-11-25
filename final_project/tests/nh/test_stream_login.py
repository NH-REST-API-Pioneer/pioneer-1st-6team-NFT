import asyncio
import json
from datetime import UTC, datetime

import httpx2
import pytest

from nh_mcp.domain import JsonValue
from nh_mcp.nh.client import (
    TOKEN_PATH,
    WEBSOCKET_KEY_PATH,
    Credentials,
    NHClient,
    NHError,
    Rejected,
)

CANARY_TOKEN = "fake-token-0000"
CANARY_WS_KEY = "fake-ws-key-000000000000000000000000"


class FakeNH:
    def __init__(self, replies: list[tuple[int, JsonValue]]) -> None:
        self.replies = replies
        self.requests: list[httpx2.Request] = []
        self.tokens = 0

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if request.url.path == TOKEN_PATH:
            self.tokens += 1
            body: JsonValue = {
                "access_token": f"{CANARY_TOKEN}-{self.tokens}",
                "expires_in": "86400",
            }
            return httpx2.Response(200, content=json.dumps(body).encode())
        status, reply = self.replies.pop(0)
        return httpx2.Response(status, content=json.dumps(reply).encode())


def make(fake: FakeNH) -> NHClient:
    return NHClient(
        Credentials("fake-app-key-0000", "fake-app-secret-0000"),
        lambda: datetime(2026, 12, 1, tzinfo=UTC),
        transport=httpx2.MockTransport(fake),
    )


def test_gets_a_websocket_key_with_the_bearer_token() -> None:
    fake = FakeNH([(200, {"access_key": CANARY_WS_KEY})])

    async def scenario() -> None:
        login = await make(fake).stream_login()
        assert (login.access_key, login.session_token) == (CANARY_WS_KEY, f"{CANARY_TOKEN}-1")
        assert CANARY_WS_KEY not in repr(login) and CANARY_TOKEN not in repr(login)

    asyncio.run(scenario())
    key_request = fake.requests[-1]
    assert (key_request.method, key_request.url.path) == ("GET", WEBSOCKET_KEY_PATH)
    assert str(key_request.url).startswith("https://apidemo.futures.co.kr/")
    assert key_request.headers["Authorization"] == f"Bearer {CANARY_TOKEN}-1"


def test_expired_token_is_reissued_once_and_the_new_token_is_handed_out() -> None:
    fake = FakeNH([(200, {"errorCode": "EGW018"}), (200, {"access_key": CANARY_WS_KEY})])

    async def scenario() -> None:
        login = await make(fake).stream_login()
        assert login.session_token == f"{CANARY_TOKEN}-2"

    asyncio.run(scenario())
    assert fake.tokens == 2


def test_renew_token_drops_the_held_token() -> None:
    fake = FakeNH([(200, {"access_key": CANARY_WS_KEY}), (200, {"access_key": CANARY_WS_KEY})])

    async def scenario() -> None:
        client = make(fake)
        await client.stream_login()
        await client.stream_login(renew_token=True)

    asyncio.run(scenario())
    assert fake.tokens == 2


EMPTY: dict[str, JsonValue] = {}
FAILURES: list[tuple[tuple[int, JsonValue], type[NHError]]] = [
    ((200, {"errorCode": "EGW001", "errorMessage": "키 오류"}), Rejected),
    ((500, EMPTY), Rejected),
    ((200, {"access_key": ""}), NHError),
    ((200, EMPTY), NHError),
]


@pytest.mark.parametrize(("reply", "error"), FAILURES)
def test_failed_key_issue_raises(reply: tuple[int, JsonValue], error: type[NHError]) -> None:
    async def scenario() -> None:
        with pytest.raises(error) as caught:
            await make(FakeNH([reply])).stream_login()
        assert CANARY_TOKEN not in str(caught.value)

    asyncio.run(scenario())
