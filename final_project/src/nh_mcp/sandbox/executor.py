"""전략 실행기. Docker 소켓을 가진 유일한 프로세스이고, 입구는 `POST /run` 하나다.

여기서 지키는 것:
1. 공유 토큰이 맞지 않으면 아무것도 하지 않는다.
2. 본문은 코드·params·봉 세 필드만 받는다. 이미지, 한도, 인자 같은 값은 받지 않는다.
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
from typing import cast

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from nh_mcp.domain import JsonValue
from nh_mcp.sandbox.container import remove_leftovers, run_in_container
from nh_mcp.sandbox.wire import TOKEN_HEADER, Reply, WireBar, reply_to_json

MAX_CONCURRENT = 3
MAX_BODY = 2_097_152
MAX_BARS = 2000
PORT = 8100
MIN_TOKEN_LENGTH = 32

type RunFn = Callable[[str, dict[str, JsonValue], list[WireBar]], Awaitable[Reply]]

_LOG = logging.getLogger(__name__)
_BODY_KEYS = frozenset({"code", "params", "bars"})
_BAR_KEYS = frozenset({"at", "open", "high", "low", "close", "volume"})
_PRICE_KEYS = ("at", "open", "high", "low", "close")


def _bar(raw: object) -> WireBar | None:
    """봉 하나를 검사한다. 키가 정확히 여섯 개, 시각·가격은 문자열, 거래량은 정수 또는 null."""
    if not isinstance(raw, dict):
        return None
    item = cast(dict[str, object], raw)
    if frozenset(item) != _BAR_KEYS:
        return None
    volume = item["volume"]
    if isinstance(volume, bool) or not (volume is None or isinstance(volume, int)):
        return None
    texts: dict[str, str] = {}
    for key in _PRICE_KEYS:
        value = item[key]
        if not isinstance(value, str):
            return None
        texts[key] = value
    return {**texts, "volume": volume}


def _job(raw: object) -> tuple[str, dict[str, JsonValue], list[WireBar]] | None:
    if not isinstance(raw, dict):
        return None
    body = cast(dict[str, object], raw)
    if frozenset(body) != _BODY_KEYS:
        return None
    code, params, raw_bars = body["code"], body["params"], body["bars"]
    if not isinstance(code, str) or not isinstance(params, dict):
        return None
    if not isinstance(raw_bars, list) or len(cast(list[object], raw_bars)) > MAX_BARS:
        return None
    wire: list[WireBar] = []
    for item in cast(list[object], raw_bars):
        bar = _bar(item)
        if bar is None:
            return None
        wire.append(bar)
    return code, cast(dict[str, JsonValue], params), wire


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


async def _parse(request: Request) -> tuple[str, dict[str, JsonValue], list[WireBar]] | Response:
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
            reply = await run(*job)
        except Exception:
            # 컨테이너를 못 띄운 경우 등. 내용은 로그에만 남기고 응답에는 싣지 않는다.
            _LOG.exception("전략 실행 실패")
            return JSONResponse({"status": "error"}, status_code=500)
        finally:
            running -= 1
        return JSONResponse(reply_to_json(reply))

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
