"""도구들이 함께 쓰는 것: 지금 사용자, 응답 meta, 서비스 예외를 오류 코드로 바꾸기."""

import sqlite3
from collections.abc import Awaitable
from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated

from pydantic import WithJsonSchema

from nh_mcp import envelope
from nh_mcp.auth.access import current_user_id
from nh_mcp.config import ENV
from nh_mcp.domain import Clock, JsonValue, UserId
from nh_mcp.services.backtest import RunnerUnavailable, StrategyFailed, StrategyInvalid
from nh_mcp.services.combos import NotResumable, UnknownCombo
from nh_mcp.services.options import OPTION_SWITCH_RULES, UnknownExpiry
from nh_mcp.services.orders import (
    ConfirmInvalid,
    InvalidCancel,
    LimitsNotSet,
    MarketClosed,
    NotApproved,
    OrdersDisabled,
    RiskBlocked,
    UnknownState,
)
from nh_mcp.services.queries import BrokerRejected, KeyMissing, Unavailable, UnknownSymbol
from nh_mcp.services.strategy import AUTO_RUN_OFF

DOMESTIC_ONLY = "지금은 국내(domestic)만 지원합니다."

# 정수 인자. 스키마는 integer로 알리되 값은 받은 그대로 넘긴다. FastMCP 기본 검사는 느슨해서
# true→1, "1"→1, 1.0→1로 바꿔 버린다. 도구가 `whole`로 진짜 정수인지 보고 INVALID_INPUT을 준다.
type Whole = Annotated[object, WithJsonSchema({"type": "integer"})]
# 주문 스위치가 꺼졌을 때의 규칙 이름. guard.risk.DEMO_ONLY와 같은 값이다(도구는 guard를 못 부른다).
ORDERS_OFF_RULE = "demo_only"
ORDERS_OFF = "지금은 주문이 꺼져 있습니다."
KEY_MISSING = "NH 모의 키가 등록돼 있지 않습니다. 등록 화면에서 등록해 주세요: "
UNAVAILABLE = "NH 모의 서버가 답하지 않습니다. 잠시 뒤 다시 시도해 주세요."
RUNNER_UNAVAILABLE = "전략 실행기가 바쁘거나 답하지 않습니다. 잠시 뒤 다시 시도해 주세요."
# 사용자가 등록 화면의 묶음 스위치를 켜야 풀리는 거절. 문구 끝에 등록 화면 주소를 붙인다.
SWITCH_RULES = frozenset({AUTO_RUN_OFF}) | OPTION_SWITCH_RULES
# 묶음 한도(max_combo_loss)가 없어 묶음 규칙이 막은 것. 한 종목 주문의 LimitsNotSet과 같은 코드로
# 답한다. guard.risk.LIMITS_NOT_SET과 같은 값이다(도구는 guard를 못 부른다).
LIMITS_NOT_SET_RULE = "limits_not_set"
COMBO_LIMIT_HINT = " 사용자와 값을 정한 뒤 set_risk_limits의 options.max_combo_loss로 정하세요."

# 문구가 늘 같은 서비스 예외 → (오류 코드, 문구).
_FIXED: dict[type[Exception], tuple[envelope.ErrorCode, str]] = {
    UnknownSymbol: (
        "INVALID_INPUT",
        "search_symbol(선물)이나 get_option_chain(옵션) 결과에 있는 종목코드만 쓸 수 있습니다.",
    ),
    UnknownExpiry: (
        "INVALID_INPUT",
        "코스피200 월물 옵션 만기가 아닙니다. expiry 없이 get_option_chain을 불러 받은 "
        "expiries에서 고르세요.",
    ),
    LimitsNotSet: (
        "LIMITS_NOT_SET",
        "위험 한도가 없어 주문할 수 없습니다. 옵션 주문은 옵션 한도(set_risk_limits의 options)도 "
        "있어야 합니다. 사용자와 값을 정한 뒤 set_risk_limits로 정하세요.",
    ),
    ConfirmInvalid: (
        "CONFIRM_EXPIRED",
        "확인 토큰이 없거나 만료됐거나 이미 썼습니다. 그 토큰을 준 미리보기(preview_order, "
        "preview_combo, preview_strategy)부터 다시 하세요.",
    ),
    NotApproved: (
        "CONFIRM_EXPIRED",
        "사용자가 승인하지 않아 주문하지 않았습니다. 이 확인 토큰은 더 쓸 수 없습니다.",
    ),
    InvalidCancel: (
        "INVALID_INPUT",
        "오늘 주문 내역에 남은 수량이 있는 그 주문번호가 없습니다. "
        "get_orders로 주문번호를 확인하세요.",
    ),
    UnknownCombo: (
        "INVALID_INPUT",
        "그 combo_id의 묶음이 없습니다. place_combo 결과의 combo_id를 그대로 넣어 주세요.",
    ),
}


