"""NH 응답 녹화와 재생.

녹화: 실제 전송을 감싸 요청과 응답을 파일로 남긴다. 저장 전에 계좌 식별 정보와 토큰을 지우고,
키·시크릿·토큰이 남아 있으면 저장하지 않는다. 토큰 발급 요청은 키가 담겨 있어 남기지 않는다.
재생: 남긴 응답을 가짜 서버처럼 돌려준다. 테스트는 모의 서버 없이 돈다.
"""

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx2

from nh_mcp.domain import JsonValue
from nh_mcp.nh.client import TOKEN_PATH, JsonObject, NHError, parse_body

REDACTED = "<redacted>"
# 명세에서 계좌번호·계좌명을 담는 필드(ac_no, occurs1_ac_no, occurs1_ac_nm)와 인증 값.
SENSITIVE_FIELDS = frozenset({"access_token", "appkey", "appsecret", "ac_no", "ac_nm"})
SENSITIVE_SUFFIXES = ("_ac_no", "_ac_nm")
REPLAY_TOKEN = "replay-token-not-real"


class RecordingError(NHError):
    """지운 뒤에도 비밀값이 남아 있어 저장을 거부했다. 메시지에 그 값을 넣지 않는다."""


def is_sensitive(field: str) -> bool:
    name = field.lower()
    return name in SENSITIVE_FIELDS or name.endswith(SENSITIVE_SUFFIXES)


def scrub(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return {k: (REDACTED if is_sensitive(k) else scrub(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(item) for item in value]
    return value


@dataclass(frozen=True)
class Fixture:
    path: str
    request: JsonObject
    status: int
    body: JsonValue

    def to_json(self) -> str:
        data = {
            "path": self.path,
            "request": self.request,
            "status": self.status,
            "body": self.body,
        }
        return json.dumps(data, ensure_ascii=False, indent=2) + "\n"

    @staticmethod
    def load(file: Path) -> "Fixture":
        data = parse_body(file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise NHError(f"녹화 파일 형식이 틀렸습니다: {file.name}")
        path, request, status = data.get("path"), data.get("request"), data.get("status")
        if (
            not isinstance(path, str)
            or not isinstance(request, dict)
            or not isinstance(status, int)
        ):
            raise NHError(f"녹화 파일 형식이 틀렸습니다: {file.name}")
        return Fixture(path, request, status, data.get("body"))


class RecordingTransport(httpx2.AsyncBaseTransport):
    def __init__(
        self,
        inner: httpx2.AsyncBaseTransport,
        sink: Callable[[Fixture], None],
        secrets: Sequence[str],
    ) -> None:
        self._inner = inner
        self._sink = sink
        self._secrets = [s for s in secrets if s]

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        response = await self._inner.handle_async_request(request)
        content = await response.aread()
        if request.url.path != TOKEN_PATH:
            self._keep(request, response.status_code, content)
        # 본문은 이미 풀었으므로 압축 관련 머리말은 넘기지 않는다.
        return httpx2.Response(response.status_code, content=content)

    async def aclose(self) -> None:
        await self._inner.aclose()

    def _keep(self, request: httpx2.Request, status: int, content: bytes) -> None:
        sent = scrub(parse_body(request.content.decode() or "{}"))
        try:
            received = parse_body(content.decode())
        except ValueError:
            received = content.decode(errors="replace")
        fixture = Fixture(
            request.url.path,
            sent if isinstance(sent, dict) else {},
            status,
            scrub(received),
        )
        token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        text = fixture.to_json()
        if any(secret in text for secret in [*self._secrets, token] if secret):
            raise RecordingError(
                f"{request.url.path} 응답에 비밀값이 남아 있어 저장하지 않았습니다."
            )
        self._sink(fixture)


def replay_transport(fixtures: Sequence[Fixture]) -> httpx2.MockTransport:
    """같은 경로는 녹화 순서대로 돌려준다. 녹화에 없는 요청은 실패시킨다."""
    queues: dict[str, list[Fixture]] = {}
    for fixture in fixtures:
        queues.setdefault(fixture.path, []).append(fixture)

    def handle(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == TOKEN_PATH:
            body: JsonValue = {"access_token": REPLAY_TOKEN, "expires_in": "86400"}
            return httpx2.Response(200, content=json.dumps(body).encode())
        queue = queues.get(request.url.path)
        if not queue:
            raise NHError(f"녹화에 없는 요청입니다: {request.url.path}")
        fixture = queue.pop(0)
        return httpx2.Response(fixture.status, content=json.dumps(fixture.body).encode())

    return httpx2.MockTransport(handle)
