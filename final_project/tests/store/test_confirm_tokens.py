import asyncio
import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from nh_mcp.domain import UserId
from nh_mcp.guard.confirm import judge
from nh_mcp.store import confirm_tokens, db, users
from nh_mcp.store.confirm_tokens import NewToken

NOW = datetime(2026, 12, 1, 1, 0, tzinfo=UTC)
TTL = timedelta(minutes=2)
PAYLOAD = '{"symbol": "A016C000"}'


def two_users(conn: sqlite3.Connection) -> tuple[UserId, UserId]:
    return (
        users.get_or_create(conn, "sub-a", "a@example.com", NOW),
        users.get_or_create(conn, "sub-b", "b@example.com", NOW),
    )


def test_issue_then_find_by_the_original_token() -> None:
    conn = db.connect(":memory:")
    user_a, _ = two_users(conn)

    token, expires_at = confirm_tokens.issue(conn, NewToken(user_a, "order", PAYLOAD), NOW, TTL)
    row = confirm_tokens.find(conn, token)

    assert expires_at == NOW + TTL
    assert row is not None
    assert (row.user_id, row.purpose, row.payload) == (user_a, "order", PAYLOAD)
    assert row.expires_at == NOW + TTL
    assert row.used_at is None


def test_find_returns_timezone_aware_times_that_judge_can_compare() -> None:
    conn = db.connect(":memory:")
    user_a, _ = two_users(conn)
    token, _ = confirm_tokens.issue(conn, NewToken(user_a, "order", PAYLOAD), NOW, TTL)
    row = confirm_tokens.find(conn, token)

    assert row is not None
    assert row.expires_at.tzinfo is not None
    assert judge(row, user_a, "order", NOW) == "usable"
    assert judge(row, user_a, "order", NOW + TTL) == "invalid"


def test_unknown_token_is_not_found() -> None:
    conn = db.connect(":memory:")

    assert confirm_tokens.find(conn, "no-such-token") is None


def test_tokens_differ_each_time() -> None:
    conn = db.connect(":memory:")
    user_a, _ = two_users(conn)

    first, _ = confirm_tokens.issue(conn, NewToken(user_a, "order", PAYLOAD), NOW, TTL)
    second, _ = confirm_tokens.issue(conn, NewToken(user_a, "order", PAYLOAD), NOW, TTL)

    assert first != second


def test_original_token_is_not_stored(tmp_path: Path) -> None:
    path = tmp_path / "nh.db"
    conn = db.connect(path)
    user_a, _ = two_users(conn)
    token, _ = confirm_tokens.issue(conn, NewToken(user_a, "order", PAYLOAD), NOW, TTL)

    hashes = [row[0] for row in conn.execute("SELECT token_hash FROM confirm_tokens")]
    conn.close()

    assert len(hashes) == 1
    assert token not in hashes[0]
    assert len(hashes[0]) == 64
    assert token.encode() not in path.read_bytes()


def test_consume_works_once() -> None:
    conn = db.connect(":memory:")
    user_a, _ = two_users(conn)
    token, _ = confirm_tokens.issue(conn, NewToken(user_a, "order", PAYLOAD), NOW, TTL)

    assert confirm_tokens.consume(conn, token, user_a, "order", NOW) is True
    assert confirm_tokens.consume(conn, token, user_a, "order", NOW) is False
    row = confirm_tokens.find(conn, token)
    assert row is not None
    assert row.used_at == NOW


def test_other_users_token_cannot_be_consumed() -> None:
    conn = db.connect(":memory:")
    user_a, user_b = two_users(conn)
    token, _ = confirm_tokens.issue(conn, NewToken(user_a, "order", PAYLOAD), NOW, TTL)

    assert confirm_tokens.consume(conn, token, user_b, "order", NOW) is False
    # 남이 시도해도 주인의 토큰은 그대로 쓸 수 있다.
    assert confirm_tokens.consume(conn, token, user_a, "order", NOW) is True


def test_other_purpose_cannot_consume() -> None:
    conn = db.connect(":memory:")
    user_a, _ = two_users(conn)
    token, _ = confirm_tokens.issue(conn, NewToken(user_a, "limits", PAYLOAD), NOW, TTL)

    assert confirm_tokens.consume(conn, token, user_a, "order", NOW) is False
    assert confirm_tokens.consume(conn, token, user_a, "limits", NOW) is True


def test_expired_token_cannot_be_consumed() -> None:
    conn = db.connect(":memory:")
    user_a, _ = two_users(conn)
    token, _ = confirm_tokens.issue(conn, NewToken(user_a, "order", PAYLOAD), NOW, TTL)

    assert confirm_tokens.consume(conn, token, user_a, "order", NOW + TTL) is False
    assert confirm_tokens.consume(conn, token, user_a, "order", NOW + TTL - timedelta(seconds=1))


def test_expiry_works_across_different_utc_offsets() -> None:
    conn = db.connect(":memory:")
    user_a, _ = two_users(conn)
    kst = timezone(timedelta(hours=9))
    token, _ = confirm_tokens.issue(
        conn, NewToken(user_a, "order", PAYLOAD), NOW.astimezone(kst), TTL
    )

    assert confirm_tokens.consume(conn, token, user_a, "order", NOW + TTL) is False
    assert confirm_tokens.consume(conn, token, user_a, "order", NOW + timedelta(seconds=30))


def test_unknown_token_cannot_be_consumed() -> None:
    conn = db.connect(":memory:")
    user_a, _ = two_users(conn)

    assert confirm_tokens.consume(conn, "nope", user_a, "order", NOW) is False


def test_two_simultaneous_consumes_succeed_only_once(tmp_path: Path) -> None:
    path = tmp_path / "nh.db"
    conn = db.connect(path)
    user_a, _ = two_users(conn)
    token, _ = confirm_tokens.issue(conn, NewToken(user_a, "order", PAYLOAD), NOW, TTL)
    # 연결 여덟 개가 서로 다른 스레드에서 동시에 같은 토큰을 소진하려 한다.
    others = [db.connect(path) for _ in range(8)]

    async def race() -> list[bool]:
        return list(
            await asyncio.gather(
                *(
                    asyncio.to_thread(confirm_tokens.consume, other, token, user_a, "order", NOW)
                    for other in others
                )
            )
        )

    results = asyncio.run(race())

    assert results.count(True) == 1


def test_strategy_token_round_trips() -> None:
    conn = db.connect(":memory:")
    user_a, _ = two_users(conn)

    token, _ = confirm_tokens.issue(conn, NewToken(user_a, "strategy", PAYLOAD), NOW, TTL)
    row = confirm_tokens.find(conn, token)

    assert row is not None
    assert row.purpose == "strategy"
    assert confirm_tokens.consume(conn, token, user_a, "strategy", NOW) is True


def test_unknown_purpose_reads_as_missing() -> None:
    conn = db.connect(":memory:")
    user_a, _ = two_users(conn)
    # CHECK가 막지 않는 상태(옛·다른 판)에서 들어온 모르는 용도의 행을 흉내 낸다.
    conn.execute("DROP TABLE confirm_tokens")
    conn.execute(
        "CREATE TABLE confirm_tokens (token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, "
        "purpose TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL, "
        "expires_at TEXT NOT NULL, used_at TEXT)"
    )
    conn.execute(
        "INSERT INTO confirm_tokens VALUES (?, ?, 'mystery', '{}', ?, ?, NULL)",
        (hashlib.sha256(b"odd").hexdigest(), user_a, db.stamp(NOW), db.stamp(NOW + TTL)),
    )

    assert confirm_tokens.find(conn, "odd") is None
