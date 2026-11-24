from datetime import UTC, datetime, timedelta

from nh_mcp.domain import TokenPurpose, TokenRow, UserId
from nh_mcp.guard.confirm import judge

NOW = datetime(2026, 12, 1, 1, 0, tzinfo=UTC)
USER = UserId(1)


def row(
    user_id: UserId = USER,
    purpose: TokenPurpose = "order",
    expires_at: datetime = NOW + timedelta(minutes=5),
    used_at: datetime | None = None,
) -> TokenRow:
    return TokenRow(user_id, purpose, "{}", expires_at, used_at)


def test_usable() -> None:
    assert judge(row(), USER, "order", NOW) == "usable"


def test_missing_is_invalid() -> None:
    assert judge(None, USER, "order", NOW) == "invalid"


def test_other_user_is_invalid() -> None:
    assert judge(row(user_id=UserId(2)), USER, "order", NOW) == "invalid"


def test_other_purpose_is_invalid() -> None:
    assert judge(row(purpose="limits"), USER, "order", NOW) == "invalid"


def test_used_is_invalid() -> None:
    assert judge(row(used_at=NOW), USER, "order", NOW) == "invalid"


def test_expired_at_boundary_is_invalid() -> None:
    assert judge(row(expires_at=NOW), USER, "order", NOW) == "invalid"


def test_one_second_before_expiry_is_usable() -> None:
    assert judge(row(expires_at=NOW + timedelta(seconds=1)), USER, "order", NOW) == "usable"
