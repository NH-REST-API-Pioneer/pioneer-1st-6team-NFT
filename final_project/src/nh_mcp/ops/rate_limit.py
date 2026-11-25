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
# 백테스트는 격리 컨테이너를 하나씩 띄운다.
BACKTEST_BUCKET = "backtest"
# 틱 저장 설정을 바꿀 때마다 NH에 웹소켓 키를 새로 받는다.
RECORDING_BUCKET = "recording"
# 버킷마다 횟수를 세는 고정 창.
WINDOW = timedelta(minutes=1)

LIMITS: Mapping[str, int] = MappingProxyType(
    {
        PREVIEW_BUCKET: 20,
        ORDER_BUCKET: 10,
        LIMITS_BUCKET: 10,
        BACKTEST_BUCKET: 10,
        RECORDING_BUCKET: 10,
    }
)


class RateLimiter:
    def __init__(self, clock: Clock, limits: Mapping[str, int], window: timedelta = WINDOW) -> None:
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
