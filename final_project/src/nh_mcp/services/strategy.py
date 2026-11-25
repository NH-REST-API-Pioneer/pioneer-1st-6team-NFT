"""실행 승인 미리보기: 전략을 켜도 되는 범위인지 보고, 통과하면 2분짜리 확인 토큰을 발급한다.

검사 순서(하나라도 막히면 거기서 끝): 코드·파라미터 크기·흐름 모양 → 국내 선물 종목 → 한도 있음 →
범위 ≤ 한도 → 횟수·만료 범위 → 모델 파일이 아직 있음 → 같은 사용자·흐름·코드·파라미터·모델의
성공한 백테스트.
토큰에는 흐름과 승인 범위 전체를 담는다. 코드 원문은 넣지 않고 해시만 넣는다
(원문은 `backtest_runs`에 있다).
아직 이 토큰을 쓰는 곳이 없다. M4b의 `start_strategy`가 소진한다.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from nh_mcp.domain import (
    KST,
    Clock,
    Contract,
    JsonValue,
    ModelHash,
    OnLoss,
    RiskLimits,
    StrategyMode,
    StrategyScope,
    Streams,
    UserId,
)
from nh_mcp.guard.risk import LIMITS_NOT_SET
from nh_mcp.markets.domestic import DomesticMarket
from nh_mcp.models.store import ModelStore
from nh_mcp.services.backtest import (
    NO_MODEL,
    STREAM_NAMES,
    StrategyInvalid,
    check_strategy,
    check_streams,
    futures,
    run_key,
    streams_data,
)
from nh_mcp.services.orders import LimitsNotSet, RiskBlocked
from nh_mcp.services.queries import QueryService
from nh_mcp.store import audit, backtests, confirm_tokens, limits
from nh_mcp.store.backtests import RunKey
from nh_mcp.store.confirm_tokens import NewToken

STRATEGY_TTL = timedelta(minutes=2)
MAX_DAILY_ORDERS = 100
MAX_APPROVAL = timedelta(days=7)
SCOPE_EXCEEDS_LIMITS = "scope_exceeds_limits"
NOT_BACKTESTED = "not_backtested"

ON_LOSS_NAMES: dict[OnLoss, str] = {
    "liquidate": "손실 한도에서 정리",
    "hold": "손실 한도에서 유지",
}
MODE_NAMES: dict[StrategyMode, str] = {"auto": "자동 모드", "suggest": "제안 모드"}
AUDIT_ACTION = "strategy.preview"
NO_BACKTEST = (
    "이 코드와 파라미터, 흐름(종목과 minute·day·tick·book), 모델 파일로 성공한 백테스트가 "
    "없습니다. "
    "먼저 같은 값으로 backtest_strategy를 돌리세요."
)
# 요약에 보여 줄 모델 해시 앞자리 수.
MODEL_PREFIX = 12


@dataclass(frozen=True)
class StrategyRequest:
    code: str
    params: dict[str, JsonValue]
    streams: Streams
    scope: StrategyScope
    # 백테스트에 쓴 모델 파일의 해시. 없으면 모델 없는 전략이다.
    model: ModelHash | None = None


@dataclass(frozen=True)
class StrategyPreview:
    code_hash: str
    summary: str
    confirm_token: str
    expires_at: datetime


def _plain(value: Decimal) -> str:
    return format(value, "f")


def _exceeded(scope: StrategyScope, limit: RiskLimits) -> list[str]:
    """한도를 넘은 항목을 사용자 말로."""
    over: list[str] = []
    if scope.quantity > limit.max_order_qty:
        over.append(
            f"quantity {scope.quantity}계약이 1회 주문 한도 {limit.max_order_qty}계약보다 큼"
        )
    if scope.max_position > limit.max_position:
        over.append(
            f"max_position {scope.max_position}계약이 보유 한도 {limit.max_position}계약보다 큼"
        )
    if scope.daily_loss > limit.daily_loss:
        loss, cap = _plain(scope.daily_loss), _plain(limit.daily_loss)
        over.append(f"daily_loss {loss}원이 손실 한도 {cap}원보다 큼")
    if scope.quantity > scope.max_position:
        over.append(f"quantity {scope.quantity}계약이 max_position {scope.max_position}계약보다 큼")
    return over


def _form_problem(scope: StrategyScope) -> str | None:
    """값이 말이 되는 모양이면 None, 아니면 알려 줄 문장."""
    if scope.quantity < 1 or scope.max_position < 1:
        return "quantity와 max_position은 1 이상이어야 합니다."
    if not scope.daily_loss.is_finite() or scope.daily_loss <= 0:
        return "daily_loss는 0보다 커야 합니다."
    if scope.expires_at.utcoffset() is None:
        return "expires_at에는 시간대(예: +09:00)가 있어야 합니다."
    return None


def _range_problem(scope: StrategyScope, now: datetime) -> str | None:
    """횟수와 만료가 허용 범위 안이면 None, 아니면 알려 줄 문장."""
    if not 1 <= scope.daily_orders <= MAX_DAILY_ORDERS:
        return f"daily_orders는 1 이상 {MAX_DAILY_ORDERS} 이하여야 합니다."
    if not now < scope.expires_at <= now + MAX_APPROVAL:
        return "expires_at은 지금 이후 7일 이내여야 합니다."
    return None


def _payload(request: StrategyRequest, key: RunKey) -> str:
    scope = request.scope
    values: dict[str, JsonValue] = {
        "market": "domestic",
        "streams": streams_data(request.streams),
        "code_hash": key.code_hash,
        "params": key.params,
        "model_hash": key.model_hash,
        "scope": {
            "quantity": scope.quantity,
            "max_position": scope.max_position,
            "daily_orders": scope.daily_orders,
            "daily_loss": _plain(scope.daily_loss),
            "expires_at": scope.expires_at.astimezone(UTC).isoformat(),
            "on_loss": scope.on_loss,
            "mode": scope.mode,
        },
    }
    return json.dumps(values, sort_keys=True, ensure_ascii=False)


def _subjects(contracts: list[Contract], streams: Streams) -> str:
    """종목 이름과 흐름. 예: "코스피200 F 202612 1분봉, 미니코스피 F 202612 일봉+1분봉"."""
    return ", ".join(
        c.name + " " + "+".join(STREAM_NAMES[name] for name in sorted(streams[c.symbol]))
        for c in contracts
    )


def _summary(contracts: list[Contract], request: StrategyRequest) -> str:
    """승인 범위를 사람이 읽을 문장으로. 저장하는 값에서 서버가 만든다."""
    scope = request.scope
    until = scope.expires_at.astimezone(KST).strftime("%Y-%m-%d %H:%M")
    model = [] if request.model is None else [f"모델 {request.model[:MODEL_PREFIX]}"]
    return " · ".join(
        [
            _subjects(contracts, request.streams),
            *model,
            f"1회 {scope.quantity}계약",
            f"최대 {scope.max_position}계약",
            f"하루 {scope.daily_orders}회",
            f"일일 손실 {format(scope.daily_loss, ',f')}원",
            f"{until}까지",
            ON_LOSS_NAMES[scope.on_loss],
            MODE_NAMES[scope.mode],
        ]
    )


def _failure(exc: Exception) -> dict[str, JsonValue]:
    """감사 로그에 남길 실패 내용. 예외 메시지는 넣지 않는다."""
    if isinstance(exc, RiskBlocked):
        return {"outcome": "blocked", "rule": exc.rule}
    if isinstance(exc, LimitsNotSet):
        return {"outcome": "blocked", "rule": LIMITS_NOT_SET}
    if isinstance(exc, StrategyInvalid):
        return {"outcome": "invalid"}
    return {"outcome": "error", "error": type(exc).__name__}


class StrategyService:
    def __init__(
        self, conn: sqlite3.Connection, clock: Clock, domestic: DomesticMarket, models: ModelStore
    ) -> None:
        self._conn = conn
        self._clock = clock
        self._queries = QueryService(domestic)
        self._models = models

    async def preview(self, user_id: UserId, request: StrategyRequest) -> StrategyPreview:
        """검사를 통과하면 승인 범위 전체를 담은 확인 토큰을 발급한다. 전략은 켜지 않는다."""
        key = run_key(request.code, request.params, request.streams, request.model)
        detail: dict[str, JsonValue] = {
            "streams": streams_data(request.streams),
            "code_hash": key.code_hash,
            "model_hash": key.model_hash,
        }
        try:
            contracts = await self._check(user_id, request, key)
            new = NewToken(user_id, "strategy", _payload(request, key))
            token, expires_at = confirm_tokens.issue(self._conn, new, self._clock(), STRATEGY_TTL)
        except Exception as exc:
            self._audit(user_id, detail | _failure(exc))
            raise
        self._audit(user_id, detail | {"outcome": "issued"})
        return StrategyPreview(key.code_hash, _summary(contracts, request), token, expires_at)

    async def _check(
        self, user_id: UserId, request: StrategyRequest, key: RunKey
    ) -> list[Contract]:
        """스펙의 검사 순서. 통과하면 종목들(종목코드 순)."""
        problem = (
            check_strategy(request.code, request.params)
            or check_streams(request.streams)
            or _form_problem(request.scope)
        )
        if problem is not None:
            raise StrategyInvalid(problem)
        contracts = await futures(self._queries, request.streams)
        limit = limits.get(self._conn, user_id, "domestic")
        if limit is None:
            raise LimitsNotSet()
        over = _exceeded(request.scope, limit)
        if over:
            raise RiskBlocked(
                SCOPE_EXCEEDS_LIMITS, "승인 범위가 내 한도를 넘습니다: " + "; ".join(over) + "."
            )
        problem = _range_problem(request.scope, self._clock())
        if problem is not None:
            raise StrategyInvalid(problem)
        # 백테스트한 뒤에 지운 모델 파일로는 켤 수 없다(모르면 막는다).
        if request.model is not None and not self._models.exists(user_id, request.model):
            raise StrategyInvalid(NO_MODEL)
        if not backtests.has_success(self._conn, user_id, key):
            raise RiskBlocked(NOT_BACKTESTED, NO_BACKTEST)
        return contracts

    def _audit(self, user_id: UserId, detail: dict[str, JsonValue]) -> None:
        audit.record(self._conn, user_id, AUDIT_ACTION, detail, self._clock())
