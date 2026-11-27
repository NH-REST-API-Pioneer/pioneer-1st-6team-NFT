"""사용자 한도. 낮추기는 바로 적용하고, 처음 정하기와 올리기는 사람의 승인을 거친다.

승인을 어떻게 받는지(승인 창, 웹 링크)는 부르는 쪽이 정한다. 여기서는 값이 바뀌어도
같은 승인으로 적용되지 않게 하는 것만 맡는다: 적용은 늘 "내가 본 값이 그대로일 때만"이다.
"""

import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal, cast

from nh_mcp.domain import Clock, JsonValue, Market, OptionLimits, RiskLimits, UserId
from nh_mcp.guard.confirm import judge
from nh_mcp.services.approval import Approval
from nh_mcp.store import audit, confirm_tokens, limits
from nh_mcp.store.confirm_tokens import NewToken
from nh_mcp.store.limits import Change

LINK_TTL = timedelta(minutes=10)
APPROVE_PATH = "/limits/approve"

type SetOutcome = Literal["applied", "declined", "changed", "needs_approval"]
type LinkOutcome = Literal["applied", "invalid", "changed"]


@dataclass(frozen=True)
class LimitPlan:
    """바꾸려는 내용. 만들 때는 아무것도 바꾸지 않는다."""

    needs_approval: bool
    current: RiskLimits | None
    requested: RiskLimits
    message: str
    state: str


@dataclass(frozen=True)
class SetResult:
    outcome: SetOutcome
    current: RiskLimits | None
    requested: RiskLimits
    approval_url: str | None
    expires_at: datetime | None


def _options_json(value: OptionLimits | None) -> dict[str, JsonValue] | None:
    if value is None:
        return None
    return {
        "premium_daily": str(value.premium_daily),
        "max_position": value.max_position,
        "max_short": value.max_short,
    }


def _json(value: RiskLimits | None) -> dict[str, JsonValue] | None:
    if value is None:
        return None
    return {
        "daily_loss": str(value.daily_loss),
        "max_position": value.max_position,
        "max_order_qty": value.max_order_qty,
        "options": _options_json(value.options),
    }


def _payload(market: Market, current: RiskLimits | None, requested: RiskLimits) -> str:
    return json.dumps(
        {"market": market, "current": _json(current), "requested": _json(requested)},
        sort_keys=True,
    )


def _object(raw: object) -> dict[object, object] | None:
    return cast(dict[object, object], raw) if isinstance(raw, dict) else None


def _options_from(raw: object) -> OptionLimits | None:
    """저장된 payload의 옵션 한도. null이거나 칸이 없으면(M5 전) None, 모양이 다르면 ValueError."""
    if raw is None:
        return None
    values = _object(raw)
    if values is None:
        raise ValueError
    premium = values.get("premium_daily")
    position, short = values.get("max_position"), values.get("max_short")
    if not isinstance(premium, str) or type(position) is not int or type(short) is not int:
        raise ValueError
    return OptionLimits(Decimal(premium), position, short)


def _limits_from(raw: object) -> RiskLimits | None:
    """저장된 payload의 한도 객체. 모양이 다르면 None."""
    values = _object(raw)
    if values is None:
        return None
    daily_loss = values.get("daily_loss")
    position, quantity = values.get("max_position"), values.get("max_order_qty")
    if not isinstance(daily_loss, str) or type(position) is not int or type(quantity) is not int:
        return None
    try:
        options = _options_from(values.get("options"))
        return RiskLimits(Decimal(daily_loss), position, quantity, options)
    except (ArithmeticError, ValueError):
        return None


@dataclass(frozen=True)
class _Pending:
    market: Market
    current: RiskLimits | None
    requested: RiskLimits


def _parse(payload: str) -> _Pending | None:
    try:
        values = _object(json.loads(payload))
    except ValueError:
        return None
    if values is None or "current" not in values:
        return None
    market = values.get("market")
    requested = _limits_from(values.get("requested"))
    current = _limits_from(values["current"])
    if requested is None or market not in ("domestic", "overseas"):
        return None
    if current is None and values["current"] is not None:
        return None
    return _Pending("domestic" if market == "domestic" else "overseas", current, requested)


def _option_values(value: OptionLimits | None) -> tuple[str, str, str]:
    if value is None:
        return ("없음", "없음", "없음")
    return (f"{value.premium_daily}원", f"{value.max_position}계약", f"{value.max_short}계약")


def _option_message(current: OptionLimits | None, requested: OptionLimits | None) -> str:
    """승인 문구의 옵션 부분. 양쪽 다 옵션 한도가 없으면 빈 문자열."""
    if current is None and requested is None:
        return ""
    before, after = _option_values(current), _option_values(requested)
    return (
        f", 옵션 하루 프리미엄 {before[0]}→{after[0]}, 옵션 전체 최대 {before[1]}→{after[1]}, "
        f"옵션 매도 최대 {before[2]}→{after[2]}"
    )


def _message(current: RiskLimits | None, requested: RiskLimits) -> str:
    before_loss = "없음" if current is None else str(current.daily_loss)
    before_position = "없음" if current is None else str(current.max_position)
    before_qty = "없음" if current is None else str(current.max_order_qty)
    options = _option_message(None if current is None else current.options, requested.options)
    return (
        f"한도를 바꿉니다. 하루 손실 {before_loss}→{requested.daily_loss}원, "
        f"종목별 최대 {before_position}→{requested.max_position}계약, "
        f"1회 최대 {before_qty}→{requested.max_order_qty}계약{options}. 승인할까요?"
    )


