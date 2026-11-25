"""NH 실시간 메시지 만들기와 읽기. 순수 함수만 둔다(연결은 session.py).

근거: `docs/nh-api.md` "실시간"(Q8, 2026-11-25 모의 서버에 직접 연결해 확인).
- 응답은 `code` 칸(`INIT`, `RT_INIT`, `RT_FAIL`, `pingpong`), 데이터는 `header.action_code`.
- `KA` 구독은 야간 체결을 머리말 `KC`로도 준다. 주간(`KA`·`KB`)만 남기고 나머지는 버린다.
- `KA`에서 `tick_div`가 1이 아닌 행은 체결이 아니다(체결량 0인 기타 데이터).
- 시각은 `exch_tm`(HHMMSS)뿐이다. 날짜는 받는 쪽이 붙인다.
"""

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal, cast

from nh_mcp.store.ticks import LEVELS, PRICE_SCALE, Cell, RecordStream, Sub

STREAM_PATH = "/trade/ws-stream"
CODES: dict[RecordStream, str] = {"tick": "KA", "book": "KB"}
STREAMS: dict[str, RecordStream] = {code: stream for stream, code in CODES.items()}
INIT_OK = "INIT_SUCCESS"
_HOURS, _MINUTES = 24, 60

type Skip = Literal["night", "not_trade", "unknown", "malformed"]


@dataclass(frozen=True)
class Pingpong:
    """받은 그대로 돌려보내야 한다. 안 보내면 NH가 세션을 끝낸다."""


@dataclass(frozen=True)
class InitReply:
    ok: bool


@dataclass(frozen=True)
class SubReply:
    """구독(A)·해제(D) 응답. 해제도 RT_INIT으로 온다."""

    sub: Sub | None
    action: str
    ok: bool
    message: str


@dataclass(frozen=True)
class Data:
    """저장할 행 하나. values는 seq를 뺀 칸(`store/ticks.py`의 칸 순서에서 seq 다음부터)."""

    sub: Sub
    values: tuple[Cell, ...]


@dataclass(frozen=True)
class Skipped:
    reason: Skip


type Incoming = Pingpong | InitReply | SubReply | Data | Skipped


class Malformed(ValueError):
    """칸이 비었거나 숫자가 아니다."""


def stream_url(base: str, access_key: str) -> str:
    """연결 주소. 웹소켓 키가 들어 있으므로 로그에 남기지 않는다."""
    return f"{base}{STREAM_PATH}?access_key={access_key}"


def session_init(token: str) -> str:
    """연결 직후 보내는 첫 메시지. 접근토큰이 들어 있으므로 로그에 남기지 않는다."""
    return json.dumps(
        {
            "header": {"api_ver": "1", "action": "0"},
            "body": {"action_code": "session_init", "session_token": token},
        }
    )


def subscription(sub: Sub, on: bool) -> str:
    return json.dumps(
        {
            "header": {"api_ver": "1", "action": "A" if on else "D"},
            "body": {"action_code": CODES[sub.stream], "action_sym": sub.symbol},
        }
    )


def parse(raw: str | bytes) -> Incoming:
    try:
        loaded: object = json.loads(raw)
    except ValueError:
        return Skipped("malformed")
    if not isinstance(loaded, dict):
        return Skipped("malformed")
    message = cast(dict[str, object], loaded)
    header = message.get("header")
    if isinstance(header, dict):
        return _data(cast(dict[str, object], header), message.get("body"))
    return _reply(message)


def _reply(message: dict[str, object]) -> Incoming:
    """머리말이 없는 메시지는 요청에 대한 응답이다(명세)."""
    code = message.get("code")
    if code == "pingpong":
        return Pingpong()
    if code == "INIT":
        return InitReply(message.get("msg") == INIT_OK)
    if code in ("RT_INIT", "RT_FAIL"):
        return _sub_reply(code == "RT_INIT", message)
    return Skipped("unknown")


def _sub_reply(ok: bool, message: dict[str, object]) -> SubReply:
    data = message.get("data")
    fields = cast(dict[str, object], data) if isinstance(data, dict) else {}
    stream = STREAMS.get(str(fields.get("action_code", "")))
    symbol = fields.get("action_sym")
    sub = Sub(symbol, stream) if stream is not None and isinstance(symbol, str) else None
    return SubReply(sub, str(fields.get("action", "")), ok, str(message.get("msg", "")))


def _data(header: dict[str, object], body: object) -> Incoming:
    code, symbol = header.get("action_code"), header.get("action_sym")
    if code == "KC":
        return Skipped("night")
    stream = STREAMS.get(str(code))
    if stream is None:
        return Skipped("unknown")
    if not isinstance(symbol, str) or not isinstance(body, dict):
        return Skipped("malformed")
    fields = cast(dict[str, object], body)
    if stream == "tick" and fields.get("tick_div") != "1":
        return Skipped("not_trade")
    try:
        values = _trade(fields) if stream == "tick" else _book(fields)
    except Malformed:
        return Skipped("malformed")
    return Data(Sub(symbol, stream), values)


def _trade(f: dict[str, object]) -> tuple[Cell, ...]:
    return (
        _seconds(f.get("exch_tm")),
        _required(_price(f.get("last_pric"))),
        _required(_count(f.get("exec_qty"))),
        _price(f.get("bid_best_pric")),
        _price(f.get("ask_best_pric")),
        _count(f.get("exec_div")),
        _count(f.get("cum_trd_qty")),
    )


def _book(f: dict[str, object]) -> tuple[Cell, ...]:
    values: list[Cell] = [
        _seconds(f.get("exch_tm")),
        _count(f.get("ask_qty")),
        _count(f.get("ask_cnt")),
        _count(f.get("bid_qty")),
        _count(f.get("bid_cnt")),
    ]
    for level in range(1, LEVELS + 1):
        for side in ("ask", "bid"):
            values.append(_price(f.get(f"{side}_pric_{level}")))
            values.append(_count(f.get(f"{side}_qty_{level}")))
            values.append(_count(f.get(f"{side}_cnt_{level}")))
    return tuple(values)


def _required(cell: Cell) -> int:
    if cell is None:
        raise Malformed
    return cell


def _seconds(raw: object) -> int:
    if not isinstance(raw, str) or len(raw) != len("HHMMSS") or not raw.isdigit():
        raise Malformed
    hours, minutes, seconds = int(raw[:2]), int(raw[2:4]), int(raw[4:])
    if hours >= _HOURS or minutes >= _MINUTES or seconds >= _MINUTES:
        raise Malformed
    return hours * 3600 + minutes * 60 + seconds


def _count(raw: object) -> Cell:
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        raise Malformed
    try:
        return int(raw)
    except ValueError:
        raise Malformed from None


def _price(raw: object) -> Cell:
    """가격 문자열 → PRICE_SCALE자리 정수. 자리가 더 많으면 반올림하지 않고 버린다(Malformed)."""
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        raise Malformed
    try:
        scaled = Decimal(raw).scaleb(PRICE_SCALE)
    except InvalidOperation:
        raise Malformed from None
    if not scaled.is_finite() or scaled != scaled.to_integral_value():
        raise Malformed
    return int(scaled)
