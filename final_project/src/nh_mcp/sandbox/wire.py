"""MCP 서버와 실행기가 HTTP로 주고받는 모양. 가격은 문자열, 시각은 KST ISO 초 단위다.

MCP 서버(클라이언트)와 실행기가 같이 쓰는 상수·타입은 여기 둔다. 클라이언트가 docker를 띄우는
container.py나 실행기 앱(executor.py)을 import하지 않게 하려는 것이다.
"""

from dataclasses import dataclass
from typing import Literal, cast

from nh_mcp.domain import (
    KST,
    Candle,
    JsonValue,
    SandboxFailure,
    SandboxResult,
    SignalName,
)

TOKEN_HEADER = "x-sandbox-token"
MAX_DETAIL = 500

# 실행기가 하네스에 보내는 봉. 가격은 문자열, 시각은 ISO 문자열이다.
type WireBar = dict[str, str | int | None]


@dataclass(frozen=True)
class Reply:
    """컨테이너 실행 결과. 실패하면 signals는 비고, bar_index는 문제가 난 봉(모르면 None)."""

    status: Literal["ok"] | SandboxFailure
    signals: list[SignalName]
    bar_index: int | None
    detail: str


_REPLY_KEYS = frozenset({"status", "signals", "bar_index", "detail"})


def bar_to_wire(c: Candle) -> WireBar:
    """봉을 실행기가 받는 모양으로 바꾼다. 가격을 float로 거치지 않는다."""
    return {
        "at": c.at.astimezone(KST).isoformat(timespec="seconds"),
        "open": format(c.open, "f"),
        "high": format(c.high, "f"),
        "low": format(c.low, "f"),
        "close": format(c.close, "f"),
        "volume": c.volume,
    }


def reply_to_json(r: Reply) -> dict[str, JsonValue]:
    # list는 불변(invariant)이라 list[SignalName]을 list[JsonValue]로 바로 못 넘긴다.
    signals: list[JsonValue] = [*r.signals]
    return {"status": r.status, "signals": signals, "bar_index": r.bar_index, "detail": r.detail}


def _signals(raw: object) -> list[SignalName] | None:
    if not isinstance(raw, list):
        return None
    checked: list[SignalName] = []
    for item in cast(list[object], raw):
        if not isinstance(item, str):
            return None
        match item:
            case "buy" | "sell" | "exit" | "none" as signal:
                checked.append(signal)
            case _:
                return None
    return checked


def _status(raw: object) -> Literal["ok"] | SandboxFailure | None:
    if not isinstance(raw, str):
        return None
    match raw:
        case (
            "ok"
            | "invalid_code"
            | "invalid_signal"
            | "exception"
            | "cpu"
            | "memory"
            | "processes"
            | "output"
            | "timeout" as status
        ):
            return status
        case _:
            return None


def result_from_json(raw: object) -> SandboxResult | None:
    """실행기 응답 본문을 결과로 바꾼다. 모양이 틀리면 None."""
    if not isinstance(raw, dict):
        return None
    body = cast(dict[str, object], raw)
    if frozenset(body) != _REPLY_KEYS:
        return None
    status, signals = _status(body["status"]), _signals(body["signals"])
    index, detail = body["bar_index"], body["detail"]
    if status is None or signals is None or not isinstance(detail, str):
        return None
    if isinstance(index, bool) or not (index is None or isinstance(index, int)):
        return None
    return SandboxResult(status, signals, index, detail)
