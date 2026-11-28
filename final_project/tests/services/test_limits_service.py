"""한도 서비스: 바로 적용 / 승인 / 링크 / 그 사이 바뀐 경우."""

import json
import sqlite3
from datetime import timedelta
from decimal import Decimal

import pytest
from fakes import T0, Clock

from nh_mcp.domain import OptionLimits, RiskLimits, TokenPurpose, UserId
from nh_mcp.services.limits import LimitPlan, LimitService
from nh_mcp.store import confirm_tokens, db, limits, users
from nh_mcp.store.confirm_tokens import NewToken

BASE = "http://localhost:8000/limits/approve"
CURRENT = RiskLimits(Decimal("100000"), 3, 2)
LOWER = RiskLimits(Decimal("50000"), 3, 2)
HIGHER = RiskLimits(Decimal("50000"), 5, 2)
OTHER = RiskLimits(Decimal("70000"), 4, 2)
OPTIONS = OptionLimits(Decimal("500000"), 10, 2)
WITH_OPTIONS = RiskLimits(Decimal("100000"), 3, 2, OPTIONS)


class Setup:
    def __init__(self) -> None:
        self.conn: sqlite3.Connection = db.connect(":memory:")
        self.clock = Clock()
        self.service = LimitService(self.conn, self.clock, BASE)
        self.user: UserId = users.get_or_create(self.conn, "sub-a", "a@example.com", T0)
        self.other_user: UserId = users.get_or_create(self.conn, "sub-b", "b@example.com", T0)

    def stored(self, user: UserId | None = None) -> RiskLimits | None:
        return limits.get(self.conn, user or self.user, "domestic")

    def seed(self, value: RiskLimits) -> None:
        limits.put(self.conn, self.user, "domestic", value, T0)

    def actions(self) -> list[tuple[int, str, dict[str, object]]]:
        rows = self.conn.execute("SELECT user_id, action, detail FROM audit_log ORDER BY id")
        return [(int(r[0]), str(r[1]), json.loads(r[2])) for r in rows.fetchall()]

    def link_token(self, plan: LimitPlan) -> str:
        """링크 승인이 필요한 변경을 발급받아 토큰 원문을 돌려준다."""
        result = self.service.commit(self.user, "domestic", plan, "unsupported")
        assert result.approval_url is not None
        return result.approval_url.rsplit("/", 1)[1]


def test_first_time_needs_approval_and_plan_changes_nothing() -> None:
    s = Setup()

    plan = s.service.plan(s.user, "domestic", CURRENT)

    assert plan.needs_approval is True
    assert plan.current is None
    assert plan.requested == CURRENT
    assert s.stored() is None


def test_first_time_message_says_none_for_current_values() -> None:
    plan = Setup().service.plan(Setup().user, "domestic", CURRENT)

    assert plan.message == (
        "한도를 바꿉니다. 하루 손실 없음→100000원, 종목별 최대 없음→3계약, "
        "1회 최대 없음→2계약. 승인할까요?"
    )


def test_message_shows_current_and_new_values() -> None:
    s = Setup()
    s.seed(CURRENT)

    plan = s.service.plan(s.user, "domestic", HIGHER)

    assert plan.message == (
        "한도를 바꿉니다. 하루 손실 100000→50000원, 종목별 최대 3→5계약, "
        "1회 최대 2→2계약. 승인할까요?"
    )


def test_first_time_approved_applies() -> None:
    s = Setup()
    plan = s.service.plan(s.user, "domestic", CURRENT)

    result = s.service.commit(s.user, "domestic", plan, "approved")

    assert result.outcome == "applied"
    assert result.approval_url is None
    assert s.stored() == CURRENT


def test_first_time_declined_stores_nothing() -> None:
    s = Setup()
    plan = s.service.plan(s.user, "domestic", CURRENT)

    result = s.service.commit(s.user, "domestic", plan, "declined")

    assert result.outcome == "declined"
    assert s.stored() is None


def test_first_time_unsupported_issues_a_link_and_stores_nothing() -> None:
    s = Setup()
    plan = s.service.plan(s.user, "domestic", CURRENT)

    result = s.service.commit(s.user, "domestic", plan, "unsupported")

    assert result.outcome == "needs_approval"
    assert result.approval_url is not None
    assert result.approval_url.startswith(BASE + "/")
    assert result.expires_at == T0 + timedelta(minutes=10)
    assert s.stored() is None


def test_missing_answer_for_a_change_that_needs_approval_applies_nothing() -> None:
    s = Setup()
    plan = s.service.plan(s.user, "domestic", CURRENT)

    result = s.service.commit(s.user, "domestic", plan, None)

    assert result.outcome == "declined"
    assert s.stored() is None


