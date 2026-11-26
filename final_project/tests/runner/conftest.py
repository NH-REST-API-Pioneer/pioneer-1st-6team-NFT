"""실행기 테스트: NH 호출 제한기는 실제 시계로 잠들어 멈춘 시계와 맞지 않아 등급 한도를 크게 둔다.

호출 제한은 `tests/nh/test_limits.py`가 따로 본다. 여기서는 실행기의 순서·주문만 본다.
"""

from collections.abc import Iterator

import pytest

from nh_mcp.nh import limits


@pytest.fixture(autouse=True)
def unlimited_nh(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(limits, "PER_SECOND", dict.fromkeys(limits.PER_SECOND, 1_000_000))
    yield
