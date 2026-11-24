"""격리 테스트가 함께 쓰는 봉 만들기."""

from nh_mcp.sandbox.wire import WireBar


def bars(n: int) -> list[WireBar]:
    """실행기가 보내는 모양 그대로의 봉 n개. 가격은 문자열, 거래량은 정수."""
    out: list[WireBar] = []
    for i in range(n):
        price = str(350 + i % 7)
        out.append(
            {
                "at": f"2026-11-02T09:{i % 60:02d}:00+09:00",
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": 10 + i,
            }
        )
    return out
