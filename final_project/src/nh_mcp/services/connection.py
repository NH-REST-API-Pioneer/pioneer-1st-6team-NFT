"""연결 상태 보기와 연결 끊기. 키 값은 돌려주지 않는다."""

from dataclasses import dataclass

from nh_mcp.domain import JsonValue, Market, UserId
from nh_mcp.keys.service import KeyStore
from nh_mcp.nh.clients import ClientPool
from nh_mcp.services.recording import RecordingService

MARKETS: tuple[Market, ...] = ("domestic", "overseas")


@dataclass(frozen=True)
class ConnectionService:
    store: KeyStore
    pool: ClientPool
    recording: RecordingService
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
        """저장된 키, 들고 있던 NH 클라이언트(접근토큰), 틱 저장 설정을 함께 없앤다.

        저장을 먼저 멈춘다(열린 연결로 구독 해제를 보내고 닫는다). 설정을 지우므로 나중에 새 키를
        넣어도 저절로 다시 켜지지 않는다. 저장한 틱은 보관 기간까지 남는다.
        도구(disconnect_account)와 키 화면의 지우기가 같이 쓴다.
        """
        await self.recording.stop(user_id)
        self.store.delete_all(user_id)
        await self.pool.forget(user_id)
