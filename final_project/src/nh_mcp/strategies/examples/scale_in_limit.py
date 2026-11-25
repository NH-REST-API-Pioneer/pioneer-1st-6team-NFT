"""예제 전략(수량 조절, 지정가): 1분봉 종가가 최근 평균보다 step만큼 내려갈 때마다 1계약씩 더 사서
max_steps까지 늘리고, 평균으로 돌아오면 모두 정리한다. 주문은 모두 지정가다.

params: window(평균을 낼 1분봉 수, 기본 20), step(한 단계의 하락 비율, 기본 "0.003"),
max_steps(최대 계약 수, 기본 3), tick(그 종목의 틱 단위, 필수, 예 "0.05").
ctx에는 종목 정보가 없어 틱 단위를 params로 받는다. search_symbol 결과의 틱 단위를 넣는다.
settings.max_position은 max_steps 이상으로 둔다. 목표가 그보다 크면 출력이 invalid_output이 된다.

지정가 체결: 살 때는 종가보다 1틱 낮게, 팔 때는 1틱 높게 건다. 그 뒤 봉의 저가가 지정가보다
낮아야(팔 때는 고가가 높아야) 지정가에 전량 체결된다. 저가가 지정가에 닿기만 해서는 체결되지
않는다(대기 순서를 알 수 없어서다). 새 목표를 내면 그 종목에 걸려 있던 주문은 취소된다. 그래서
목표가 지금 포지션과 같으면 아무것도 내지 않아 걸린 주문을 그대로 둔다.
"""

from decimal import Decimal
from typing import Protocol

type Event = dict[str, object]


class Context(Protocol):
    """하네스가 넘기는 ctx 가운데 이 예제가 쓰는 것."""

    params: dict[str, object]
    positions: dict[str, int]

    def history(self, symbol: str, interval: str) -> list[Event]: ...


def init(ctx: Context) -> None:
    # 처음 한 번 부른다. 여기서 오류를 내면 봉을 넣기 전에 멈춘다.
    if Decimal(str(ctx.params.get("tick", "0"))) <= 0:
        raise ValueError('params에 그 종목의 틱 단위 "tick"을 문자열로 넣어 주세요(예: "0.05")')
    if Decimal(str(ctx.params.get("step", "0.003"))) <= 0:
        raise ValueError('params의 "step"은 0보다 큰 비율 문자열이어야 합니다(예: "0.003")')
    if int(str(ctx.params.get("max_steps", 3))) < 1:
        raise ValueError('params의 "max_steps"는 1 이상이어야 합니다')


def steps_below(close: Decimal, mean: Decimal, step: Decimal, max_steps: int) -> int:
    """종가가 평균보다 몇 단계 아래인가(0~max_steps). 평균 이상이면 0."""
    if close >= mean:
        return 0
    return min(int((mean - close) / (mean * step)), max_steps)


def target(held: int, close: Decimal, mean: Decimal, step: Decimal, max_steps: int) -> int:
    """목표 계약 수. 평균 이상이면 0, 아니면 지금보다 줄이지 않고 내려간 단계만큼 늘린다."""
    if close >= mean:
        return 0
    return max(held, steps_below(close, mean, step, max_steps))


def on_event(event: Event, ctx: Context) -> dict[str, dict[str, object]] | None:
    # 1분봉에서만 판단한다. 체결 이벤트에는 목표를 그대로 둔다.
    if event["type"] != "bar" or event["interval"] != "minute":
        return None
    window = int(str(ctx.params.get("window", 20)))
    step = Decimal(str(ctx.params.get("step", "0.003")))
    max_steps = int(str(ctx.params.get("max_steps", 3)))
    tick = Decimal(str(ctx.params["tick"]))
    symbol = str(event["symbol"])
    history = ctx.history(symbol, "minute")
    if len(history) < window:
        return None
    mean = sum((Decimal(str(b["close"])) for b in history[-window:]), Decimal(0)) / window
    close = Decimal(str(event["close"]))
    held = ctx.positions.get(symbol, 0)
    goal = target(held, close, mean, step, max_steps)
    if goal == held:
        return None
    price = close - tick if goal > held else close + tick
    return {symbol: {"position": goal, "price": str(price)}}
