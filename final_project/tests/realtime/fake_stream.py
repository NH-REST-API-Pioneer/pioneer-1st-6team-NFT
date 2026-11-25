"""NH 실시간 서버 흉내(127.0.0.1에서 `websockets.serve`). 받은 메시지를 연결마다 기록한다."""

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from typing import cast
from urllib.parse import parse_qs, urlsplit

from websockets.asyncio.server import Server, ServerConnection, serve

from nh_mcp.config import NH_STREAM_URL
from nh_mcp.nh.client import StreamLogin
from nh_mcp.realtime.protocol import STREAM_PATH
from nh_mcp.realtime.session import Socket, open_socket
from nh_mcp.store.ticks import LEVELS

# 실제 키가 아니다. 이 값이 로그·예외·repr에 나오면 안 된다.
CANARY_TOKEN = "fake-session-token-0000"
CANARY_KEY_PREFIX = "fake-ws-key-"
REFUSAL = "예외 발생 허용된 action_code가 아닙니다"
# 가짜 서버의 연결 라이브러리도 DEBUG에서 요청 줄(키)을 적는다. 로그 검사에 서버 쪽 줄이 섞이지 않게
# 막는다.
_SILENT = logging.Logger("tests.fake_stream", level=logging.CRITICAL + 1)


@dataclass(frozen=True)
class Request:
    """클라이언트가 보낸 메시지 하나의 머리말 action과 본문."""

    action: str
    body: dict[str, str]


def _request(text: str) -> Request:
    """머리말이 없는 메시지(되돌려 받은 pingpong 등)는 빈 요청이다."""
    loaded = cast(dict[str, dict[str, str]], json.loads(text))
    if "header" not in loaded:
        return Request("", {})
    return Request(loaded["header"]["action"], loaded["body"])


@dataclass
class Conn:
    """연결 하나: 쓴 웹소켓 키, 받은 메시지, 서버 쪽 소켓."""

    access_key: str
    socket: ServerConnection
    received: list[Request] = field(default_factory=list[Request])
    raw: list[str] = field(default_factory=list[str])

    def sent(self, action: str) -> list[tuple[str, str]]:
        """이 연결로 받은 구독(A)·해제(D) 요청의 (action_code, 종목)."""
        return [
            (r.body["action_code"], r.body["action_sym"])
            for r in self.received
            if r.action == action
        ]


class FakeStream:
    """구독은 RT_INIT으로 받고, refuse에 든 (코드, 종목)만 RT_FAIL로 거절한다."""

    def __init__(self) -> None:
        self.conns: list[Conn] = []
        self.refuse: set[tuple[str, str]] = set()
        self.init_ok = True
        self.refusal = REFUSAL
        self.issued = 0
        self.renews: list[bool] = []
        self.port = 0
        self._server: Server | None = None

    # 로그인 대신: 연결마다 새 웹소켓 키를 준다. 토큰은 늘 같다.
    async def login(self, _user: object, renew: bool) -> StreamLogin:
        self.issued += 1
        self.renews.append(renew)
        return StreamLogin(f"{CANARY_KEY_PREFIX}{self.issued:04d}", CANARY_TOKEN)

    def connect(self, url: str) -> AbstractAsyncContextManager[Socket]:
        """운영 연결 함수를 그대로 쓰되 모의 서버 주소만 이 가짜 서버로 돌린다."""
        assert url.startswith(NH_STREAM_URL + STREAM_PATH + "?access_key=")
        return open_socket(url.replace(NH_STREAM_URL, f"ws://127.0.0.1:{self.port}", 1))

    @property
    def last(self) -> Conn:
        return self.conns[-1]

    async def push(self, message: dict[str, object]) -> None:
        await self.last.socket.send(json.dumps(message))

    async def drop(self) -> None:
        """서버 쪽에서 연결을 끊는다(점검·장애 흉내)."""
        self.last.socket.transport.abort()

    async def _handle(self, socket: ServerConnection) -> None:
        request = socket.request
        assert request is not None
        query = parse_qs(urlsplit(request.path).query)
        conn = Conn(query["access_key"][0], socket)
        self.conns.append(conn)
        async for raw in socket:
            text = raw if isinstance(raw, str) else raw.decode()
            conn.raw.append(text)
            request = _request(text)
            conn.received.append(request)
            code, symbol = request.body.get("action_code"), request.body.get("action_sym", "")
            if code is None:
                continue
            if code == "session_init":
                msg = "INIT_SUCCESS" if self.init_ok else "INIT_FAIL"
                await socket.send(json.dumps({"code": "INIT", "msg": msg}))
                continue
            refused = (code, symbol) in self.refuse
            reply = {
                "code": "RT_FAIL" if refused else "RT_INIT",
                "data": {"action": request.action, "action_code": code, "action_sym": symbol},
                "msg": self.refusal if refused else "",
            }
            await socket.send(json.dumps(reply, ensure_ascii=False))

    @asynccontextmanager
    async def running(self) -> AsyncGenerator["FakeStream"]:
        async with serve(self._handle, "127.0.0.1", 0, logger=_SILENT) as server:
            self._server = server
            self.port = server.sockets[0].getsockname()[1]
            yield self


def trade(symbol: str, tm: str, price: str, qty: str, tick_div: str = "1") -> dict[str, object]:
    return {
        "header": {"action_code": "KA", "action_sym": symbol},
        "body": {
            "sym": symbol,
            "tick_div": tick_div,
            "exch_tm": tm,
            "last_pric": price,
            "exec_qty": qty,
            "bid_best_pric": "726.10",
            "ask_best_pric": "726.15",
            "exec_div": "1",
            "cum_trd_qty": "68092",
            "theo_bas": "1.00",
        },
    }


def book(symbol: str, tm: str, best_ask: int = 72615, code: str = "KB") -> dict[str, object]:
    """10단계 호가. 가격은 1/100 단위 정수로 받아 NH처럼 소수 둘째 자리 문자열로 만든다."""
    body: dict[str, object] = {
        "nght_tp": "KB",
        "sym": symbol,
        "seq_no": "",
        "exch_tm": tm,
        "ask_qty": "1926",
        "ask_cnt": "971",
        "bid_qty": "714",
        "bid_cnt": "454",
    }
    for level in range(1, LEVELS + 1):
        body[f"ask_pric_{level}"] = _price(best_ask + 5 * (level - 1))
        body[f"bid_pric_{level}"] = _price(best_ask - 5 * level)
        body[f"ask_qty_{level}"] = str(level)
        body[f"bid_qty_{level}"] = str(level + 1)
        body[f"ask_cnt_{level}"] = str(level + 2)
        body[f"bid_cnt_{level}"] = str(level + 3)
    return {"header": {"action_code": code, "action_sym": symbol}, "body": body}


def _price(hundredths: int) -> str:
    return f"{hundredths // 100}.{hundredths % 100:02d}"


async def until(check: Callable[[], bool], timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(0.01)
