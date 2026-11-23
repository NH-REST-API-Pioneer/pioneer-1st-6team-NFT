import os
import secrets
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from nh_mcp.domain import Market, UserId
from nh_mcp.keys.vault import KEY_BYTES, Slot, Vault
from nh_mcp.store import db, nh_keys, users

NOW = datetime(2026, 12, 1, 1, 0, tzinfo=UTC)


def seal_pair(
    vault: Vault, user_id: UserId, market: Market, app_key: str, secret: str
) -> nh_keys.SealedKeyPair:
    return nh_keys.SealedKeyPair(
        sealed_app_key=vault.seal(app_key, Slot(user_id, market, "app_key")),
        sealed_app_secret=vault.seal(secret, Slot(user_id, market, "app_secret")),
        app_key_last4=app_key[-4:],
    )


def two_users(conn: sqlite3.Connection) -> tuple[UserId, UserId]:
    return (
        users.get_or_create(conn, "google-sub-a", "a@example.com", NOW),
        users.get_or_create(conn, "google-sub-b", "b@example.com", NOW),
    )


def test_db_file_has_no_plaintext_key(tmp_path: Path) -> None:
    app_key, secret = secrets.token_hex(20), secrets.token_hex(32)
    path = tmp_path / "nh.db"
    conn = db.connect(path)
    vault = Vault(os.urandom(KEY_BYTES))
    user_a, _ = two_users(conn)
    nh_keys.save(
        conn, user_a, "domestic", seal_pair(vault, user_a, "domestic", app_key, secret), NOW
    )
    conn.close()

    raw = path.read_bytes()
    assert app_key.encode() not in raw
    assert secret.encode() not in raw
    # 저장이 실제로 일어났는지: 화면에 보여 줄 끝 4자리는 평문으로 남는다.
    assert app_key[-4:].encode() in raw


def test_user_b_cannot_load_user_a_key() -> None:
    conn = db.connect(":memory:")
    vault = Vault(os.urandom(KEY_BYTES))
    user_a, user_b = two_users(conn)
    nh_keys.save(
        conn, user_a, "domestic", seal_pair(vault, user_a, "domestic", "a-key", "a-sec"), NOW
    )

    assert nh_keys.load(conn, user_b, "domestic") is None
    assert nh_keys.registered_markets(conn, user_b) == []
    assert nh_keys.registered_markets(conn, user_a) == ["domestic"]


def test_delete_all_removes_only_that_user() -> None:
    conn = db.connect(":memory:")
    vault = Vault(os.urandom(KEY_BYTES))
    user_a, user_b = two_users(conn)
    for user in (user_a, user_b):
        nh_keys.save(conn, user, "domestic", seal_pair(vault, user, "domestic", "k", "s"), NOW)

    nh_keys.delete_all(conn, user_a)

    assert nh_keys.load(conn, user_a, "domestic") is None
    assert nh_keys.load(conn, user_b, "domestic") is not None


def test_saving_again_replaces_the_key() -> None:
    conn = db.connect(":memory:")
    vault = Vault(os.urandom(KEY_BYTES))
    user_a, _ = two_users(conn)
    nh_keys.save(
        conn, user_a, "domestic", seal_pair(vault, user_a, "domestic", "old-1111", "s"), NOW
    )
    nh_keys.save(
        conn, user_a, "domestic", seal_pair(vault, user_a, "domestic", "new-2222", "s"), NOW
    )

    loaded = nh_keys.load(conn, user_a, "domestic")
    assert loaded is not None
    assert vault.unseal(loaded.sealed_app_key, Slot(user_a, "domestic", "app_key")) == "new-2222"


def test_unknown_or_suspended_user_is_blocked() -> None:
    conn = db.connect(":memory:")
    user_a, _ = two_users(conn)
    assert users.is_suspended(conn, user_a) is False
    users.set_suspended(conn, user_a, True)
    assert users.is_suspended(conn, user_a) is True
    assert users.is_suspended(conn, UserId(999)) is True


def test_get_or_create_is_stable_per_google_account() -> None:
    conn = db.connect(":memory:")
    first = users.get_or_create(conn, "google-sub-a", "a@example.com", NOW)
    again = users.get_or_create(conn, "google-sub-a", "a-new@example.com", NOW)
    assert first == again
