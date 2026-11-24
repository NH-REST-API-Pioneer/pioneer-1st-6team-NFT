"""사용자별 요청 속도 제한. 버킷마다 고정 창(기본 1분)에서 정한 횟수까지만 받는다.

메모리에만 둔다(서버 한 대, 프로세스 하나). 재시작하면 초기화된다.
거부된 요청은 세지 않고 창을 늘리지도 않는다. 창은 그 버킷의 첫 요청에서 시작한다.
"""

from collections.abc import Mapping
from datetime import datetime, timedelta
from types import MappingProxyType

from nh_mcp.domain import Clock, UserId

PREVIEW_BUCKET = "preview"
# 주문과 취소는 한 버킷을 같이 쓴다.
ORDER_BUCKET = "order"
LIMITS_BUCKET = "limits"

LIMITS: Mapping[str, int] = MappingProxyType(
    {PREVIEW_BUCKET: 20, ORDER_BUCKET: 10, LIMITS_BUCKET: 10}
)


class RateLimiter:
    def __init__(
        self, clock: Clock, limits: Mapping[str, int], window: timedelta = timedelta(minutes=1)
    ) -> None:
        self._clock = clock
        self._limits = dict(limits)
        self._window = window
        self._counts: dict[tuple[UserId, str], tuple[datetime, int]] = {}

    def allow(self, user_id: UserId, bucket: str) -> bool:
        """받을 수 있으면 세고 True. 이번 창에서 이미 다 썼으면 False. 모르는 버킷은 KeyError."""
        limit = self._limits[bucket]
        now = self._clock()
        key = (user_id, bucket)
        started, count = self._counts.get(key, (now, 0))
        if now - started >= self._window:
            started, count = now, 0
        if count >= limit:
            return False
        self._counts[key] = (started, count + 1)
        return True
