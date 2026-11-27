import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import pytest

from nh_mcp.domain import JsonValue
from nh_mcp.nh.client import TOKEN_PATH, Credentials, NHClient
from nh_mcp.nh.limits import grade_of
from nh_mcp.nh.record import board_case, cases, orderable_case
from nh_mcp.nh.recording import (
    REDACTED,
    Fixture,
    RecordingError,
    RecordingTransport,
    replay_transport,
    scrub,
)

FAKE_KEY = "fake-app-key-0000"
FAKE_SECRET = "fake-app-secret-0000"
FAKE_TOKEN = "fake-token-0000"
OPEN_INTEREST = "/trade/v1/domestic/open-interest"
PRICE = "/trade/v1/domestic/price"
NOW = datetime(2026, 12, 1, 1, 0, tzinfo=UTC)

ACCOUNT_BODY: dict[str, JsonValue] = {
    "nrec": "1",
    "Occurs1": [
        {
            "occurs1_ac_no": "12345678901",
            "occurs1_ac_nm": "홍길동",
            "occurs1_sym": "A0166000",
            "occurs1_pl_amt": "15000",
            "occurs1_user_ip_addr": "10.20.30.40",
        }
    ],
    "ac_no": "12345678901",
}


def test_scrub_removes_account_identifiers_and_tokens() -> None:
    cleaned = scrub({**ACCOUNT_BODY, "access_token": FAKE_TOKEN})
    text = json.dumps(cleaned, ensure_ascii=False)
    assert "12345678901" not in text
    assert "홍길동" not in text
    assert "10.20.30.40" not in text
    assert FAKE_TOKEN not in text
    assert "A0166000" in text and "15000" in text
    assert isinstance(cleaned, dict) and cleaned["ac_no"] == REDACTED


def fake_nh(body: JsonValue) -> httpx2.MockTransport:
    def handle(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == TOKEN_PATH:
            token: JsonValue = {"access_token": FAKE_TOKEN, "expires_in": "86400"}
            return httpx2.Response(200, content=json.dumps(token).encode())
        return httpx2.Response(200, content=json.dumps(body, ensure_ascii=False).encode())

    return httpx2.MockTransport(handle)


def record_through(body: JsonValue, path: str = OPEN_INTEREST) -> tuple[list[Fixture], JsonValue]:
    captured: list[Fixture] = []
    transport = RecordingTransport(fake_nh(body), captured.append, [FAKE_KEY, FAKE_SECRET])

    async def scenario() -> JsonValue:
        client = NHClient(Credentials(FAKE_KEY, FAKE_SECRET), lambda: NOW, transport=transport)
        try:
            return await client.post(path, {"nxt_key": ""})
        finally:
            await client.aclose()

    return captured, asyncio.run(scenario())


def test_recording_saves_scrubbed_response_and_skips_token_request() -> None:
    captured, received = record_through(ACCOUNT_BODY)

    assert [f.path for f in captured] == [OPEN_INTEREST]
    saved = captured[0].to_json()
    for secret in ("12345678901", "홍길동", FAKE_KEY, FAKE_SECRET, FAKE_TOKEN):
        assert secret not in saved
    # 호출한 쪽은 원래 응답을 받는다. 지우는 것은 저장본뿐이다.
    assert received == ACCOUNT_BODY


def test_recording_refuses_when_a_secret_survives_scrubbing() -> None:
    with pytest.raises(RecordingError) as caught:
        record_through({"errorCode": "ETR001", "errorMessage": f"키 {FAKE_KEY} 오류"})
    assert FAKE_KEY not in str(caught.value)


def test_fixture_file_roundtrip(tmp_path: Path) -> None:
    fixture = Fixture(PRICE, {"sym": "A0166000"}, 200, {"last_pric": "350.25"})
    file = tmp_path / "price.json"
    file.write_text(fixture.to_json(), encoding="utf-8")
    assert Fixture.load(file) == fixture


def test_replay_serves_recorded_responses_in_order() -> None:
    first = Fixture(PRICE, {}, 200, {"last_pric": "1"})
    second = Fixture(PRICE, {}, 200, {"last_pric": "2"})

    async def scenario() -> list[JsonValue]:
        client = NHClient(
            Credentials(FAKE_KEY, FAKE_SECRET),
            lambda: NOW,
            transport=replay_transport([first, second]),
        )
        try:
            return [await client.post(PRICE, {}), await client.post(PRICE, {})]
        finally:
            await client.aclose()

    assert asyncio.run(scenario()) == [{"last_pric": "1"}, {"last_pric": "2"}]


def test_replay_refuses_requests_it_has_no_recording_for() -> None:
    async def scenario() -> None:
        client = NHClient(
            Credentials(FAKE_KEY, FAKE_SECRET), lambda: NOW, transport=replay_transport([])
        )
        try:
            await client.post(PRICE, {})
        finally:
            await client.aclose()

    with pytest.raises(Exception, match="녹화에 없는 요청"):
        asyncio.run(scenario())


def test_recorder_never_calls_order_apis() -> None:
    recorded = [
        *cases("A0166000", "20261201"),
        orderable_case("A0166000", "350.25"),
        board_case("261210", 1, "99999999"),
    ]
    assert recorded
    assert all(grade_of(path) != "order" for _, path, _ in recorded)
