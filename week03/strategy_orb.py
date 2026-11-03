"""세션 개장 돌파 전략 — 판정 규칙 한 벌 (3주차 후보 B).

원달러는 시장이 새로 열리는 순간(서울 외환시장 · 런던 · 미국 지표 발표)에 크게 움직입니다.
개장 뒤 첫 range_min 분의 고가·저가를 기준 범위로 잡고, 1분봉 종가가 그 범위를 벗어나면
벗어난 쪽으로 따라 들어갑니다. 후보 A(볼린저+RSI, strategy_bb.py)가 평균회귀라면 이쪽은 추세 추종입니다.

1분봉을 넣으면 1분봉이 닫힐 때마다 판단합니다. 체결(어떤 값에 사고팔았나)은 여기서 다루지 않습니다.

## 규칙 요약 (값은 Params, 시각은 한국 시간)

- 개장 시점: 서울 09:00 · 런던 17:00 · 뉴욕 22:30 (미국 지표 발표).
  서머타임이 끝난 11월~3월 기준입니다. 3월 둘째 주부터는 런던 16:00 · 뉴욕 21:30 으로 바꿉니다.
- 기준 범위: 개장 시점부터 range_min 분 동안 1분봉의 최고가·최저가.
- 진입: 범위가 정해진 뒤 entry_min 분 안에, 1분봉 종가가 범위 위로 (1 + buffer_ticks) 틱 이상
  나가면 매수, 아래로 나가면 매도. 진입 시각 = 판단한 1분봉의 다음 분.
- 손절: 범위 반대편 끝 (판단한 종가에서 잰 틱 수).
- 익절: 진입가에서 범위 폭 × target_r 틱.
- 시간 청산: 진입 뒤 hold_min 분, 또는 다음 개장 시점(exit_by), 또는 23:50 중 먼저 오는 때.
- 진입 막기: 그 개장 시점에 이미 진입함 (개장마다 한 번) · 보유 중 ·
            범위 폭 < min_range_ticks (잡음) · 범위 폭 > max_range_ticks (이미 움직임이 끝남) ·
            손절 폭 > max_stop_ticks · 그날 손절이 max_stops_per_day 번 났음 · 진입 시각이 매매 시간 밖.
- MKU 는 환율의 역수입니다. 매수 = 원화 강세(환율 하락) 쪽, 매도 = 원화 약세(환율 상승) 쪽.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from common import (FLATTEN_AT, TICK, TICK_VALUE, DayState, Minute, hhmmss_of,  # noqa: F401
                    in_trading_hours, minutes_of, pnl_ticks, stop_price, target_price,
                    ticks_between, won_view)


@dataclass(frozen=True)
class Params:
    """기본값 = 출발값 (아직 최적화 전). 틱 값은 1원 ≈ 5.5틱 (환율 1,345원 근처) 으로 잡았습니다."""
    windows: tuple[tuple[str, str], ...] = (("서울", "090000"), ("런던", "170000"), ("뉴욕", "223000"))
    range_min: int = 15         # 기준 범위를 잡는 시간 (분)          — 최적화
    entry_min: int = 60         # 범위가 정해진 뒤 돌파를 기다리는 시간 (분)
    buffer_ticks: int = 0       # 범위 밖으로 1틱에 더해 이만큼 더 나가야 돌파
    min_range_ticks: int = 4    # 범위가 이보다 좁으면 잡음으로 보고 진입 안 함 (≈ 0.7원)
    max_range_ticks: int = 30   # 범위가 이보다 넓으면 이미 움직였다고 보고 진입 안 함 (≈ 5.4원)
    max_stop_ticks: int = 40    # 손절 폭 상한 (≈ 7.2원, US$100)
    target_r: float = 1.0       # 익절 = 범위 폭 × 이 값              — 최적화
    hold_min: int = 120         # 시간 청산 (분)                      — 최적화
    max_stops_per_day: int = 2  # 일일 한도 = 손절 2회 (후보 A 와 같은 규칙)
    flatten_at: str = FLATTEN_AT


@dataclass
class OpeningRange:
    """개장 시점 하나의 기준 범위와 그날 진입 여부."""
    name: str
    open: str
    high: float | None = None
    low: float | None = None
    minutes: int = 0            # 범위에 들어간 1분봉 수 (거래가 없던 분은 봉이 없음)
    entered: bool = False

    def update(self, m: Minute) -> None:
        self.high = m.high if self.high is None else max(self.high, m.high)
        self.low = m.low if self.low is None else min(self.low, m.low)
        self.minutes += 1

    @property
    def width_ticks(self) -> int | None:
        return None if self.high is None else ticks_between(self.high, self.low)


@dataclass
class Signal:
    """1분봉 마감 때의 판단. side 가 None 이면 진입 신호 없음."""
    minute: Minute
    window: str                 # 개장 시점 이름 ("서울" 등)
    side: str | None            # "buy" / "sell" / None
    reason: str                 # 신호 근거 또는 진입하지 않은 까닭
    range_high: float | None = None
    range_low: float | None = None
    stop_ticks: int | None = None
    target_ticks: int | None = None
    exit_by: str | None = None  # 이 시각까지는 청산 (다음 개장 시점 또는 23:50)


class SessionBreakout:
    """1분봉을 넣으면 기준 범위를 쌓고, 범위가 정해진 뒤에는 1분봉마다 Signal 을 돌려줍니다."""

    def __init__(self, params: Params) -> None:
        opens = [o for _, o in params.windows]
        if opens != sorted(opens):
            raise ValueError("windows 는 개장 시각 순서대로 적어야 합니다")
        self.p = params
        self.date: str | None = None
        self.ranges: list[OpeningRange] = []
        self.day = DayState()

    # ---- 날 ----
    def new_day(self, date: str) -> None:
        self.date = date
        self.ranges = [OpeningRange(name, open_) for name, open_ in self.p.windows]
        self.day = DayState()

    def _window_at(self, hhmmss: str) -> int | None:
        """이 시각이 속한 개장 시점 — 개장 시각이 지난 것 중 마지막. 첫 개장 전이면 None."""
        at = None
        for i, r in enumerate(self.ranges):
            if r.open <= hhmmss:
                at = i
        return at

    def _exit_by(self, at: int) -> str:
        nxt = self.ranges[at + 1].open if at + 1 < len(self.ranges) else self.p.flatten_at
        return min(nxt, self.p.flatten_at)

    # ---- 1분봉 입력 ----
    def on_minute(self, m: Minute) -> list[Signal]:
        """1분봉 하나를 넣습니다. 진입을 판단한 분이면 그 Signal 을 돌려줍니다."""
        if m.date != self.date:
            self.new_day(m.date)
        if not in_trading_hours(m.time):
            return []
        at = self._window_at(m.time)
        if at is None:
            return []
        r = self.ranges[at]
        t = minutes_of(m.time)
        range_end = minutes_of(r.open) + self.p.range_min
        if t < range_end:
            r.update(m)
            return []
        if t < range_end + self.p.entry_min:
            return [self._decide(m, at)]
        return []

    # ---- 판정 ----
    def _decide(self, m: Minute, at: int) -> Signal:
        p, r = self.p, self.ranges[at]
        sig = Signal(m, r.name, None, "", r.high, r.low, exit_by=self._exit_by(at))
        if r.high is None:
            sig.reason = "범위 봉 없음 (개장 뒤 거래 없음)"
            return sig

        need = 1 + p.buffer_ticks
        if ticks_between(m.close, r.high) >= need:
            side, stop_ticks = "buy", ticks_between(m.close, r.low)
        elif ticks_between(r.low, m.close) >= need:
            side, stop_ticks = "sell", ticks_between(r.high, m.close)
        else:
            sig.reason = "범위 안"
            return sig

        width = r.width_ticks
        sig.stop_ticks = stop_ticks
        sig.target_ticks = max(1, math.ceil(width * p.target_r - 1e-9))
        entry_time = hhmmss_of(minutes_of(m.time) + 1)
        if r.entered:
            sig.reason = f"{side} 신호 · {r.name} 개장에서 이미 진입함"
        elif self.day.in_position:
            sig.reason = f"{side} 신호 · 보유 중"
        elif width < p.min_range_ticks:
            sig.reason = f"{side} 신호 · 범위 {width}틱 < {p.min_range_ticks} (잡음)"
        elif width > p.max_range_ticks:
            sig.reason = f"{side} 신호 · 범위 {width}틱 > {p.max_range_ticks} (이미 움직임)"
        elif stop_ticks > p.max_stop_ticks:
            sig.reason = f"{side} 신호 · 손절 폭 {stop_ticks}틱 > {p.max_stop_ticks}"
        elif self.day.stops >= p.max_stops_per_day:
            sig.reason = f"{side} 신호 · 오늘 손절 {p.max_stops_per_day}회 도달"
        elif not in_trading_hours(entry_time) or entry_time >= p.flatten_at:
            sig.reason = f"{side} 신호 · 진입 시각 {entry_time} 이 매매 시간 밖"
        else:
            sig.side = side
            where = "위" if side == "buy" else "아래"
            sig.reason = f"{r.name} 개장 범위 {where}로 돌파 · 범위 {width}틱 · {won_view(side)}"
        return sig

    # ---- 체결 쪽이 알려 주는 것 ----
    def on_entry(self, window: str) -> None:
        self.day.in_position = True
        for r in self.ranges:
            if r.name == window:
                r.entered = True

    def on_exit(self, pnl_usd: float, stopped: bool) -> None:
        self.day.in_position = False
        self.day.realized_pnl += pnl_usd
        self.day.stops += int(stopped)
        self.day.bars_since_exit = 0
