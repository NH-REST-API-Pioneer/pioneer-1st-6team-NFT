"""MCP 서버가 실행기를 부르는 클라이언트. 토큰은 예외 메시지와 repr에 넣지 않는다."""

import httpx2

from nh_mcp.domain import SandboxJob, SandboxResult
from nh_mcp.sandbox.wire import MAX_DETAIL, TOKEN_HEADER, bar_to_wire, result_from_json

# 실행기 쪽 벽시계(70초)보다 길게 둔다. 컨테이너를 띄우고 지우는 시간이 더 든다.
TIMEOUT_SECONDS = 90.0
HTTP_OK = 200
HTTP_BUSY = 503


class SandboxBusy(Exception):
    """실행기가 동시 한도에 닿아 거절했다. 잠시 뒤 다시 시도하면 된다."""


class SandboxDown(Exception):
    """실행기에 닿지 못했거나 알 수 없는 답이 왔다. 메시지에 토큰을 넣지 않는다."""


def _consistent(result: SandboxResult, bar_count: int) -> bool:
    """내용이 일이 맞는지 본다. 성공이면 봉마다 신호 하나, 실패면 신호 없음."""
    if len(result.detail) > MAX_DETAIL:
        return False
    if result.bar_index is not None and not 0 <= result.bar_index < bar_count:
        return False
    if result.status == "ok":
        return len(result.signals) == bar_count and result.bar_index is None and not result.detail
    return not result.signals


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

    async def run(self, job: SandboxJob) -> SandboxResult:
        payload = {
            "code": job.code,
            "params": job.params,
            "bars": [bar_to_wire(c) for c in job.bars],
        }
        try:
            response = await self._http.post(
                "/run", json=payload, headers={TOKEN_HEADER: self._token}
            )
        except httpx2.HTTPError as exc:
            # 원인 예외는 잇지 않는다. 요청 머리말(토큰)이 따라오면 안 된다.
            raise SandboxDown(f"실행기에 닿지 못했습니다({type(exc).__name__})") from None
        if response.status_code == HTTP_BUSY:
            raise SandboxBusy("실행기가 바쁩니다")
        if response.status_code != HTTP_OK:
            raise SandboxDown(f"실행기가 오류로 답했습니다(HTTP {response.status_code})")
        try:
            raw: object = response.json()
        except ValueError:
            raise SandboxDown("실행기 응답을 읽지 못했습니다") from None
        result = result_from_json(raw)
        if result is None or not _consistent(result, len(job.bars)):
            raise SandboxDown("실행기 응답의 모양이 맞지 않습니다")
        return result