@dataclass(frozen=True)
class ToolContext:
    conn: sqlite3.Connection
    clock: Clock
    keys_url: str

    def user_id(self) -> UserId:
        return current_user_id(self.conn)

    def meta(self) -> envelope.Meta:
        return envelope.meta(ENV, self.clock())

    def invalid(self, message: str) -> envelope.Response:
        return envelope.error("INVALID_INPUT", message, self.meta())

    def too_many(self) -> envelope.Response:
        """사용자별 속도 제한에 걸렸다. NH를 부르기 전에 돌려준다."""
        return envelope.error(
            "TEMPORARILY_UNAVAILABLE",
            "요청이 너무 잦습니다. 1분쯤 뒤에 다시 시도해 주세요.",
            self.meta(),
            retryable=True,
        )

    async def answer(self, work: Awaitable[dict[str, JsonValue]]) -> envelope.Response:
        """서비스 결과를 성공 응답으로, 서비스 예외를 오류 코드로 바꾼다."""
        try:
            data = await work
        except Exception as exc:
            return self.failed(exc)
        return envelope.ok(data, self.meta())

    def failed(self, exc: Exception) -> envelope.Response:
        """서비스 예외를 오류 응답으로. 모르는 예외는 그대로 다시 던진다(답으로 덮지 않는다)."""
        meta = self.meta()
        for kind, (code, message) in _FIXED.items():
            if isinstance(exc, kind):
                return envelope.error(code, message, meta)
        match exc:
            case KeyMissing():
                response = envelope.error("KEY_NOT_REGISTERED", KEY_MISSING + self.keys_url, meta)
            case Unavailable():
                response = envelope.error(
                    "TEMPORARILY_UNAVAILABLE", UNAVAILABLE, meta, retryable=True
                )
            case BrokerRejected():
                response = envelope.error("BROKER_REJECTED", f"NH가 거부했습니다: {exc}", meta)
            case RiskBlocked(rule=rule, message=message):
                response = self._blocked(rule, message, meta)
            case NotResumable(message=message):
                response = envelope.error("INVALID_INPUT", message, meta)
            case MarketClosed(message=message):
                response = envelope.error("MARKET_CLOSED", message, meta)
            case UnknownState(message=message):
                response = envelope.error("UNKNOWN_STATE", message, meta)
            case OrdersDisabled():
                response = envelope.error("RISK_BLOCKED", ORDERS_OFF, meta, rule=ORDERS_OFF_RULE)
            case StrategyInvalid() | StrategyFailed() | RunnerUnavailable():
                response = _strategy_error(exc, meta)
            case _:
                raise exc
        return response

    def _blocked(self, rule: str, message: str, meta: envelope.Meta) -> envelope.Response:
        """규칙이 막은 것. 스위치 규칙이면 등록 화면 주소를 붙인다."""
        if rule == LIMITS_NOT_SET_RULE:
            return envelope.error("LIMITS_NOT_SET", message + COMBO_LIMIT_HINT, meta)
        if rule in SWITCH_RULES:
            message = f"{message} 등록 화면: {self.keys_url}"
        return envelope.error("RISK_BLOCKED", message, meta, rule=rule)


def _strategy_error(
    exc: StrategyInvalid | StrategyFailed | RunnerUnavailable, meta: envelope.Meta
) -> envelope.Response:
    """백테스트 서비스 예외를 오류 응답으로. 실패 사유는 rule에 담는다."""
    match exc:
        case StrategyInvalid(message=message):
            return envelope.error("INVALID_INPUT", message, meta)
        case StrategyFailed(reason=reason, message=message):
            return envelope.error("STRATEGY_FAILED", message, meta, rule=reason)
        case RunnerUnavailable():
            return envelope.error(
                "TEMPORARILY_UNAVAILABLE", RUNNER_UNAVAILABLE, meta, retryable=True
            )


def whole(value: object, low: int, high: int) -> int | None:
    """JSON 정수이고 low 이상 high 이하면 그 값. bool, 문자열, 실수는 None."""
    if type(value) is not int or not low <= value <= high:
        return None
    return value


def text(value: Decimal | None) -> str | None:
    """가격과 금액은 문자열로 내보낸다. float로 바꾸지 않는다."""
    return None if value is None else str(value)
