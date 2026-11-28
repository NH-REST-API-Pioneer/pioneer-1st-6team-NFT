import sqlite3
from datetime import UTC, datetime
from decimal import Decimal

from nh_mcp.domain import OptionLimits, RiskLimits, UserId
from nh_mcp.store import db, limits, users
from nh_mcp.store.limits import Change

NOW = datetime(2026, 12, 1, 1, 0, tzinfo=UTC)
A = RiskLimits(daily_loss=Decimal("1000000"), max_position=3, max_order_qty=1)
B = RiskLimits(daily_loss=Decimal("2500000.50"), max_position=5, max_order_qty=2)
A_OPTIONS = RiskLimits(Decimal("1000000"), 3, 1, OptionLimits(Decimal("300000.50"), 6, 0))


def two_users(conn: sqlite3.Connection) -> tuple[UserId, UserId]:
    return (
        users.get_or_create(conn, "sub-a", "a@example.com", NOW),
        users.get_or_create(conn, "sub-b", "b@example.com", NOW),
    )


def test_no_limits_means_none() -> None:
    conn = db.connect(":memory:")
    user_a, _ = two_users(conn)

    assert limits.get(conn, user_a, "domestic") is None


def test_put_then_get_returns_the_same_values() -> None:
    conn = db.connect(":memory:")
    user_a, _ = two_users(conn)

    limits.put(conn, user_a, "domestic", B, NOW)

    assert limits.get(conn, user_a, "domestic") == B
    assert limits.get(conn, user_a, "overseas") is None


def test_put_replaces_the_previous_values() -> None:
    conn = db.connect(":memory:")
    user_a, _ = two_users(conn)

    limits.put(conn, user_a, "domestic", A, NOW)
    limits.put(conn, user_a, "domestic", B, NOW)

    assert limits.get(conn, user_a, "domestic") == B


def test_user_b_cannot_see_user_a_limits() -> None:
    conn = db.connect(":memory:")
    user_a, user_b = two_users(conn)

    limits.put(conn, user_a, "domestic", A, NOW)

    assert limits.get(conn, user_b, "domestic") is None


def test_put_if_unchanged_sets_the_first_value_only_when_none_exists() -> None:
    conn = db.connect(":memory:")
    user_a, _ = two_users(conn)

    assert limits.put_if_unchanged(conn, user_a, "domestic", Change(None, A), NOW) is True
    assert limits.get(conn, user_a, "domestic") == A
    # 이미 값이 있으면 "없을 때만"은 실패하고 값은 그대로다.
    assert limits.put_if_unchanged(conn, user_a, "domestic", Change(None, B), NOW) is False
    assert limits.get(conn, user_a, "domestic") == A


def test_put_if_unchanged_changes_only_when_current_matches_expected() -> None:
    conn = db.connect(":memory:")
    user_a, _ = two_users(conn)
    limits.put(conn, user_a, "domestic", A, NOW)

    assert limits.put_if_unchanged(conn, user_a, "domestic", Change(B, B), NOW) is False
    assert limits.get(conn, user_a, "domestic") == A
    assert limits.put_if_unchanged(conn, user_a, "domestic", Change(A, B), NOW) is True
    assert limits.get(conn, user_a, "domestic") == B


def test_put_if_unchanged_cannot_touch_another_users_limits() -> None:
    conn = db.connect(":memory:")
    user_a, user_b = two_users(conn)
    limits.put(conn, user_a, "domestic", A, NOW)

    assert limits.put_if_unchanged(conn, user_b, "domestic", Change(A, B), NOW) is False
    assert limits.get(conn, user_a, "domestic") == A


def test_equal_decimals_with_different_spelling_count_as_unchanged() -> None:
    conn = db.connect(":memory:")
    user_a, _ = two_users(conn)
    limits.put(conn, user_a, "domestic", A, NOW)
    same_value = RiskLimits(daily_loss=Decimal("1000000.00"), max_position=3, max_order_qty=1)

    assert limits.put_if_unchanged(conn, user_a, "domestic", Change(same_value, B), NOW) is True


def test_option_limits_round_trip_and_can_be_put_back_to_none() -> None:
    conn = db.connect(":memory:")
    user_a, user_b = two_users(conn)

    limits.put(conn, user_a, "domestic", A_OPTIONS, NOW)
    limits.put(conn, user_b, "domestic", A, NOW)

    assert limits.get(conn, user_a, "domestic") == A_OPTIONS
    assert limits.get(conn, user_b, "domestic") == A
    limits.put(conn, user_a, "domestic", A, NOW)
    assert limits.get(conn, user_a, "domestic") == A


def test_put_if_unchanged_sees_a_change_in_option_limits_only() -> None:
    conn = db.connect(":memory:")
    user_a, _ = two_users(conn)
    limits.put(conn, user_a, "domestic", A_OPTIONS, NOW)

    # 선물 칸이 같아도 옵션 칸이 다르면 바뀐 것이다.
    assert limits.put_if_unchanged(conn, user_a, "domestic", Change(A, B), NOW) is False
    assert limits.get(conn, user_a, "domestic") == A_OPTIONS
    assert limits.put_if_unchanged(conn, user_a, "domestic", Change(A_OPTIONS, B), NOW) is True


def test_combo_limit_round_trips_as_decimal_and_alone_changes_the_row() -> None:
    conn = db.connect(":memory:")
    user_a, _ = two_users(conn)
    combo = OptionLimits(Decimal("300000.50"), 6, 0, Decimal("250000.25"))
    with_combo = RiskLimits(Decimal("1000000"), 3, 1, combo)
    limits.put(conn, user_a, "domestic", A_OPTIONS, NOW)

    # 묶음 칸만 달라도 바뀐 것이다.
    assert limits.put_if_unchanged(conn, user_a, "domestic", Change(with_combo, B), NOW) is False
    assert limits.put_if_unchanged(conn, user_a, "domestic", Change(A_OPTIONS, with_combo), NOW)
    stored = limits.get(conn, user_a, "domestic")
    assert stored == with_combo
    assert stored is not None and stored.options is not None
    assert type(stored.options.max_combo_loss) is Decimal
