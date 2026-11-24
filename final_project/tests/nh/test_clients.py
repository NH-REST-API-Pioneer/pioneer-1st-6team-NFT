import asyncio
from datetime import UTC, datetime

import pytest

from nh_mcp.domain import UserId
from nh_mcp.keys.service import KeyStore
from nh_mcp.keys.vault import Vault
from nh_mcp.nh.client import Credentials
from nh_mcp.nh.clients import ClientPool, KeyNotRegistered
from nh_mcp.store import db, users

NOW = datetime(2026, 12, 1, 1, 0, tzinfo=UTC)


async def accept(_: Credentials) -> None:
    return None


class Setup:
    def __init__(self) -> None:
        self.conn = db.connect(":memory:")
        vault = Vault(bytes(32))
        self.store = KeyStore(self.conn, vault, accept, lambda: NOW)
        self.pool = ClientPool(self.conn, vault, lambda: NOW)
        self.a = users.get_or_create(self.conn, "g-a", "a@example.com", NOW)
        self.b = users.get_or_create(self.conn, "g-b", "b@example.com", NOW)

    def register(self, user: UserId, key: str) -> None:
        asyncio.run(self.store.register(user, "domestic", Credentials(key, "fake-secret-0000")))


def test_no_key_means_key_not_registered() -> None:
    s = Setup()
    with pytest.raises(KeyNotRegistered):
        asyncio.run(s.pool.client_for(s.a, "domestic"))


def test_same_user_and_market_reuses_the_client() -> None:
    s = Setup()
    s.register(s.a, "fake-key-aaaa")

    async def scenario() -> bool:
        return await s.pool.client_for(s.a, "domestic") is await s.pool.client_for(s.a, "domestic")

    assert asyncio.run(scenario())


def test_users_never_share_a_client() -> None:
    s = Setup()
    s.register(s.a, "fake-key-aaaa")
    with pytest.raises(KeyNotRegistered):
        asyncio.run(s.pool.client_for(s.b, "domestic"))
    s.register(s.b, "fake-key-bbbb")

    async def scenario() -> bool:
        return await s.pool.client_for(s.a, "domestic") is not await s.pool.client_for(
            s.b, "domestic"
        )

    assert asyncio.run(scenario())


def test_replaced_or_deleted_key_drops_the_old_client() -> None:
    s = Setup()
    s.register(s.a, "fake-key-aaaa")

    async def first() -> object:
        return await s.pool.client_for(s.a, "domestic")

    old = asyncio.run(first())
    s.register(s.a, "fake-key-cccc")
    assert asyncio.run(first()) is not old

    s.store.delete_all(s.a)
    with pytest.raises(KeyNotRegistered):
        asyncio.run(first())
