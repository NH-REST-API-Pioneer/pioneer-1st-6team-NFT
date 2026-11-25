"""예제 전략(틱): 체결로 1분봉을 직접 만들고, 최근 window분 동안 오른 데다 호가도 사는 쪽이 두꺼우면
사고, 내린 데다 파는 쪽이 두꺼우면 판다. 흐름이 들고 있는 쪽과 반대로 돌면 정리한다.

data.streams 예: {"A016C000": ["tick", "book"]}. "book"을 빼면 호가 조건 없이 1분봉만 본다.
params: window(분, 기본 5), imbalance(호가 쏠림 문턱 0~1, 기본 "0.2"), quantity(계약 수, 기본 1).
호가 쏠림은 (매수 잔량 합 - 매도 잔량 합) / (둘의 합)이다.

틱 실행에서 알아 둘 것:
- 이벤트는 서버가 받은 순서 그대로 온다. 체결과 호가가 섞여 1초에 여러 개 온다. at은 거래소
  시각(초)에 받은 날짜를 붙인 것이고 뒤로 가지 않는다. 저장이 끊긴 구간은 비어 있다(결과의
  coverage.gaps).
- 1분봉은 주지 않는다. 필요하면 여기처럼 체결로 만든다: at의 앞 16자("2026-11-25T14:09")가 분이고,
  다음 분의 이벤트(체결이든 호가든)가 처음 올 때 그 분의 봉이 끝난다. 끝난 봉만 보고 판단해야
  실시간과 같다(지금 분의 봉은 아직 바뀐다).
- ctx.history는 봉 이벤트만 쌓는다. 체결로 만든 봉은 모듈 변수에 쌓는다. 실행마다 새 컨테이너라 모듈
  변수는 그 실행 안에서만 산다.
- 시장가는 판단한 뒤 그 종목의 다음 체결가 ± 1틱(살 때 +)에 체결된다. 호가로는 체결되지 않는다.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, cast

type Event = dict[str, object]


class Context(Protocol):
    """하네스가 넘기는 ctx 가운데 이 예제가 쓰는 것."""

    params: dict[str, object]
    positions: dict[str, int]


@dataclass
class Bar:
    """체결로 만든 1분봉. minute은 at의 앞 16자다."""

    minute: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

    def add(self, price: Decimal) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price


# 종목마다 끝난 1분봉(오래된 것부터), 지금 만드는 봉, 마지막 호가 쏠림.
CLOSED: dict[str, list[Bar]] = {}
OPEN: dict[str, Bar] = {}
LEAN: dict[str, Decimal] = {}


def close_bar(symbol: str, minute: str) -> bool:
    """지금 만드는 봉이 이 분보다 앞이면 끝난 봉으로 옮긴다. 옮겼으면 True."""
    bar = OPEN.get(symbol)
    if bar is None or bar.minute >= minute:
        return False
    CLOSED.setdefault(symbol, []).append(bar)
    del OPEN[symbol]
    return True


def lean(event: Event) -> Decimal:
    """호가 쏠림. 매수 잔량이 많으면 +, 매도 잔량이 많으면 -."""
    bids = sum(cast(int, qty) for _, qty in cast(list[list[object]], event["bids"]))
    asks = sum(cast(int, qty) for _, qty in cast(list[list[object]], event["asks"]))
    if bids + asks == 0:
        return Decimal(0)
    return Decimal(bids - asks) / Decimal(bids + asks)


def decide(symbol: str, ctx: Context) -> dict[str, dict[str, int]] | None:
    window = int(str(ctx.params.get("window", 5)))
    threshold = Decimal(str(ctx.params.get("imbalance", "0.2")))
    quantity = int(str(ctx.params.get("quantity", 1)))
    bars = CLOSED.get(symbol, [])
    if len(bars) < window:
        return None
    move = bars[-1].close - bars[-window].open
    tilt = LEAN.get(symbol)  # 호가 흐름이 없으면 None이고 호가 조건을 보지 않는다
    held = ctx.positions.get(symbol, 0)
    if move > 0 and (tilt is None or tilt >= threshold):
        target = quantity
    elif move < 0 and (tilt is None or tilt <= -threshold):
        target = -quantity
    elif held * move < 0:
        target = 0  # 들고 있는 쪽과 반대로 움직였다
    else:
        return None
    return None if target == held else {symbol: {"position": target}}


def on_event(event: Event, ctx: Context) -> dict[str, dict[str, int]] | None:
    # 체결·호가에서만 판단한다. 내 체결(fill)과 일봉에는 목표를 그대로 둔다.
    if event["type"] not in ("tick", "book"):
        return None
    symbol = str(event["symbol"])
    closed = close_bar(symbol, str(event["at"])[:16])
    if event["type"] == "book":
        LEAN[symbol] = lean(event)
    else:
        price = Decimal(str(event["price"]))
        bar = OPEN.get(symbol)
        if bar is None:
            OPEN[symbol] = Bar(str(event["at"])[:16], price, price, price, price)
        else:
            bar.add(price)
    return decide(symbol, ctx) if closed else None
