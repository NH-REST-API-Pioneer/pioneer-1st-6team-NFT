"""연결 상태 보기와 연결 끊기. 키 값은 돌려주지 않는다."""

from dataclasses import dataclass

from nh_mcp.domain import JsonValue, Market, UserId
from nh_mcp.keys.service import KeyStore
from nh_mcp.nh.clients import ClientPool

MARKETS: tuple[Market, ...] = ("domestic", "overseas")


@dataclass(frozen=True)
class ConnectionService:
    store: KeyStore
    pool: ClientPool
    keys_url: str

    def status(self, user_id: UserId) -> dict[str, JsonValue]:
        registered = {key.market: key.app_key_last4 for key in self.store.summaries(user_id)}
        markets: dict[str, JsonValue] = {
            market: (
                {"registered": True, "app_key_last4": registered[market]}
                if market in registered
                else {"registered": False}
            )
            for market in MARKETS
        }
        return {"markets": markets, "keys_url": self.keys_url}

    async def disconnect(self, user_id: UserId) -> None:
        """저장된 키와 들고 있던 NH 클라이언트(접근토큰)를 함께 없앤다."""
        self.store.delete_all(user_id)
        await self.pool.forget(user_id)