class LimitService:
    def __init__(self, conn: sqlite3.Connection, clock: Clock, approve_base_url: str) -> None:
        self._conn = conn
        self._clock = clock
        self._approve_base_url = approve_base_url.rstrip("/")

    def get(self, user_id: UserId, market: Market) -> RiskLimits | None:
        return limits.get(self._conn, user_id, market)

    def plan(self, user_id: UserId, market: Market, requested: RiskLimits) -> LimitPlan:
        """requested.options가 None이면 지금 옵션 한도를 그대로 둔다. 옵션 한도는 지우지 않는다."""
        current = self.get(user_id, market)
        if requested.options is None and current is not None:
            requested = replace(requested, options=current.options)
        needs_approval = current is None or current.raises(requested)
        return LimitPlan(
            needs_approval=needs_approval,
            current=current,
            requested=requested,
            message=_message(current, requested),
            state=_payload(market, current, requested),
        )

    def commit(
        self, user_id: UserId, market: Market, plan: LimitPlan, approval: Approval | None
    ) -> SetResult:
        """계획을 적용한다. 승인이 필요한데 승인이 없거나 거절이면 적용하지 않는다."""
        if not plan.needs_approval:
            return self._apply(user_id, market, plan, "direct")
        if approval == "approved":
            return self._apply(user_id, market, plan, "window")
        if approval == "unsupported":
            return self._issue_link(user_id, market, plan)
        # 거절, 그리고 답이 없는 경우(있어서는 안 된다)는 모두 적용하지 않는다.
        self._record_approve(user_id, market, plan, "declined", "window")
        return self._finish(user_id, market, plan, "declined")

    def approve_link(self, user_id: UserId, token: str) -> LinkOutcome:
        """웹 승인 링크의 승인. 남의 것·없음·만료·이미 씀은 모두 invalid다."""
        now = self._clock()
        row = confirm_tokens.find(self._conn, token)
        if judge(row, user_id, "limits", now) != "usable" or row is None:
            return "invalid"
        pending = _parse(row.payload)
        if pending is None:
            return "invalid"
        # 소진이 먼저다. 같은 링크를 동시에 두 번 눌러도 한 번만 적용된다.
        if not confirm_tokens.consume(self._conn, token, user_id, "limits", now):
            return "invalid"
        change = Change(pending.current, pending.requested)
        applied = limits.put_if_unchanged(self._conn, user_id, pending.market, change, now)
        outcome: LinkOutcome = "applied" if applied else "changed"
        self._audit_approve(user_id, pending, outcome, "link")
        return outcome

    def reject_link(self, user_id: UserId, token: str) -> bool:
        """웹 승인 링크의 거절. 토큰만 소진하고 한도는 그대로. 쓸 수 없는 토큰이면 False."""
        row = confirm_tokens.find(self._conn, token)
        if judge(row, user_id, "limits", self._clock()) != "usable" or row is None:
            return False
        pending = _parse(row.payload)
        if pending is None:
            return False
        if not confirm_tokens.consume(self._conn, token, user_id, "limits", self._clock()):
            return False
        self._audit_approve(user_id, pending, "rejected", "link")
        return True

    def pending(self, user_id: UserId, token: str) -> tuple[RiskLimits | None, RiskLimits] | None:
        """링크 화면에 보여 줄 (지금 값, 바뀔 값). 토큰을 소진하지 않는다."""
        row = confirm_tokens.find(self._conn, token)
        if judge(row, user_id, "limits", self._clock()) != "usable" or row is None:
            return None
        parsed = _parse(row.payload)
        return None if parsed is None else (parsed.current, parsed.requested)

    def _apply(self, user_id: UserId, market: Market, plan: LimitPlan, method: str) -> SetResult:
        change = Change(plan.current, plan.requested)
        applied = limits.put_if_unchanged(self._conn, user_id, market, change, self._clock())
        outcome: SetOutcome = "applied" if applied else "changed"
        if method == "window":
            self._record_approve(user_id, market, plan, outcome, method)
        return self._finish(user_id, market, plan, outcome, method)

    def _issue_link(self, user_id: UserId, market: Market, plan: LimitPlan) -> SetResult:
        payload = _payload(market, plan.current, plan.requested)
        token, expires_at = confirm_tokens.issue(
            self._conn, NewToken(user_id, "limits", payload), self._clock(), LINK_TTL
        )
        url = f"{self._approve_base_url}/{token}"
        result = self._finish(user_id, market, plan, "needs_approval")
        return replace(result, approval_url=url, expires_at=expires_at)

    def _finish(
        self,
        user_id: UserId,
        market: Market,
        plan: LimitPlan,
        outcome: SetOutcome,
        method: str | None = None,
    ) -> SetResult:
        detail: dict[str, JsonValue] = {
            "market": market,
            "outcome": outcome,
            "method": method,
            "current": _json(plan.current),
            "requested": _json(plan.requested),
        }
        audit.record(self._conn, user_id, "limits.set", detail, self._clock())
        return SetResult(outcome, plan.current, plan.requested, None, None)

    def _record_approve(
        self, user_id: UserId, market: Market, plan: LimitPlan, outcome: str, method: str
    ) -> None:
        self._audit_approve(
            user_id, _Pending(market, plan.current, plan.requested), outcome, method
        )

    def _audit_approve(self, user_id: UserId, pending: _Pending, outcome: str, method: str) -> None:
        detail: dict[str, JsonValue] = {
            "market": pending.market,
            "outcome": outcome,
            "method": method,
            "current": _json(pending.current),
            "requested": _json(pending.requested),
        }
        audit.record(self._conn, user_id, "limits.approve", detail, self._clock())
