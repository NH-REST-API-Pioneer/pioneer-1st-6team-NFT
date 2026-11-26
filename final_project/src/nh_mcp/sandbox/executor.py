"""전략 실행기. Docker 소켓을 가진 유일한 프로세스다.

입구는 둘이다. 백테스트는 `POST /run`(실행마다 컨테이너 하나). 자동 실행(M4b)은 띄워 두는 컨테이너의
세션이다.
- `POST /sessions`: 코드·params·흐름·최대 포지션·체결 조건·모델 파일·만료 시각으로 연다. 답은
  {"status": "ok", "session_id"} 또는 {"status": 사유, "detail"}(열지 못했다. 컨테이너는 지웠다).
- `POST /sessions/{id}/events`: 본문은 이벤트 하나(시장 또는 체결). 답은 {"status": "ok", "output"}
  또는 {"status": 사유, "detail"}(세션이 끝났다). 넣는 중인 세션에 또 넣으면 409.
- `DELETE /sessions/{id}`: 닫는다(컨테이너를 지운다).
- `GET /sessions/{id}`: {"state": "idle" 또는 "feeding"}.
없는 세션(닫았거나 죽었거나 만료됐다)은 404다. 실행기가 다시 시작하면 세션은 하나도 살아남지 않고
(시작할 때 남은 컨테이너를 모두 지운다) 모든 세션이 404가 된다. MCP 서버는 404를 받으면 그 전략을
멈춘다.

여기서 지키는 것:
1. 공유 토큰이 맞지 않으면 아무것도 하지 않는다(모든 입구).
2. 본문은 정해진 필드만 정확히 받는다. 이미지, 자원 한도, 인자, 경로 같은 값은 받지 않는다. 모델
   파일은 내용(base64)으로 받는다.
3. 백테스트 동시 실행은 3개까지, 살아 있는 세션은 서버 전체 40개까지. 넘으면 줄 세우지 않고 바로
   거절한다.
4. 코드는 읽지도 실행하지도 않는다. 컨테이너 쪽으로 넘기기만 한다.
5. 토큰은 로그와 응답에 쓰지 않는다. 세션 식별자는 추측할 수 없는 값이다.
6. 컨테이너가 하네스를 돌리지 못하면(이미지 없음 등) 전략 사유가 아니라 500으로 답한다.
7. 세션은 만료 시각(최대 7일로 자른다)이 지나면 닫는다. 이벤트를 넣을 때 보고, 30초마다 따로도 본다.
"""

import asyncio
import contextlib
import hmac
import json
import logging
import os
import secrets
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from functools import partial
from typing import Literal, Protocol

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from nh_mcp.domain import (
    KST,
    Clock,
    Output,
    SandboxJob,
    SandboxResult,
    SessionFailed,
    SessionJob,
    StrategyEvent,
)
from nh_mcp.sandbox.container import (
    ResidentSession,
    SessionClosedError,
    remove_leftovers,
    run_in_container,
)
from nh_mcp.sandbox.wire import (
    MAX_MODEL_TEXT,
    TOKEN_HEADER,
    event_from_wire,
    failed_to_json,
    fed_to_json,
    job_from_json,
    result_to_json,
    session_from_json,
)

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
# 살아 있는 세션(띄워 둔 컨테이너) 수의 서버 전체 상한. 여는 중인 것도 센다(사람 승인 2026-11-25).
MAX_SESSIONS = 40
# 세션 수명의 상한. 승인 만료가 이보다 멀면 여기서 자른다.
MAX_LIFETIME = timedelta(days=7)
# 만료된 세션을 닫으러 도는 주기.
SWEEP_SECONDS = 30.0
# 세션을 여는 본문에서 모델 파일을 뺀 부분의 상한. 이벤트가 없어 코드(64KB, JSON 이스케이프로 최대
# 여섯 배)·params(4KB)·종목 5개의 흐름·체결 조건이 넉넉히 들어간다.
MAX_SESSION_JOB_BODY = 1_048_576
MAX_SESSION_BODY = MAX_SESSION_JOB_BODY + MAX_MODEL_TEXT
# 이벤트 하나의 본문 상한. 가장 큰 이벤트(10단계 호가)도 1KB가 안 된다.
MAX_EVENT_BODY = 65_536

type RunFn = Callable[[SandboxJob], Awaitable[SandboxResult]]
# 세션 하나에서 지금 하는 일. 닫힌 세션은 목록에 없다.
type SessionState = Literal["idle", "feeding"]


class Session(Protocol):
    """띄워 둔 컨테이너 하나. 실제로는 container.ResidentSession이다."""

    async def feed(self, event: StrategyEvent) -> Output | SessionFailed: ...

    async def close(self) -> None: ...


type OpenFn = Callable[[SessionJob], Awaitable[Session | SessionFailed]]

_LOG = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(KST)


def _job(raw: object) -> SandboxJob | None:
    job = job_from_json(raw)
    if job is None or len(job.events) > MAX_EVENTS:
        return None
    return job


