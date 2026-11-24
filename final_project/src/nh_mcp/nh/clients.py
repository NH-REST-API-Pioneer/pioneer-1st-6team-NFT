"""사용자 · 시장별 NH 클라이언트. 저장된 키를 푸는 곳은 여기뿐이다.

클라이언트는 (사용자, 시장)으로만 찾아서 다른 사용자의 것이 섞이지 않는다.
토큰을 다시 쓰려고 클라이언트를 들고 있되, 부를 때마다 저장된 키가 그대로인지 본다.
키가 바뀌었거나 지워졌으면 들고 있던 클라이언트를 닫는다.
"""

import sqlite3

import httpx2

from nh_mcp.domain import Clock, Market, UserId
from nh_mcp.keys.vault import Slot, Vault
from nh_mcp.nh.client import Credentials, NHClient
from nh_mcp.store import nh_keys


class KeyNotRegistered(Exception):
    """이 사용자는 이 시장의 NH 키를 등록하지 않았다."""


class ClientPool:
    def __init__(
        self,
        conn: sqlite3.Connection,
        vault: Vault,
        clock: Clock,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._conn = conn
        self._vault = vault
        self._clock = clock
        self._transport = transport
        # (사용자, 시장) → (만들 때의 암호문, 클라이언트)
        self._clients: dict[tuple[UserId, Market], tuple[bytes, NHClient]] = {}

    async def client_for(self, user_id: UserId, market: Market) -> NHClient:
        sealed = nh_keys.load(self._conn, user_id, market)
        held = self._clients.get((user_id, market))
        if held is not None and (sealed is None or held[0] != sealed.sealed_app_key):
            await self._drop(user_id, market)
            held = None
        if sealed is None:
            raise KeyNotRegistered(market)
        if held is not None:
            return held[1]
        credentials = Credentials(
            self._vault.unseal(sealed.sealed_app_key, Slot(user_id, market, "app_key")),
            self._vault.unseal(sealed.sealed_app_secret, Slot(user_id, market, "app_secret")),
        )
        client = NHClient(credentials, self._clock, transport=self._transport)
        self._clients[(user_id, market)] = (sealed.sealed_app_key, client)
        return client

    async def forget(self, user_id: UserId) -> None:
        held: list[Market] = [m for (u, m) in self._clients if u == user_id]
        for market in held:
            await self._drop(user_id, market)

    async def _drop(self, user_id: UserId, market: Market) -> None:
        held = self._clients.pop((user_id, market), None)
        if held is not None:
            await held[1].aclose()
