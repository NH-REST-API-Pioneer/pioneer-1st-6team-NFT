"""사용자별 요청 속도 제한: 버킷마다 1분 고정 창."""

from datetime import timedelta

import pytest
from fakes import Clock

from nh_mcp.domain import UserId
from nh_mcp.ops.rate_limit import LIMITS, RateLimiter

ALICE = UserId(1)
BOB = UserId(2)


def test_the_bucket_values_are_the_agreed_ones() -> None:
    assert dict(LIMITS) == {
        "preview": 20,
        "order": 10,
        "limits": 10,
        "backtest": 10,
        "recording": 10,
    }


def test_twenty_previews_pass_and_the_twenty_first_is_refused() -> None:
    limiter = RateLimiter(Clock(), LIMITS)

    allowed = [limiter.allow(ALICE, "preview") for _ in range(20)]

    assert allowed == [True] * 20
    assert limiter.allow(ALICE, "preview") is False


def test_a_minute_later_it_is_allowed_again() -> None:
    clock = Clock()
    limiter = RateLimiter(clock, LIMITS)
    for _ in range(20):
        limiter.allow(ALICE, "preview")

    clock.now += timedelta(seconds=59)
    assert limiter.allow(ALICE, "preview") is False
    clock.now += timedelta(seconds=1)
    assert limiter.allow(ALICE, "preview") is True


def test_one_user_going_over_does_not_affect_another() -> None:
    limiter = RateLimiter(Clock(), LIMITS)
    for _ in range(21):
        limiter.allow(ALICE, "preview")

    assert limiter.allow(BOB, "preview") is True


def test_buckets_are_counted_separately() -> None:
    limiter = RateLimiter(Clock(), LIMITS)
    for _ in range(10):
        limiter.allow(ALICE, "order")

    assert limiter.allow(ALICE, "order") is False
    assert limiter.allow(ALICE, "limits") is True
    assert limiter.allow(ALICE, "preview") is True


def test_refused_requests_do_not_push_the_window_forward() -> None:
    clock = Clock()
    limiter = RateLimiter(clock, {"order": 1})
    assert limiter.allow(ALICE, "order") is True

    clock.now += timedelta(seconds=30)
    assert limiter.allow(ALICE, "order") is False
    clock.now += timedelta(seconds=30)
    assert limiter.allow(ALICE, "order") is True


def test_an_unknown_bucket_is_a_programming_error() -> None:
    limiter = RateLimiter(Clock(), LIMITS)

    with pytest.raises(KeyError):
        limiter.allow(ALICE, "typo")