def _model_text(model: bytes | None) -> int:
    """본문에서 모델 파일(base64, 채움 글자 포함)이 차지한 글자 수."""
    return 0 if model is None else 4 * -(-len(model) // 3)


def _status(status: str, code: int) -> Response:
    return JSONResponse({"status": status}, status_code=code)


async def _read_body(request: Request, cap: int) -> bytes | None:
    """본문을 읽는다. content-length와 실제로 읽은 양을 둘 다 보고, cap을 넘으면 None."""
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > cap):
        return None
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > cap:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


async def _read_json(request: Request, cap: int) -> tuple[bytes, object] | Response:
    """본문을 읽어 JSON으로 푼다. 넘치면 413, 못 풀면 400 응답."""
    raw = await _read_body(request, cap)
    if raw is None:
        return _status("too_large", 413)
    try:
        parsed: object = json.loads(raw)
    except (ValueError, RecursionError):
        return _status("bad_request", 400)
    return raw, parsed


async def _parse(request: Request) -> SandboxJob | Response:
    """본문을 읽고 검사한다. 통과하면 일, 아니면 그대로 돌려줄 오류 응답."""
    read = await _read_json(request, MAX_BODY)
    if isinstance(read, Response):
        return read
    raw, parsed = read
    job = _job(parsed)
    if job is None:
        return _status("bad_request", 400)
    # 모델 파일을 뺀 부분은 예전 상한 그대로다. 늘린 상한은 모델 파일 자리로만 쓴다.
    if len(raw) - _model_text(job.model) > MAX_JOB_BODY:
        return _status("too_large", 413)
    return job


@dataclass
class _Entry:
    session: Session
    # 이벤트 검사용 계약(모델 파일은 뺐다)과 잘라 둔 만료 시각.
    contract: SessionJob
    state: SessionState = "idle"


type Handler = Callable[[Request], Awaitable[Response]]


class _Sessions:
    """세션 입구와 살아 있는 세션 목록. 한 이벤트 루프에서만 쓴다.

    확인과 바꾸기(상한, idle → feeding) 사이에 await가 없게 쓴다.
    """

    def __init__(self, open_session: OpenFn, clock: Clock) -> None:
        self.open_session = open_session
        self.clock = clock
        self.live: dict[str, _Entry] = {}
        # 여는 중인 세션 수. 상한은 이것까지 센다.
        self.opening = 0

    def find(self, session_id: str) -> _Entry | None:
        """만료 전의 세션. 만료됐으면 None(닫기는 부른 쪽이나 주기 청소가 한다)."""
        entry = self.live.get(session_id)
        if entry is None or entry.contract.expires_at <= self.clock():
            return None
        return entry

    async def end(self, session_id: str) -> None:
        """목록에서 빼고 컨테이너를 지운다. 넣는 중이던 요청은 빠진 것을 보고 404로 끝난다."""
        entry = self.live.pop(session_id, None)
        if entry is not None:
            await entry.session.close()

    async def sweep(self) -> None:
        now = self.clock()
        expired = [sid for sid, e in self.live.items() if e.contract.expires_at <= now]
        await asyncio.gather(*(self.end(sid) for sid in expired))

    async def end_all(self) -> None:
        await asyncio.gather(*(self.end(sid) for sid in list(self.live)))

    async def _job(self, request: Request) -> SessionJob | Response:
        read = await _read_json(request, MAX_SESSION_BODY)
        if isinstance(read, Response):
            return read
        raw, parsed = read
        job, now = session_from_json(parsed), self.clock()
        if job is None or job.expires_at <= now:
            return _status("bad_request", 400)
        if len(raw) - _model_text(job.model) > MAX_SESSION_JOB_BODY:
            return _status("too_large", 413)
        return replace(job, expires_at=min(job.expires_at, now + MAX_LIFETIME))

    async def open(self, request: Request) -> Response:
        job = await self._job(request)
        if isinstance(job, Response):
            return job
        if len(self.live) + self.opening >= MAX_SESSIONS:
            return _status("busy", 503)
        self.opening += 1
        try:
            opened = await self.open_session(job)
        except Exception:
            _LOG.exception("전략 세션을 열지 못했습니다")
            return _status("error", 500)
        finally:
            self.opening -= 1
        if isinstance(opened, SessionFailed):
            return JSONResponse(failed_to_json(opened))
        session_id = secrets.token_urlsafe(32)
        self.live[session_id] = _Entry(opened, replace(job, model=None))
        return JSONResponse({"status": "ok", "session_id": session_id})

    async def _target(self, request: Request) -> tuple[str, _Entry, StrategyEvent] | Response:
        """넣을 세션과 이벤트. 본문이 넘치면 413, 세션이 없으면 404, 이벤트가 틀리면 400."""
        session_id: str = request.path_params["session_id"]
        read = await _read_json(request, MAX_EVENT_BODY)
        if isinstance(read, Response):
            return read
        entry = self.find(session_id)
        if entry is None:
            # 만료된 세션이면 여기서 닫는다(주기 청소를 기다리지 않는다).
            await self.end(session_id)
            return _status("gone", 404)
        event = event_from_wire(read[1], entry.contract.streams)
        return _status("bad_request", 400) if event is None else (session_id, entry, event)

    async def _feed(self, session_id: str, entry: _Entry, event: StrategyEvent) -> Response:
        try:
            outcome = await entry.session.feed(event)
        except SessionClosedError:
            return _status("gone", 404)
        except Exception:
            _LOG.exception("전략 세션에 이벤트를 넣지 못했습니다")
            await self.end(session_id)
            return _status("error", 500)
        if self.live.get(session_id) is not entry:
            # 넣는 동안 닫혔다(DELETE·만료). 컨테이너는 이미 없다.
            return _status("gone", 404)
        if isinstance(outcome, SessionFailed):
            # 컨테이너는 세션이 이미 지웠다. 목록에서도 뺀다.
            await self.end(session_id)
        else:
            entry.state = "idle"
        return JSONResponse(fed_to_json(outcome))

    async def feed(self, request: Request) -> Response:
        target = await self._target(request)
        if isinstance(target, Response):
            return target
        session_id, entry, event = target
        if entry.state != "idle":
            return _status("busy", 409)
        entry.state = "feeding"
        return await self._feed(session_id, entry, event)

    async def close(self, request: Request) -> Response:
        session_id: str = request.path_params["session_id"]
        if session_id not in self.live:
            return _status("gone", 404)
        await self.end(session_id)
        return _status("closed", 200)

    async def state(self, request: Request) -> Response:
        entry = self.find(request.path_params["session_id"])
        if entry is None:
            return _status("gone", 404)
        return JSONResponse({"state": entry.state})


