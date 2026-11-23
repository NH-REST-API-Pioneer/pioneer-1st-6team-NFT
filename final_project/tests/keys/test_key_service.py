import asyncio
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nh_mcp.domain import UserId
from nh_mcp.keys.service import KeyNotAccepted, KeyStore, KeySummary, Verify
from nh_mcp.keys.vault import KEY_BYTES, Slot, Vault
from nh_mcp.nh.client import Credentials, NoResponse, Rejected
from nh_mcp.store import db, nh_keys, users

NOW = datetime(2026, 12, 1, 1, 0, tzinfo=UTC)
FAKE_KEY = "fake-app-key-1234"
FAKE_SECRET = "fake-app-secret-0000"


async def accepts(_: Credentials) -> None:
    return None


async def rejects(_: Credentials) -> None:
    raise Rejected("EAU025", "잘못된 appkey 또는 appsecret", 400)


async def no_answer(_: Credentials) -> None:
    raise NoResponse("응답을 받지 못했습니다.")


def setup(path: Path | str = ":memory:") -> tuple[sqlite3.Connection, Vault, UserId, UserId]:
    conn = db.connect(path)
    a = users.get_or_create(conn, "g-a", "a@example.com", NOW)
    b = users.get_or_create(conn, "g-b", "b@example.com", NOW)
    return conn, Vault(os.urandom(KEY_BYTES)), a, b


def audit_text(conn: sqlite3.Connection) -> str:
    return " ".join(f"{a} {d}" for a, d in conn.execute("SELECT action, detail FROM audit_log"))


def test_accepted_key_is_encrypted_and_saved(tmp_path: Path) -> None:
    conn, vault, a, _ = setup(tmp_path / "nh.db")
    store = KeyStore(conn, vault, accepts, lambda: NOW)

    summary = asyncio.run(store.register(a, "domestic", Credentials(f" {FAKE_KEY} ", FAKE_SECRET)))

    assert summary == KeySummary("domestic", "1234")
    saved = nh_keys.load(conn, a, "domestic")
    assert saved is not None
    assert vault.unseal(saved.sealed_app_key, Slot(a, "domestic", "app_key")) == FAKE_KEY
    assert store.summaries(a) == [KeySummary("domestic", "1234")]
    conn.close()
    raw = (tmp_path / "nh.db").read_bytes()
    assert FAKE_KEY.encode() not in raw and FAKE_SECRET.encode() not in raw


@pytest.mark.parametrize("verify", [rejects, no_answer])
def test_key_that_fails_the_demo_check_is_not_saved(verify: Verify) -> None:
    conn, vault, a, _ = setup()
    store = KeyStore(conn, vault, verify, lambda: NOW)

    with pytest.raises(KeyNotAccepted):
        asyncio.run(store.register(a, "domestic", Credentials(FAKE_KEY, FAKE_SECRET)))
    assert nh_keys.load(conn, a, "domestic") is None


def test_empty_key_is_not_checked_or_saved() -> None:
    conn, vault, a, _ = setup()
    called: list[Credentials] = []

    async def spy(credentials: Credentials) -> None:
        called.append(credentials)

    with pytest.raises(KeyNotAccepted):
        asyncio.run(
            KeyStore(conn, vault, spy, lambda: NOW).register(a, "domestic", Credentials(" ", "x"))
        )
    assert called == []


def test_delete_removes_only_that_users_keys() -> None:
    conn, vault, a, b = setup()
    store = KeyStore(conn, vault, accepts, lambda: NOW)
    for user in (a, b):
        asyncio.run(store.register(user, "domestic", Credentials(FAKE_KEY, FAKE_SECRET)))

    store.delete_all(a)

    assert store.summaries(a) == []
    assert store.summaries(b) == [KeySummary("domestic", "1234")]


def test_audit_log_never_holds_the_key() -> None:
    conn, vault, a, _ = setup()
    store = KeyStore(conn, vault, accepts, lambda: NOW)
    asyncio.run(store.register(a, "domestic", Credentials(FAKE_KEY, FAKE_SECRET)))
    with pytest.raises(KeyNotAccepted):
        asyncio.run(
            KeyStore(conn, vault, rejects, lambda: NOW).register(
                a, "overseas", Credentials(FAKE_KEY, FAKE_SECRET)
            )
        )
    store.delete_all(a)

    text = audit_text(conn)
    assert "keys.registered" in text and "keys.rejected" in text and "keys.deleted" in text
    assert FAKE_KEY not in text and FAKE_SECRET not in text