def test_lowering_everything_needs_no_approval_and_applies() -> None:
    s = Setup()
    s.seed(CURRENT)

    plan = s.service.plan(s.user, "domestic", LOWER)
    result = s.service.commit(s.user, "domestic", plan, None)

    assert plan.needs_approval is False
    assert result.outcome == "applied"
    assert s.stored() == LOWER


def test_lowering_ignores_a_declined_answer() -> None:
    s = Setup()
    s.seed(CURRENT)
    plan = s.service.plan(s.user, "domestic", LOWER)

    assert s.service.commit(s.user, "domestic", plan, "declined").outcome == "applied"


def test_raising_one_value_needs_approval() -> None:
    s = Setup()
    s.seed(CURRENT)

    assert s.service.plan(s.user, "domestic", HIGHER).needs_approval is True


def test_state_is_deterministic_and_differs_when_anything_differs() -> None:
    s = Setup()
    a = s.service.plan(s.user, "domestic", CURRENT)
    b = s.service.plan(s.user, "domestic", CURRENT)
    c = s.service.plan(s.user, "domestic", HIGHER)
    s.seed(CURRENT)
    d = s.service.plan(s.user, "domestic", CURRENT)

    assert a.state == b.state
    assert len({a.state, c.state, d.state}) == 3


def test_changed_between_plan_and_commit_applies_nothing() -> None:
    s = Setup()
    plan = s.service.plan(s.user, "domestic", CURRENT)
    s.seed(OTHER)

    result = s.service.commit(s.user, "domestic", plan, "approved")

    assert result.outcome == "changed"
    assert s.stored() == OTHER


def test_lowering_that_became_a_raise_meanwhile_is_not_applied() -> None:
    s = Setup()
    s.seed(CURRENT)
    plan = s.service.plan(s.user, "domestic", LOWER)
    s.seed(RiskLimits(Decimal("10000"), 3, 2))

    result = s.service.commit(s.user, "domestic", plan, None)

    assert result.outcome == "changed"
    assert s.stored() == RiskLimits(Decimal("10000"), 3, 2)


def test_removing_option_limits_counts_as_lowering() -> None:
    """옵션 한도가 없으면 늘리는 옵션 주문이 막힐 뿐이라 위험이 줄어든다."""
    assert WITH_OPTIONS.raises(CURRENT) is False
    assert CURRENT.raises(WITH_OPTIONS) is True


def test_message_shows_option_limits_when_either_side_has_them() -> None:
    s = Setup()
    s.seed(WITH_OPTIONS)

    plan = s.service.plan(
        s.user,
        "domestic",
        RiskLimits(Decimal("100000"), 3, 2, OptionLimits(Decimal("700000"), 8, 0)),
    )

    assert plan.message == (
        "한도를 바꿉니다. 하루 손실 100000→100000원, 종목별 최대 3→3계약, "
        "1회 최대 2→2계약, 옵션 하루 프리미엄 500000원→700000원, 옵션 전체 최대 10계약→8계약, "
        "옵션 매도 최대 2계약→0계약. 승인할까요?"
    )


def test_plan_without_options_carries_the_current_option_limits() -> None:
    s = Setup()
    s.seed(WITH_OPTIONS)

    plan = s.service.plan(s.user, "domestic", LOWER)

    assert plan.requested == RiskLimits(Decimal("50000"), 3, 2, OPTIONS)
    assert plan.needs_approval is False
    s.service.commit(s.user, "domestic", plan, None)
    assert s.stored() == RiskLimits(Decimal("50000"), 3, 2, OPTIONS)


def test_limits_are_per_user() -> None:
    s = Setup()
    plan = s.service.plan(s.user, "domestic", CURRENT)
    s.service.commit(s.user, "domestic", plan, "approved")

    assert s.service.get(s.user, "domestic") == CURRENT
    assert s.service.get(s.other_user, "domestic") is None


# --- 링크 ---------------------------------------------------------------------------------


def test_link_token_payload_holds_market_current_and_requested_as_strings() -> None:
    s = Setup()
    s.seed(CURRENT)
    token = s.link_token(s.service.plan(s.user, "domestic", HIGHER))

    row = confirm_tokens.find(s.conn, token)

    assert row is not None
    assert row.purpose == "limits"
    assert json.loads(row.payload) == {
        "market": "domestic",
        "current": {"daily_loss": "100000", "max_position": 3, "max_order_qty": 2, "options": None},
        "requested": {
            "daily_loss": "50000",
            "max_position": 5,
            "max_order_qty": 2,
            "options": None,
        },
    }


