import asyncio

import pytest

from nh_mcp.nh.limits import PER_SECOND, RateLimiter, UnknownApi, grade_of


class FakeTime:
    """sleep을 부르면 시계가 그만큼 간다."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def limiter(time: FakeTime) -> RateLimiter:
    return RateLimiter(monotonic=time.monotonic, sleep=time.sleep)


def test_calls_within_the_limit_do_not_wait() -> None:
    time = FakeTime()

    async def scenario() -> None:
        rl = limiter(time)
        for _ in range(PER_SECOND["quote"]):
            await rl.acquire("quote")

    asyncio.run(scenario())
    assert time.slept == []


def test_one_more_call_waits_until_the_window_frees() -> None:
    time = FakeTime()

    async def scenario() -> None:
        rl = limiter(time)
        for _ in range(PER_SECOND["order"]):
            await rl.acquire("order")
            time.now += 0.1
        await rl.acquire("order")

    asyncio.run(scenario())
    # 첫 호출(0.0초) 뒤 1초가 지나야 6번째 주문 호출이 나간다.
    assert time.slept == [pytest.approx(0.5)]
    assert time.now == pytest.approx(1.0)


def test_grades_are_counted_separately() -> None:
    time = FakeTime()

    async def scenario() -> None:
        rl = limiter(time)
        for _ in range(PER_SECOND["order"]):
            await rl.acquire("order")
        await rl.acquire("quote")

    asyncio.run(scenario())
    assert time.slept == []


def test_concurrent_calls_never_exceed_the_limit() -> None:
    time = FakeTime()
    sent_at: list[float] = []

    async def one(rl: RateLimiter) -> None:
        await rl.acquire("default")
        sent_at.append(time.now)

    async def scenario() -> None:
        rl = limiter(time)
        await asyncio.gather(*(one(rl) for _ in range(PER_SECOND["default"] * 3)))

    asyncio.run(scenario())
    for start in sent_at:
        in_window = [t for t in sent_at if start <= t < start + 1.0]
        assert len(in_window) <= PER_SECOND["default"]


def test_spec_grades() -> None:
    assert grade_of("/trade/v1/domestic/price") == "quote"
    assert grade_of("/trade/v1/domestic/chart-minute") == "quote"
    assert grade_of("/trade/v1/domestic/product-detail") == "account"
    assert grade_of("/trade/v1/domestic/order") == "order"
    assert grade_of("/auth-service/v1/token") == "default"


def test_unknown_api_is_refused() -> None:
    with pytest.raises(UnknownApi):
        grade_of("/trade/v1/domestic/not-in-spec")
