"""모든 도구가 같은 모양으로 답하는 응답 형식: ok / error / meta."""

from datetime import datetime
from typing import Literal, NotRequired, TypedDict

from nh_mcp.domain import KST, Env, JsonValue

type ErrorCode = Literal[
    "INVALID_INPUT",
    "RISK_BLOCKED",
    "CONFIRM_EXPIRED",
    "MARKET_CLOSED",
    "BROKER_REJECTED",
    "UNKNOWN_STATE",
    "KEY_NOT_REGISTERED",
    "LIMITS_NOT_SET",
    # NH나 개발자센터가 잠시 답하지 않는다. 잠시 뒤 다시 시도할 수 있다.
    "TEMPORARILY_UNAVAILABLE",
    # AI가 짠 전략 코드가 예외를 냈거나 자원 제한을 넘었다. 사유는 rule에 있다.
    "STRATEGY_FAILED",
]


class Meta(TypedDict):
    env: Env
    as_of: str


class ErrorBody(TypedDict):
    code: ErrorCode
    message: str
    retryable: bool
    rule: NotRequired[str]


class Response(TypedDict):
    """모든 도구의 응답. 성공이면 data, 실패면 error가 있다.

    성공과 실패를 타입 두 개의 합으로 두면 FastMCP가 결과를 {"result": ...}로 한 번 더 감싸서
    AI가 받는 모양이 달라진다. 그래서 객체 하나로 둔다.
    """

    ok: bool
    meta: Meta
    data: NotRequired[dict[str, JsonValue]]
    error: NotRequired[ErrorBody]


def meta(env: Env, now: datetime) -> Meta:
    return {"env": env, "as_of": now.astimezone(KST).isoformat(timespec="seconds")}


def ok(data: dict[str, JsonValue], meta: Meta) -> Response:
    return {"ok": True, "data": data, "meta": meta}


def error(
    code: ErrorCode,
    message: str,
    meta: Meta,
    *,
    retryable: bool = False,
    rule: str | None = None,
) -> Response:
    body: ErrorBody = {"code": code, "message": message, "retryable": retryable}
    if rule is not None:
        body["rule"] = rule
    return {"ok": False, "error": body, "meta": meta}