def test_link_payload_holds_option_limits_as_strings_and_counts() -> None:
    s = Setup()
    s.seed(CURRENT)
    token = s.link_token(s.service.plan(s.user, "domestic", WITH_OPTIONS))

    row = confirm_tokens.find(s.conn, token)

    assert row is not None
    payload = json.loads(row.payload)
    assert payload["current"]["options"] is None
    assert payload["requested"]["options"] == {
        "premium_daily": "500000",
        "max_position": 10,
        "max_short": 2,
        "max_combo_loss": None,
    }
    assert s.service.pending(s.user, token) == (CURRENT, WITH_OPTIONS)


def test_first_time_link_payload_has_null_current() -> None:
    s = Setup()
    token = s.link_token(s.service.plan(s.user, "domestic", CURRENT))

    row = confirm_tokens.find(s.conn, token)

    assert row is not None
    assert json.loads(row.payload)["current"] is None


def test_pending_shows_the_change_without_consuming_the_token() -> None:
    s = Setup()
    s.seed(CURRENT)
    token = s.link_token(s.service.plan(s.user, "domestic", HIGHER))

    assert s.service.pending(s.user, token) == (CURRENT, HIGHER)
    assert s.service.pending(s.user, token) == (CURRENT, HIGHER)
    assert s.stored() == CURRENT


def test_pending_is_none_for_other_user_unknown_or_expired() -> None:
    s = Setup()
    token = s.link_token(s.service.plan(s.user, "domestic", CURRENT))

    assert s.service.pending(s.other_user, token) is None
    assert s.service.pending(s.user, "no-such-token") is None
    s.clock.now = T0 + timedelta(minutes=10)
    assert s.service.pending(s.user, token) is None


def test_approve_link_applies_first_time_limits() -> None:
    s = Setup()
    token = s.link_token(s.service.plan(s.user, "domestic", CURRENT))

    assert s.service.approve_link(s.user, token) == "applied"
    assert s.stored() == CURRENT


def test_approve_link_applies_a_raise() -> None:
    s = Setup()
    s.seed(CURRENT)
    token = s.link_token(s.service.plan(s.user, "domestic", HIGHER))

    assert s.service.approve_link(s.user, token) == "applied"
    assert s.stored() == HIGHER


def test_other_users_link_is_invalid_and_changes_nothing() -> None:
    s = Setup()
    token = s.link_token(s.service.plan(s.user, "domestic", CURRENT))

    assert s.service.approve_link(s.other_user, token) == "invalid"
    assert s.stored() is None
    assert s.stored(s.other_user) is None
    # 남이 시도했다고 내 토큰이 소진되지는 않는다.
    assert s.service.approve_link(s.user, token) == "applied"


def test_second_use_is_invalid() -> None:
    s = Setup()
    token = s.link_token(s.service.plan(s.user, "domestic", CURRENT))
    s.service.approve_link(s.user, token)
    s.seed(LOWER)

    assert s.service.approve_link(s.user, token) == "invalid"
    assert s.stored() == LOWER


def test_expired_link_is_invalid() -> None:
    s = Setup()
    token = s.link_token(s.service.plan(s.user, "domestic", CURRENT))
    s.clock.now = T0 + timedelta(minutes=10)

    assert s.service.approve_link(s.user, token) == "invalid"
    assert s.stored() is None


def test_unknown_token_is_invalid() -> None:
    assert Setup().service.approve_link(Setup().user, "nope") == "invalid"


@pytest.mark.parametrize("purpose", ["order", "strategy"])
def test_other_purpose_token_is_not_a_limits_link(purpose: TokenPurpose) -> None:
    s = Setup()
    token, _ = confirm_tokens.issue(
        s.conn, NewToken(s.user, purpose, "{}"), T0, timedelta(minutes=2)
    )

    assert s.service.approve_link(s.user, token) == "invalid"
    assert s.service.pending(s.user, token) is None


def test_strategy_token_with_a_limits_payload_is_not_a_limits_link() -> None:
    """한도 내용이 그대로 든 토큰이라도 용도가 전략(strategy)이면 한도를 바꾸지 않는다."""
    s = Setup()
    token = s.link_token(s.service.plan(s.user, "domestic", CURRENT))
    with s.conn:
        s.conn.execute("UPDATE confirm_tokens SET purpose = 'strategy'")

    assert s.service.pending(s.user, token) is None
    assert s.service.approve_link(s.user, token) == "invalid"
    assert s.stored() is None


