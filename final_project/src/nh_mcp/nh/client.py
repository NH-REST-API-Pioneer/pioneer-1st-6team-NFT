"""NH선물 REST 호출. 주소는 모의 서버로 고정돼 있고 바꾸는 인자가 없다.

여기서 지키는 것:
1. 접근토큰을 붙이고, 만료 전에 다시 받는다.
2. HTTP 200이어도 본문의 errorCode로 실패를 판정한다.
3. 응답을 받지 못한 요청은 다시 보내지 않는다. 처리됐는지 알 수 없기 때문이다.
   토큰이 거절된 경우(문 앞에서 막힌 요청)만 토큰을 다시 받아 한 번 더 보낸다.
4. APP Key, Secret, 접근토큰은 예외 메시지와 repr에 넣지 않는다.
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import cast

import httpx2

from nh_mcp.config import NH_BASE_URL
from nh_mcp.domain import JsonValue

TOKEN_PATH = "/auth-service/v1/token"
TIMEOUT_SECONDS = 10.0
# 만료 직전에 보낸 요청이 거절되지 않게 미리 다시 받는다.
TOKEN_MARGIN = timedelta(minutes=5)
HTTP_UNAUTHORIZED = 401
HTTP_ERROR_FROM = 400
# 토큰을 다시 받으라는 errorCode.
REISSUE_CODES = frozenset({"EGW018", "EGW052"})
# 정상 처리인데 errorCode가 실려 오는 값. 팀 앱이 실제 응답에서 본 것이고 명세 근거는 없다.
OK_CODES = frozenset({"EGW002", "EGW013", "0"})
HIDDEN = "***"

type Clock = Callable[[], datetime]
type JsonObject = dict[str, JsonValue]


class NHError(Exception):
    """NH 호출 실패. 메시지에 키, 시크릿, 토큰을 넣지 않는다."""


class NoResponse(NHError):
    """응답을 받지 못했다. 처리됐는지 알 수 없으므로 다시 보내지 않는다."""


class Rejected(NHError):
    """NH가 요청을 거부했다(HTTP 오류 또는 본문의 errorCode)."""

    def __init__(self, code: str, message: str, http_status: int) -> None:
        super().__init__(f"NH가 거부했습니다: {code} {message}".rstrip())
        self.code = code
        self.message = message
        self.http_status = http_status


@dataclass(frozen=True)
class Credentials:
    app_key: str = field(repr=False)
    app_secret: str = field(repr=False)


def parse_body(text: str) -> JsonValue:
    # 실수는 문자열로 둔다. 가격과 금액을 float로 만들지 않는다.
    return _as_json(json.loads(text, parse_float=str))


def _as_json(value: object) -> JsonValue:
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, list):
        return [_as_json(item) for item in cast(list[object], value)]
    if isinstance(value, dict):
        return {str(k): _as_json(v) for k, v in cast(dict[object, object], value).items()}
    raise NHError("응답을 해석할 수 없습니다.")


class NHClient:
    def __init__(
        self,
        credentials: Credentials,
        clock: Clock,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._credentials = credentials
        self._clock = clock
        self._http = httpx2.AsyncClient(
            base_url=NH_BASE_URL, transport=transport, timeout=TIMEOUT_SECONDS
        )
        self._token: str | None = None
        self._token_until: datetime | None = None

    def __repr__(self) -> str:
        return "NHClient(<hidden>)"

    async def aclose(self) -> None:
        await self._http.aclose()

    async def post(self, path: str, payload: Mapping[str, JsonValue]) -> JsonObject:
        status, body = await self._send(path, dict(payload), await self._ensure_token())
        if status == HTTP_UNAUTHORIZED or _error_code(body) in REISSUE_CODES:
            self._token = None
            status, body = await self._send(path, dict(payload), await self._ensure_token())
        return self._accept(status, body)

    async def _ensure_token(self) -> str:
        now = self._clock()
        if self._token and self._token_until and now < self._token_until - TOKEN_MARGIN:
            return self._token
        form = {
            "grant_type": "client_credentials",
            "appkey": self._credentials.app_key,
            "appsecret": self._credentials.app_secret,
        }
        status, body = await self._request(TOKEN_PATH, form=form)
        issued = self._accept(status, body)
        token, expires_in = issued.get("access_token"), issued.get("expires_in")
        if not isinstance(token, str) or not token or not isinstance(expires_in, str | int):
            raise NHError("접근토큰 응답에 토큰이나 만료 시간이 없습니다.")
        self._token = token
        self._token_until = now + timedelta(seconds=int(expires_in))
        return token

    async def _send(self, path: str, payload: JsonObject, token: str) -> tuple[int, JsonValue]:
        return await self._request(path, json_body=payload, token=token)

    async def _request(
        self,
        path: str,
        *,
        json_body: JsonObject | None = None,
        form: dict[str, str] | None = None,
        token: str | None = None,
    ) -> tuple[int, JsonValue]:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            response = await self._http.post(path, json=json_body, data=form, headers=headers)
        except httpx2.HTTPError:
            # 원래 예외에는 요청 머리말(토큰)이 달려 있을 수 있어 잇지 않는다.
            raise NoResponse(f"{path} 응답을 받지 못했습니다. 처리됐는지 알 수 없습니다.") from None
        try:
            body = parse_body(response.text)
        except ValueError:
            body = response.text
        return response.status_code, body

    def _accept(self, status: int, body: JsonValue) -> JsonObject:
        code = _error_code(body)
        failed_code = code if code and code not in OK_CODES else ""
        if status >= HTTP_ERROR_FROM or failed_code:
            message = body.get("errorMessage") if isinstance(body, dict) else body
            raise Rejected(failed_code or str(status), self._hide(str(message or "")), status)
        if not isinstance(body, dict):
            raise NHError("응답이 객체가 아닙니다.")
        return body

    def _hide(self, text: str) -> str:
        for secret in (self._credentials.app_key, self._credentials.app_secret, self._token):
            if secret:
                text = text.replace(secret, HIDDEN)
        return text


def _error_code(body: JsonValue) -> str:
    if isinstance(body, dict):
        code = body.get("errorCode")
        if isinstance(code, str | int):
            return str(code).strip()
    return ""
