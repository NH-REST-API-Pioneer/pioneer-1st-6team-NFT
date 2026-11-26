"""MCP 서버가 실행기를 부르는 클라이언트. 토큰은 예외 메시지와 repr에 넣지 않는다."""

from dataclasses import dataclass, field, replace

import httpx2

from nh_mcp.domain import (
    JsonValue,
    Output,
    SandboxJob,
    SandboxResult,
    SessionFailed,
    SessionJob,
    StrategyEvent,
)
from nh_mcp.sandbox.wire import (
    MAX_DETAIL,
    TOKEN_HEADER,
    BadOutput,
    event_to_wire,
    fed_from_json,
    job_to_json,
    opened_from_json,
    result_from_json,
    session_to_json,
)

# 실행기 쪽 벽시계(70초)보다 길게 둔다. 컨테이너를 띄우고 지우는 시간이 더 든다. 세션을 여는 마감
# (60초)도 이 안에 든다.
TIMEOUT_SECONDS = 90.0
HTTP_OK = 200
HTTP_GONE = 404
HTTP_BUSY = 503


class SandboxBusy(Exception):
    """실행기가 동시 한도(백테스트 3개, 세션 40개)에 닿아 거절했다. 잠시 뒤 다시 시도하면 된다."""


class SandboxDown(Exception):
    """실행기에 닿지 못했거나 알 수 없는 답이 왔다. 메시지에 토큰을 넣지 않는다."""


class SessionGone(Exception):
    """세션이 없다. 닫았거나, 죽었거나, 만료됐거나, 실행기가 다시 시작했다. 그 전략을 멈춘다."""


@dataclass(frozen=True)
class SessionHandle:
    """열린 세션 하나. 식별자는 실행기가 만든 추측할 수 없는 값이라 repr에 넣지 않는다.

    job은 출력을 다시 검사할 계약이다(모델 파일은 뺐다).
    """

    session_id: str = field(repr=False)
    job: SessionJob


def _consistent(result: SandboxResult, event_count: int) -> bool:
    """내용이 일과 맞는지 본다. 성공이면 시장 이벤트마다 출력이 적어도 하나(체결 이벤트의 출력이
    더해진다), 실패면 출력 없음. 정확한 줄 수는 서비스가 체결 계산을 다시 돌려 맞춘다.
    """
    if len(result.detail) > MAX_DETAIL:
        return False
    if result.index is not None and not 0 <= result.index < event_count:
        return False
    if result.status == "ok":
        return len(result.outputs) >= event_count and result.index is None and not result.detail
    return not result.outputs


class SandboxClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token
        # trust_env=False: 프록시 환경 변수·.netrc를 따르지 않는다. 토큰과 코드는 실행기로만 간다.
        self._http = httpx2.AsyncClient(
            base_url=base_url, transport=transport, timeout=TIMEOUT_SECONDS, trust_env=False
        )

    def __repr__(self) -> str:
        return "SandboxClient(<hidden>)"

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, path: str, body: JsonValue = None) -> httpx2.Response:
        try:
            return await self._http.request(
                method, path, json=body, headers={TOKEN_HEADER: self._token}
            )
        except httpx2.HTTPError as exc:
            # 원인 예외는 잇지 않는다. 요청 머리말(토큰)이 따라오면 안 된다.
            raise SandboxDown(f"실행기에 닿지 못했습니다({type(exc).__name__})") from None

    async def _answer(
        self, method: str, path: str, body: JsonValue = None, gone: bool = False
    ) -> object:
        """200 응답의 JSON 본문. 503은 SandboxBusy, gone이면 404는 SessionGone.

        그 밖의 응답과 닿지 못한 것은 SandboxDown이다.
        """
        response = await self._call(method, path, body)
        if response.status_code == HTTP_BUSY:
            raise SandboxBusy("실행기가 바쁩니다")
        if gone and response.status_code == HTTP_GONE:
            raise SessionGone("전략 세션이 없습니다")
        if response.status_code != HTTP_OK:
            raise SandboxDown(f"실행기가 오류로 답했습니다(HTTP {response.status_code})")
        try:
            raw: object = response.json()
        except ValueError:
            raise SandboxDown("실행기 응답을 읽지 못했습니다") from None
        return raw

    async def run(self, job: SandboxJob) -> SandboxResult:
        raw = await self._answer("POST", "/run", job_to_json(job))
        result = result_from_json(raw, job)
        if result is None or not _consistent(result, len(job.events)):
            raise SandboxDown("실행기 응답의 모양이 맞지 않습니다")
        return result

    async def open_session(self, job: SessionJob) -> SessionHandle | SessionFailed:
        """띄워 두는 컨테이너를 연다. 코드가 못 뜨면(문법 오류, init 예외 등) SessionFailed."""
        opened = opened_from_json(await self._answer("POST", "/sessions", session_to_json(job)))
        if opened is None:
            raise SandboxDown("실행기 응답의 모양이 맞지 않습니다")
        if isinstance(opened, SessionFailed):
            return opened
        return SessionHandle(opened, replace(job, model=None))

    async def feed(self, handle: SessionHandle, event: StrategyEvent) -> Output | SessionFailed:
        """이벤트 하나를 넣고 출력 하나를 받는다. SessionFailed면 세션은 끝났다(컨테이너는 없다)."""
        path = f"/sessions/{handle.session_id}/events"
        raw = await self._answer("POST", path, event_to_wire(event), gone=True)
        try:
            return fed_from_json(raw, handle.job)
        except BadOutput:
            raise SandboxDown("실행기 응답의 모양이 맞지 않습니다") from None

    async def close(self, handle: SessionHandle) -> None:
        """세션을 닫는다. 이미 없는 세션(404)도 닫힌 것으로 본다."""
        response = await self._call("DELETE", f"/sessions/{handle.session_id}")
        if response.status_code not in (HTTP_OK, HTTP_GONE):
            raise SandboxDown(f"실행기가 오류로 답했습니다(HTTP {response.status_code})")
