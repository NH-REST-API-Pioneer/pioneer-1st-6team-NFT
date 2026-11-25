"""전략 실행기. Docker 소켓을 가진 유일한 프로세스이고, 입구는 `POST /run` 하나다.

여기서 지키는 것:
1. 공유 토큰이 맞지 않으면 아무것도 하지 않는다.
2. 본문은 코드·params·흐름·최대 포지션·체결 조건·이벤트·모델 파일 일곱 필드만 받는다. 이미지,
   자원 한도, 인자, 경로 같은 값은 받지 않는다. 모델 파일은 내용(base64)으로 받는다.
3. 동시 실행은 3개까지. 넘으면 줄 세우지 않고 바로 거절한다.
4. 코드는 읽지도 실행하지도 않는다. 컨테이너 쪽으로 넘기기만 한다.
5. 토큰은 로그와 응답에 쓰지 않는다.
6. 컨테이너가 하네스를 돌리지 못하면(이미지 없음 등) 전략 사유가 아니라 500으로 답한다.
"""

import asyncio
import hmac
import json
import logging
import os
from collections.abc import Awaitable, Callable
from functools import partial

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from nh_mcp.domain import SandboxJob, SandboxResult
from nh_mcp.sandbox.container import remove_leftovers, run_in_container
from nh_mcp.sandbox.wire import MAX_MODEL_TEXT, TOKEN_HEADER, job_from_json, result_to_json

MAX_CONCURRENT = 3
# 모델 파일을 뺀 본문 바이트 상한(48MiB). 가장 큰 일은 틱 실행 상한(90,000개)을 모두 10단계 호가로
# 채운 것이다: 5종목, 가격 784.55, 잔량 네 자리, 코드·params를 한도까지 채우면 공백 없이 약 36.7MB,
# 공백을 넣어 약 41.0MB(2026-11-25 잼). 모의 서버의 실제 호가(5단계)는 이벤트 하나에 약 240바이트,
# 체결은 약 136바이트다. 봉 실행의 가장 큰 일(1분봉 25,000개, 약 5MB)도 물론 들어간다.
MAX_JOB_BODY = 50_331_648
# 본문 바이트 상한: 위 상한 + 50MiB 모델 파일의 base64 글자 수(69,905,068) = 120,236,716.
MAX_BODY = MAX_JOB_BODY + MAX_MODEL_TEXT
# 시장 이벤트 수. 체결 이벤트는 실행기가 만들므로 세지 않는다. 백테스트 상한 가운데 큰 쪽(틱 실행
# 90,000, 봉 실행 25,000)과 같다.
MAX_EVENTS = 90_000
PORT = 8100
MIN_TOKEN_LENGTH = 32

type RunFn = Callable[[SandboxJob], Awaitable[SandboxResult]]

_LOG = logging.getLogger(__name__)


def _job(raw: object) -> SandboxJob | None:
    job = job_from_json(raw)
    if job is None or len(job.events) > MAX_EVENTS:
        return None
    return job


def _model_text(job: SandboxJob) -> int:
    """본문에서 모델 파일(base64, 채움 글자 포함)이 차지한 글자 수."""
    return 0 if job.model is None else 4 * -(-len(job.model) // 3)


async def _read_body(request: Request) -> bytes | None:
    """본문을 읽는다. content-length와 실제로 읽은 양을 둘 다 보고, 넘으면 None."""
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > MAX_BODY):
        return None
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BODY:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


async def _parse(request: Request) -> SandboxJob | Response:
    """본문을 읽고 검사한다. 통과하면 일, 아니면 그대로 돌려줄 오류 응답."""
    raw = await _read_body(request)
    if raw is None:
        return JSONResponse({"status": "too_large"}, status_code=413)
    try:
        parsed: object = json.loads(raw)
    except (ValueError, RecursionError):
        return JSONResponse({"status": "bad_request"}, status_code=400)
    job = _job(parsed)
    if job is None:
        return JSONResponse({"status": "bad_request"}, status_code=400)
    # 모델 파일을 뺀 부분은 예전 상한 그대로다. 늘린 상한은 모델 파일 자리로만 쓴다.
    if len(raw) - _model_text(job) > MAX_JOB_BODY:
        return JSONResponse({"status": "too_large"}, status_code=413)
    return job


def make_app(token: str, run: RunFn) -> Starlette:
    expected = token.encode()
    # 줄 세우지 않고 세기만 한다. 확인과 증가 사이에 await가 없어 한 이벤트 루프에서는 안전하다.
    running = 0

    async def handle(request: Request) -> Response:
        nonlocal running
        sent = request.headers.get(TOKEN_HEADER, "").encode()
        if not hmac.compare_digest(sent, expected):
            return JSONResponse({"status": "unauthorized"}, status_code=401)
        job = await _parse(request)
        if isinstance(job, Response):
            return job
        if running >= MAX_CONCURRENT:
            return JSONResponse({"status": "busy"}, status_code=503)
        running += 1
        try:
            reply = await run(job)
        except Exception:
            # 컨테이너를 못 띄운 경우 등. 내용은 로그에만 남기고 응답에는 싣지 않는다.
            _LOG.exception("전략 실행 실패")
            return JSONResponse({"status": "error"}, status_code=500)
        finally:
            running -= 1
        return JSONResponse(result_to_json(reply))

    return Starlette(routes=[Route("/run", handle, methods=["POST"])])


def main() -> None:
    token = os.environ.get("SANDBOX_TOKEN", "")
    if len(token) < MIN_TOKEN_LENGTH:
        raise SystemExit(f"SANDBOX_TOKEN은 {MIN_TOKEN_LENGTH}자 이상이어야 합니다.")
    image = os.environ.get("SANDBOX_IMAGE", "")
    if not image:
        raise SystemExit("SANDBOX_IMAGE(전략 이미지 이름)를 지정해야 합니다.")
    # 지난 실행기가 지우지 못하고 죽었을 수 있다. 남은 전략 컨테이너를 먼저 치운다.
    asyncio.run(remove_leftovers())
    app = make_app(token, partial(run_in_container, image=image))
    # 컨테이너 안에서 도는 내부 서버다. 바깥에는 compose 네트워크로만 열린다.
    uvicorn.run(app, host="0.0.0.0", port=PORT, access_log=False)


if __name__ == "__main__":
    main()
