"""예제 전략(두 종목): 두 종목의 가격 차(스프레드)가 최근 평균에서 크게 벌어지면 비싸진 쪽을 팔고
싸진 쪽을 사고, 평균 가까이 돌아오면 둘 다 정리한다.

params: a·b(두 종목코드), window(짝지은 분 수, 기본 30), entry_z(진입할 z, 기본 "2"),
exit_z(정리할 z, 기본 "0.5"), quantity(계약 수, 기본 1). z는 (지금 스프레드 - 평균) / 표준편차다.
backtest_strategy의 data.streams에도 두 종목을 ["minute"]으로 넣는다.
"""

from decimal import Decimal
from typing import Protocol

type Event = dict[str, object]


class Context(Protocol):
    """하네스가 넘기는 ctx 가운데 이 예제가 쓰는 것."""

    params: dict[str, object]

    def history(self, symbol: str, interval: str) -> list[Event]: ...


def init(ctx: Context) -> None:
    # 처음 한 번 부른다. 여기서 오류를 내면 봉을 넣기 전에 멈춘다.
    a, b = ctx.params.get("a"), ctx.params.get("b")
    if not a or not b or a == b:
        raise ValueError('params에 서로 다른 두 종목코드 "a", "b"를 넣어 주세요')


def closes(ctx: Context, symbol: str, event: Event, depth: int) -> dict[str, Decimal]:
    """그 종목의 최근 1분봉 depth개의 시각 → 종가. 이번 이벤트가 그 종목 봉이면 그것까지 넣는다."""
    bars = ctx.history(symbol, "minute")[-depth:]
    if event["symbol"] == symbol:
        bars.append(event)
    return {str(bar["at"]): Decimal(str(bar["close"])) for bar in bars}


def target(z: Decimal, entry: Decimal, exit_band: Decimal, quantity: int) -> int | None:
    """a의 목표 계약 수(b는 반대 부호). None이면 그대로 둔다."""
    if z > entry:
        return -quantity  # a가 b보다 비싸게 벌어졌다: a를 팔고 b를 산다
    if z < -entry:
        return quantity
    if abs(z) < exit_band:
        return 0
    return None


def on_event(event: Event, ctx: Context) -> dict[str, dict[str, int]] | None:
    # 1분봉에서만 판단한다. 체결 이벤트에는 목표를 그대로 둔다.
    if event["type"] != "bar" or event["interval"] != "minute":
        return None
    a, b = str(ctx.params["a"]), str(ctx.params["b"])
    window = int(str(ctx.params.get("window", 30)))
    entry = Decimal(str(ctx.params.get("entry_z", "2")))
    exit_band = Decimal(str(ctx.params.get("exit_z", "0.5")))
    quantity = int(str(ctx.params.get("quantity", 1)))
    # 거래가 드문 종목은 봉이 띄엄띄엄 있으니 넉넉히 거슬러 본다.
    left, right = closes(ctx, a, event, 10 * window), closes(ctx, b, event, 10 * window)
    # 두 종목 모두 봉이 있는 분만 짝짓는다. 같은 분의 다른 종목 봉이 아직 안 왔으면(같은 분이면
    # 종목코드 순서로 온다) 그 봉이 올 때까지 기다린다.
    paired = sorted(left.keys() & right.keys())
    if len(paired) < window or paired[-1] != event["at"]:
        return None
    spreads = [left[at] - right[at] for at in paired[-window:]]
    mean = sum(spreads, Decimal(0)) / window
    spread_std = (sum(((s - mean) ** 2 for s in spreads), Decimal(0)) / window).sqrt()
    if spread_std == 0:
        return None
    side = target((spreads[-1] - mean) / spread_std, entry, exit_band, quantity)
    if side is None:
        return None
    return {a: {"position": side}, b: {"position": -side}}
