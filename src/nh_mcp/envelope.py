"""모든 도구가 같은 모양으로 답하는 응답 형식: ok / error / meta."""

from datetime import datetime, timedelta, timezone
from typing import Literal, NotRequired, TypedDict

from nh_mcp.domain import Env

# 한국은 서머타임이 없어 고정 오프셋으로 충분하다.
KST = timezone(timedelta(hours=9))

# float는 넣지 않는다. 가격과 금액은 Decimal을 문자열로 바꿔 담는다.
type JsonValue = str | int | bool | list[JsonValue] | dict[str, JsonValue] | None

type ErrorCode = Literal[
    "INVALID_INPUT",
    "RISK_BLOCKED",
    "CONFIRM_EXPIRED",
    "MARKET_CLOSED",
    "BROKER_REJECTED",
    "UNKNOWN_STATE",
    "KEY_NOT_REGISTERED",
    "LIMITS_NOT_SET",
]


class Meta(TypedDict):
    env: Env
    as_of: str


class ErrorBody(TypedDict):
    code: ErrorCode
    message: str
    retryable: bool
    rule: NotRequired[str]


class Success(TypedDict):
    ok: Literal[True]
    data: dict[str, JsonValue]
    meta: Meta


class Failure(TypedDict):
    ok: Literal[False]
    error: ErrorBody
    meta: Meta


def meta(env: Env, now: datetime) -> Meta:
    return {"env": env, "as_of": now.astimezone(KST).isoformat(timespec="seconds")}


def ok(data: dict[str, JsonValue], meta: Meta) -> Success:
    return {"ok": True, "data": data, "meta": meta}


def error(
    code: ErrorCode,
    message: str,
    meta: Meta,
    *,
    retryable: bool = False,
    rule: str | None = None,
) -> Failure:
    body: ErrorBody = {"code": code, "message": message, "retryable": retryable}
    if rule is not None:
        body["rule"] = rule
    return {"ok": False, "error": body, "meta": meta}