def test_malformed_payload_is_invalid() -> None:
    s = Setup()
    for payload in ["not json", "{}", json.dumps({"market": "domestic", "current": None})]:
        token, _ = confirm_tokens.issue(
            s.conn, NewToken(s.user, "limits", payload), T0, timedelta(minutes=10)
        )
        assert s.service.approve_link(s.user, token) == "invalid"
        assert s.service.pending(s.user, token) is None
    assert s.stored() is None


def test_link_after_values_changed_elsewhere_is_changed_and_values_stay() -> None:
    s = Setup()
    s.seed(CURRENT)
    token = s.link_token(s.service.plan(s.user, "domestic", HIGHER))
    s.seed(OTHER)

    assert s.service.approve_link(s.user, token) == "changed"
    assert s.stored() == OTHER
    # 소진됐으므로 값이 원래대로 돌아와도 이 링크는 되살아나지 않는다.
    s.seed(CURRENT)
    assert s.service.approve_link(s.user, token) == "invalid"
    assert s.stored() == CURRENT


def test_approve_link_applies_first_time_option_limits() -> None:
    s = Setup()
    s.seed(CURRENT)
    token = s.link_token(s.service.plan(s.user, "domestic", WITH_OPTIONS))

    assert s.service.approve_link(s.user, token) == "applied"
    assert s.stored() == WITH_OPTIONS


def test_link_after_option_limits_changed_elsewhere_is_changed() -> None:
    s = Setup()
    s.seed(CURRENT)
    token = s.link_token(s.service.plan(s.user, "domestic", WITH_OPTIONS))
    meanwhile = RiskLimits(Decimal("100000"), 3, 2, OptionLimits(Decimal("1"), 1, 0))
    s.seed(meanwhile)

    assert s.service.approve_link(s.user, token) == "changed"
    assert s.stored() == meanwhile


@pytest.mark.parametrize(
    "options",
    [
        {"premium_daily": 500000, "max_position": 10, "max_short": 2},
        {"premium_daily": "abc", "max_position": 10, "max_short": 2},
        {"premium_daily": "500000", "max_position": "10", "max_short": 2},
        {"premium_daily": "500000", "max_position": 10},
        "500000",
        {"premium_daily": "500000", "max_position": 10, "max_short": 2, "max_combo_loss": 1},
        {"premium_daily": "500000", "max_position": 10, "max_short": 2, "max_combo_loss": "x"},
    ],
)
def test_malformed_option_limits_in_the_payload_are_invalid(options: object) -> None:
    s = Setup()
    requested = {"daily_loss": "100000", "max_position": 3, "max_order_qty": 2, "options": options}
    payload = json.dumps({"market": "domestic", "current": None, "requested": requested})
    token, _ = confirm_tokens.issue(
        s.conn, NewToken(s.user, "limits", payload), T0, timedelta(minutes=10)
    )

    assert s.service.pending(s.user, token) is None
    assert s.service.approve_link(s.user, token) == "invalid"
    assert s.stored() is None


def test_first_time_link_after_someone_set_limits_is_changed() -> None:
    s = Setup()
    token = s.link_token(s.service.plan(s.user, "domestic", CURRENT))
    s.seed(LOWER)

    assert s.service.approve_link(s.user, token) == "changed"
    assert s.stored() == LOWER


# --- 감사 로그 ----------------------------------------------------------------------------


def test_audit_records_set_and_approve_with_user_and_no_token() -> None:
    s = Setup()
    plan = s.service.plan(s.user, "domestic", CURRENT)
    s.service.commit(s.user, "domestic", plan, "approved")
    plan2 = s.service.plan(s.user, "domestic", HIGHER)
    token = s.link_token(plan2)
    s.service.approve_link(s.user, token)

    rows = s.actions()
    actions = [(user, action) for user, action, _ in rows]

    assert (s.user, "limits.set") in actions
    assert (s.user, "limits.approve") in actions
    assert all(user == s.user for user, _, _ in rows)
    assert token not in json.dumps(rows)
    assert all(token not in json.dumps(detail) for _, _, detail in rows)


def test_audit_detail_names_outcome_and_method() -> None:
    s = Setup()
    plan = s.service.plan(s.user, "domestic", CURRENT)
    s.service.commit(s.user, "domestic", plan, "approved")
    token = s.link_token(s.service.plan(s.user, "domestic", HIGHER))
    s.service.approve_link(s.user, token)

    seen = [(action, d["outcome"], d["method"]) for _, action, d in s.actions()]

    assert seen == [
        ("limits.approve", "applied", "window"),
        ("limits.set", "applied", "window"),
        ("limits.set", "needs_approval", None),
        ("limits.approve", "applied", "link"),
    ]
    first_set = s.actions()[1][2]
    assert first_set["market"] == "domestic"
    assert first_set["requested"] == {
        "daily_loss": "100000",
        "max_position": 3,
        "max_order_qty": 2,
        "options": None,
    }


