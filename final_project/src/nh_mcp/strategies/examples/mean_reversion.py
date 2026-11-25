"""예제 전략: 최근 평균보다 band 넘게 싸지면 사고, 평균으로 돌아오면 정리한다."""

from decimal import Decimal
from typing import Protocol


class Context(Protocol):
    """하네스가 넘기는 ctx 가운데 이 예제가 쓰는 것."""

    params: dict[str, object]

    def history(self, symbol: str, interval: str) -> list[dict[str, object]]: ...


def on_event(event: dict[str, object], ctx: Context) -> dict[str, dict[str, int]] | None:
    # 봉에서만 판단한다. 체결 이벤트에는 목표를 그대로 둔다.
    if event["type"] != "bar":
        return None
    window = int(str(ctx.params.get("window", 20)))
    band = Decimal(str(ctx.params.get("band", "0.02")))
    quantity = int(str(ctx.params.get("quantity", 1)))
    symbol = str(event["symbol"])
    # history는 지금 봉 이전의 봉들이다. 평균을 낼 만큼 쌓이기 전에는 판단하지 않는다.
    history = ctx.history(symbol, str(event["interval"]))
    if len(history) < window:
        return None
    closes = [Decimal(str(past["close"])) for past in history[-window:]]
    mean = sum(closes, Decimal(0)) / Decimal(window)
    close = Decimal(str(event["close"]))
    if close < mean * (Decimal(1) - band):
        return {symbol: {"position": quantity}}
    if close >= mean:
        return {symbol: {"position": 0}}
    return None
