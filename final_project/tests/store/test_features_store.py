"""기능 묶음 설정: 기본은 꺼짐, 사용자마다 따로, 켜기·끄기는 몇 번 해도 같다."""

from datetime import UTC, datetime, timedelta

from nh_mcp.store import db, features, users

NOW = datetime(2026, 12, 1, 1, 0, tzinfo=UTC)


def test_off_by_default_and_split_by_user() -> None:
    conn = db.connect(":memory:")
    a = users.get_or_create(conn, "g-a", "a@example.com", NOW)
    b = users.get_or_create(conn, "g-b", "b@example.com", NOW)

    assert not features.enabled(conn, a, "auto_run")
    features.turn_on(conn, a, "auto_run", NOW)

    assert features.enabled(conn, a, "auto_run")
    assert not features.enabled(conn, b, "auto_run")
    features.turn_off(conn, b, "auto_run")
    assert features.enabled(conn, a, "auto_run")


def test_turning_on_twice_keeps_the_first_time_and_off_is_off() -> None:
    conn = db.connect(":memory:")
    a = users.get_or_create(conn, "g-a", "a@example.com", NOW)

    features.turn_on(conn, a, "auto_run", NOW)
    features.turn_on(conn, a, "auto_run", NOW + timedelta(hours=1))
    [(enabled_at,)] = conn.execute("SELECT enabled_at FROM user_features").fetchall()
    assert datetime.fromisoformat(enabled_at) == NOW

    features.turn_off(conn, a, "auto_run")
    features.turn_off(conn, a, "auto_run")
    assert not features.enabled(conn, a, "auto_run")