def test_audit_records_a_declined_window_answer() -> None:
    s = Setup()
    plan = s.service.plan(s.user, "domestic", CURRENT)
    s.service.commit(s.user, "domestic", plan, "declined")

    seen = [(action, d["outcome"]) for _, action, d in s.actions()]

    assert seen == [("limits.approve", "declined"), ("limits.set", "declined")]


# --- 묶음 최대 손실 한도 (M6) -----------------------------------------------------------

COMBO = OptionLimits(Decimal("500000"), 10, 2, Decimal("300000"))
WITH_COMBO = RiskLimits(Decimal("100000"), 3, 2, COMBO)


def _combo(value: str) -> RiskLimits:
    return RiskLimits(
        Decimal("100000"), 3, 2, OptionLimits(Decimal("500000"), 10, 2, Decimal(value))
    )


def test_first_setting_or_raising_the_combo_limit_is_a_raise_and_lowering_is_not() -> None:
    assert WITH_OPTIONS.raises(WITH_COMBO) is True
    assert WITH_COMBO.raises(_combo("300000.01")) is True
    assert WITH_COMBO.raises(_combo("300000")) is False
    assert WITH_COMBO.raises(_combo("1")) is False
    # 지우는 것은 묶음 주문을 막을 뿐이라 낮추기다(도구로는 지울 수 없다).
    assert WITH_COMBO.raises(WITH_OPTIONS) is False


def test_first_combo_limit_needs_approval_and_the_message_names_it() -> None:
    s = Setup()
    s.seed(WITH_OPTIONS)

    plan = s.service.plan(s.user, "domestic", WITH_COMBO)

    assert plan.needs_approval is True
    assert plan.message.endswith(", 묶음 최대 손실 없음→300000원. 승인할까요?")
    assert s.stored() == WITH_OPTIONS


def test_lowering_the_combo_limit_needs_no_approval() -> None:
    s = Setup()
    s.seed(WITH_COMBO)

    plan = s.service.plan(s.user, "domestic", _combo("100000"))
    s.service.commit(s.user, "domestic", plan, "declined")

    assert plan.needs_approval is False
    assert "묶음 최대 손실 300000원→100000원" in plan.message
    assert s.stored() == _combo("100000")


def test_plan_with_options_but_no_combo_limit_keeps_the_current_combo_limit() -> None:
    s = Setup()
    s.seed(WITH_COMBO)

    plan = s.service.plan(
        s.user, "domestic", RiskLimits(Decimal("100000"), 3, 2, OptionLimits(Decimal("1"), 1, 0))
    )

    assert plan.requested.options == OptionLimits(Decimal("1"), 1, 0, Decimal("300000"))
    assert plan.needs_approval is False


def test_approve_link_applies_a_first_combo_limit() -> None:
    s = Setup()
    s.seed(WITH_OPTIONS)
    token = s.link_token(s.service.plan(s.user, "domestic", WITH_COMBO))

    row = confirm_tokens.find(s.conn, token)
    assert row is not None
    assert json.loads(row.payload)["requested"]["options"]["max_combo_loss"] == "300000"
    assert s.service.approve_link(s.user, token) == "applied"
    assert s.stored() == WITH_COMBO


def test_link_payload_from_before_the_combo_limit_reads_it_as_none() -> None:
    """M6 배포 전에 발급한 링크(묶음 칸 없음)도 그대로 승인된다."""
    s = Setup()
    s.seed(CURRENT)
    options = {"premium_daily": "500000", "max_position": 10, "max_short": 2}
    requested = {"daily_loss": "100000", "max_position": 3, "max_order_qty": 2, "options": options}
    current = {"daily_loss": "100000", "max_position": 3, "max_order_qty": 2, "options": None}
    payload = json.dumps({"market": "domestic", "current": current, "requested": requested})
    token, _ = confirm_tokens.issue(
        s.conn, NewToken(s.user, "limits", payload), T0, timedelta(minutes=10)
    )

    assert s.service.pending(s.user, token) == (CURRENT, WITH_OPTIONS)
    assert s.service.approve_link(s.user, token) == "applied"
    assert s.stored() == WITH_OPTIONS
