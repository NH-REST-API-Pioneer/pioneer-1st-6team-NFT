"""예제 전략: 최근 평균보다 band 넘게 싸지면 사고, 평균으로 돌아오면 정리한다."""

from decimal import Decimal


def on_bar(
    bar: dict[str, object], history: list[dict[str, object]], params: dict[str, object]
) -> str:
    window = int(str(params.get("window", 20)))
    band = Decimal(str(params.get("band", "0.02")))
    # history는 지금 봉 이전의 봉들이다. 평균을 낼 만큼 쌓이기 전에는 판단하지 않는다.
    if len(history) < window:
        return "none"
    closes = [Decimal(str(past["close"])) for past in history[-window:]]
    mean = sum(closes, Decimal(0)) / Decimal(window)
    close = Decimal(str(bar["close"]))
    if close < mean * (Decimal(1) - band):
        return "buy"
    if close >= mean:
        return "exit"
    return "none"