async def _sweep_forever(sessions: _Sessions, every: float) -> None:
    while True:
        await asyncio.sleep(every)
        try:
            await sessions.sweep()
        except Exception:
            _LOG.exception("만료된 세션을 닫지 못했습니다")


def make_app(
    token: str,
    run: RunFn,
    open_session: OpenFn,
    clock: Clock = _now,
    sweep_seconds: float = SWEEP_SECONDS,
) -> Starlette:
    expected = token.encode()
    # 줄 세우지 않고 세기만 한다. 확인과 증가 사이에 await가 없어 한 이벤트 루프에서는 안전하다.
    running = 0
    sessions = _Sessions(open_session, clock)

    def guarded(handler: Handler) -> Handler:
        """공유 토큰이 맞을 때만 handler를 부른다."""

        async def checked(request: Request) -> Response:
            sent = request.headers.get(TOKEN_HEADER, "").encode()
            if not hmac.compare_digest(sent, expected):
                return _status("unauthorized", 401)
            return await handler(request)

        return checked

    async def handle(request: Request) -> Response:
        nonlocal running
        job = await _parse(request)
        if isinstance(job, Response):
            return job
        if running >= MAX_CONCURRENT:
            return _status("busy", 503)
        running += 1
        try:
            reply = await run(job)
        except Exception:
            # 컨테이너를 못 띄운 경우 등. 내용은 로그에만 남기고 응답에는 싣지 않는다.
            _LOG.exception("전략 실행 실패")
            return _status("error", 500)
        finally:
            running -= 1
        return JSONResponse(result_to_json(reply))

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncGenerator[None]:
        sweeper = asyncio.create_task(_sweep_forever(sessions, sweep_seconds))
        try:
            yield
        finally:
            sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sweeper
            # 실행기가 내려가면 세션도 끝난다. 컨테이너를 남기지 않는다.
            await sessions.end_all()

    return Starlette(
        routes=[
            Route("/run", guarded(handle), methods=["POST"]),
            Route("/sessions", guarded(sessions.open), methods=["POST"]),
            Route("/sessions/{session_id}/events", guarded(sessions.feed), methods=["POST"]),
            Route("/sessions/{session_id}", guarded(sessions.close), methods=["DELETE"]),
            Route("/sessions/{session_id}", guarded(sessions.state), methods=["GET"]),
        ],
        lifespan=lifespan,
    )


def main() -> None:
    token = os.environ.get("SANDBOX_TOKEN", "")
    if len(token) < MIN_TOKEN_LENGTH:
        raise SystemExit(f"SANDBOX_TOKEN은 {MIN_TOKEN_LENGTH}자 이상이어야 합니다.")
    image = os.environ.get("SANDBOX_IMAGE", "")
    if not image:
        raise SystemExit("SANDBOX_IMAGE(전략 이미지 이름)를 지정해야 합니다.")
    # 지난 실행기가 지우지 못하고 죽었을 수 있다. 남은 전략 컨테이너를 먼저 치운다.
    asyncio.run(remove_leftovers())
    app = make_app(
        token, partial(run_in_container, image=image), partial(ResidentSession.open, image=image)
    )
    # 컨테이너 안에서 도는 내부 서버다. 바깥에는 compose 네트워크로만 열린다.
    uvicorn.run(app, host="0.0.0.0", port=PORT, access_log=False)


if __name__ == "__main__":
    main()
