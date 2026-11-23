"""NH 키 등록과 삭제.

모의 서버에서 접근토큰이 발급되는 키만 암호화해 저장한다.
실전 키나 틀린 키는 시험을 통과하지 못하고, 확인하지 못한 경우(응답 없음)도 저장하지 않는다.
감사 로그에는 키를 넣지 않는다.
"""

import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from nh_mcp.domain import Clock, JsonValue, Market, UserId
from nh_mcp.keys.vault import Slot, Vault
from nh_mcp.nh.client import Credentials, NHClient, NHError, Rejected
from nh_mcp.store import audit, nh_keys

type Verify = Callable[[Credentials], Awaitable[None]]


class KeyNotAccepted(Exception):
    """모의 서버에서 이 키로 토큰을 받지 못했거나 확인하지 못했다. 저장하지 않았다."""


@dataclass(frozen=True)
class KeySummary:
    market: Market
    app_key_last4: str


async def verify_with_demo_server(credentials: Credentials, clock: Clock) -> None:
    client = NHClient(credentials, clock)
    try:
        await client.check_credentials()
    finally:
        await client.aclose()


class KeyStore:
    def __init__(
        self, conn: sqlite3.Connection, vault: Vault, verify: Verify, clock: Clock
    ) -> None:
        self._conn = conn
        self._vault = vault
        self._verify = verify
        self._clock = clock

    async def register(
        self, user_id: UserId, market: Market, credentials: Credentials
    ) -> KeySummary:
        app_key, app_secret = credentials.app_key.strip(), credentials.app_secret.strip()
        if not app_key or not app_secret:
            raise KeyNotAccepted("APP Key와 Secret을 모두 넣어 주세요.")
        try:
            await self._verify(Credentials(app_key, app_secret))
        except Rejected as exc:
            self._audit(user_id, "keys.rejected", {"market": market, "code": exc.code})
            raise KeyNotAccepted(
                "모의 서버에서 이 키로 토큰을 받지 못했습니다. 모의 계좌의 키인지 확인해 주세요."
            ) from None
        except NHError:
            self._audit(user_id, "keys.unverified", {"market": market})
            raise KeyNotAccepted(
                "모의 서버에 확인하지 못했습니다. 잠시 뒤 다시 시도해 주세요."
            ) from None

        pair = nh_keys.SealedKeyPair(
            sealed_app_key=self._vault.seal(app_key, Slot(user_id, market, "app_key")),
            sealed_app_secret=self._vault.seal(app_secret, Slot(user_id, market, "app_secret")),
            app_key_last4=app_key[-4:],
        )
        nh_keys.save(self._conn, user_id, market, pair, self._clock())
        self._audit(user_id, "keys.registered", {"market": market, "last4": pair.app_key_last4})
        return KeySummary(market, pair.app_key_last4)

    def summaries(self, user_id: UserId) -> list[KeySummary]:
        result: list[KeySummary] = []
        for market in nh_keys.registered_markets(self._conn, user_id):
            pair = nh_keys.load(self._conn, user_id, market)
            if pair is not None:
                result.append(KeySummary(market, pair.app_key_last4))
        return result

    def delete_all(self, user_id: UserId) -> None:
        nh_keys.delete_all(self._conn, user_id)
        self._audit(user_id, "keys.deleted", {})

    def _audit(self, user_id: UserId, action: str, detail: dict[str, JsonValue]) -> None:
        audit.record(self._conn, user_id, action, detail, self._clock())
