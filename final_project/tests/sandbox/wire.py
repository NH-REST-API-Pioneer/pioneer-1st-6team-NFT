"""격리 테스트가 함께 쓰는 일 만들기. 한 종목 A의 1분봉 실행이다."""

from datetime import datetime, timedelta
from decimal import Decimal

from nh_mcp.domain import KST, BarEvent, Candle, JsonValue, MarketEvent, SandboxJob, SymbolTerms

SYMBOL = "A"
TICK = Decimal("0.05")
TERMS = {SYMBOL: SymbolTerms(TICK, Decimal("250000"), Decimal("0"))}
T0 = datetime(2026, 11, 2, 9, 0, tzinfo=KST)


def bars(n: int) -> list[MarketEvent]:
    """A의 1분봉 n개. 가격은 350~356을 돈다."""
    out: list[MarketEvent] = []
    for i in range(n):
        price = Decimal(350 + i % 7)
        candle = Candle(T0 + timedelta(minutes=i), price, price, price, price, 10 + i)
        out.append(BarEvent(SYMBOL, "minute", candle))
    return out


def job(
    code: str,
    events: list[MarketEvent] | None = None,
    params: dict[str, JsonValue] | None = None,
    max_position: int = 1,
) -> SandboxJob:
    """A의 1분봉 실행. 이벤트를 주지 않으면 봉 3개."""
    return SandboxJob(
        code=code,
        params=params or {},
        streams={SYMBOL: frozenset({"minute"})},
        max_position=max_position,
        terms=TERMS,
        events=bars(3) if events is None else events,
    )
