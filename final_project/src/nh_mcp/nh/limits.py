"""NH 호출 제한. API마다 명세의 "API 요청 제한" 등급을 따른다.

초당 건수는 명세에 없다. 팀 앱이 쓰던 수치를 잠정으로 쓰고, NH 답을 받으면 고친다.
제한기는 NH 클라이언트(= APP Key 하나)마다 하나씩 둔다.
"""

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Literal

type Grade = Literal["quote", "account", "order", "default"]
type Sleep = Callable[[float], Awaitable[None]]

WINDOW_SECONDS = 1.0
PER_SECOND: dict[Grade, int] = {"quote": 10, "account": 10, "order": 5, "default": 3}

_DOMESTIC = "/trade/v1/domestic/"
GRADES: dict[str, Grade] = {
    "/auth-service/v1/token": "default",
    "/auth-service/v1/revoke": "default",
    "/permission/v1/web-socket-key": "default",
    **{
        _DOMESTIC + name: "quote"
        for name in (
            "price",
            "price-bid",
            "price-night",
            "price-expect-fill",
            "price-all-futures",
            "price-all-option",
            "price-all-option-night",
            "chart-minute",
            "chart-daily",
            "trend-investor",
        )
    },
    **{
        _DOMESTIC + name: "account"
        for name in (
            "deposit",
            "open-interest",
            "open-position",
            "orderable-quantity",
            "order-history",
            "order-error",
            "product-detail",
        )
    },
    **{
        _DOMESTIC + name: "order"
        for name in ("order", "modify", "cancel", "order-night", "modify-night", "cancel-night")
    },
}


class UnknownApi(Exception):
    """등급을 모르는 경로다. 모르면 부르지 않는다."""


def grade_of(path: str) -> Grade:
    try:
        return GRADES[path]
    except KeyError:
        raise UnknownApi(f"호출 제한 등급을 모르는 API입니다: {path}") from None


class RateLimiter:
    """등급마다 최근 1초 안의 호출 수를 센다."""

    def __init__(
        self,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.sleep = sleep
        self._monotonic = monotonic
        self._sent: dict[Grade, deque[float]] = {grade: deque() for grade in PER_SECOND}
        self._locks: dict[Grade, asyncio.Lock] = {grade: asyncio.Lock() for grade in PER_SECOND}

    async def acquire(self, grade: Grade) -> None:
        async with self._locks[grade]:
            sent = self._sent[grade]
            while True:
                now = self._monotonic()
                while sent and now - sent[0] >= WINDOW_SECONDS:
                    sent.popleft()
                if len(sent) < PER_SECOND[grade]:
                    sent.append(now)
                    return
                await self.sleep(sent[0] + WINDOW_SECONDS - now)
