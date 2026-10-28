"""VWAP 밴드 평균회귀 전략 — 판정 규칙 한 벌.

백테스트(backtest.py)와 모의계좌 실행이 **이 파일 하나**를 같이 씁니다. 그래서 「규칙과 코드가
일치하는가」를 이 파일로 보여 줍니다. 체결(어떤 값에 사고팔았나)은 여기서 다루지 않습니다.

## 규칙 요약 (값은 Params)

- 세션 VWAP: 09:00 부터 1분봉마다 누적. 전형가 (고+저+종)/3 × 거래량의 합 ÷ 거래량의 합.
- 밴드: VWAP ± k × σ. σ 는 **거래량 가중 표준편차** √(Σv·(전형가−VWAP)² ÷ Σv).
- 판정 봉: 1분봉을 09:00 기준으로 bar_min 분씩 묶은 봉, 봉 마감 때 한 번.
- 매도 진입: (confirm) 앞 봉 종가 > 상단 이고 이번 봉 종가 ≤ 상단 — 밖으로 나갔다 복귀.
            (confirm 아님) 이번 봉 종가 > 상단.
  매수 진입: 하단에서 같은 방식.
- 익절: 가격이 VWAP 에 닿음 (직전 1분봉까지의 VWAP).
- 손절: 진입가에서 불리하게 max(ATR × stop_atr, min_stop_ticks 틱). ATR 은 판정 봉의 Wilder ATR,
        날을 넘겨 이어 셉니다 (VWAP 은 매일 새로).
- 시간 청산: 진입 뒤 hold_min 분.
- 진입 막기: 시간(entry_start~entry_end) · 보유 중 · 청산 뒤 cooldown_bars 봉 ·
            VWAP 까지 거리 < min_edge_ticks · 그날 손절이 max_stops_per_day 번 났음.
- 장 마감 정리: flatten_at 부터 전량 청산. 마감 단일가(15:35~) 봉은 쓰지 않습니다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

TICK = 0.1          # 호가 단위 (원) — product-detail cd_val_inf_1
TICK_VALUE = 1000   # 1틱 가치 (원) — product-detail tick_val


@dataclass(frozen=True)
class Params:
    """기본값 = 채택한 값 (2026-10-27). 정석 출발값은 backtest.STANDARD."""
    bar_min: int = 3            # 판정 봉 (분)                — 최적화
    band_k: float = 1.5         # 밴드 폭 (σ 배수)            — 최적화
    confirm: bool = True        # 밴드 밖 → 안 복귀를 확인하고 진입 — 최적화
    stop_atr: float = 3.0       # 손절 = ATR × 이 값          — 최적화
    atr_period: int = 14
    min_stop_ticks: int = 3     # 손절 바닥 (안전장치)
    min_edge_ticks: int = 3     # VWAP 까지 이만큼은 남아야 진입 (왕복 슬리피지 2틱보다 크게)
    hold_min: int = 120         # 시간 청산 (분)              — 최적화
    cooldown_bars: int = 1      # 청산 뒤 쉬는 판정 봉 수
    max_stops_per_day: int = 2  # 일일 한도 = 손절 2회. 손절 폭이 ATR 로 변하므로 금액 대신 횟수로 둠
    session_start: str = "090000"
    session_end: str = "153500"   # 이 시각부터는 마감 단일가 — 쓰지 않음
    entry_start: str = "093000"
    entry_end: str = "150000"
    flatten_at: str = "153000"


@dataclass(frozen=True)
class Minute:
    """1분봉 하나. time 은 HHMMSS (그 분의 시작 시각)."""
    date: str
    time: str
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class SignalBar:
    """판정 봉 하나와, 마감 때 본 지표값."""
    date: str
    start: str
    end: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: float | None = None
    sigma: float | None = None
    upper: float | None = None
    lower: float | None = None
    atr: float | None = None


@dataclass
class Signal:
    """판정 봉 마감 때의 판단. side 가 None 이면 진입 신호 없음."""
    bar: SignalBar
    side: str | None            # "buy" / "sell" / None
    reason: str                 # 신호 근거 또는 진입하지 않은 까닭
    stop_ticks: int | None = None


def minutes_of(hhmmss: str) -> int:
    return int(hhmmss[:2]) * 60 + int(hhmmss[2:4])


def hhmmss_of(minutes: int) -> str:
    return f"{minutes // 60:02d}{minutes % 60:02d}00"


class SessionVWAP:
    """세션 VWAP 과 거래량 가중 σ. 매일 reset()."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.sum_v = 0.0
        self.sum_pv = 0.0
        self.sum_p2v = 0.0

    def update(self, m: Minute) -> None:
        if m.volume <= 0:
            return
        tp = (m.high + m.low + m.close) / 3.0
        self.sum_v += m.volume
        self.sum_pv += tp * m.volume
        self.sum_p2v += tp * tp * m.volume

    @property
    def vwap(self) -> float | None:
        return self.sum_pv / self.sum_v if self.sum_v > 0 else None

    @property
    def sigma(self) -> float | None:
        if self.sum_v <= 0:
            return None
        mean = self.sum_pv / self.sum_v
        return math.sqrt(max(self.sum_p2v / self.sum_v - mean * mean, 0.0))


class WilderATR:
    """판정 봉의 ATR. 첫 값은 TR 단순 평균, 그 뒤 Wilder 평활 (NH 앱 indicators.py 와 같은 식)."""

    def __init__(self, period: int) -> None:
        self.period = period
        self.prev_close: float | None = None
        self.ranges: list[float] = []
        self.value: float | None = None

    def update(self, high: float, low: float, close: float) -> float | None:
        tr = high - low if self.prev_close is None else max(
            high - low, abs(high - self.prev_close), abs(low - self.prev_close))
        self.prev_close = close
        if self.value is None:
            self.ranges.append(tr)
            if len(self.ranges) == self.period:
                self.value = math.fsum(self.ranges) / self.period   # fsum: 파이썬 버전과 상관없이 같은 값
        else:
            self.value = (self.value * (self.period - 1) + tr) / self.period
        return self.value


@dataclass
class DayState:
    """하루 동안의 진입 제한 상태. 체결 쪽(백테스트·실행)이 채웁니다."""
    realized_pnl: float = 0.0       # 원
    stops: int = 0                  # 그날 손절로 끝난 거래 수
    in_position: bool = False
    bars_since_exit: int = 10**9


class VWAPReversion:
    """1분봉을 넣으면 VWAP 을 갱신하고, 판정 봉이 닫힐 때 Signal 을 돌려줍니다."""

    def __init__(self, params: Params) -> None:
        self.p = params
        self.vwap = SessionVWAP()
        self.atr = WilderATR(params.atr_period)
        self.date: str | None = None
        self.bucket: int | None = None
        self.building: SignalBar | None = None
        self.prev_bar: SignalBar | None = None
        self.day = DayState()
        self.start_min = minutes_of(params.session_start)

    # ---- 날과 세션 ----
    def in_session(self, m: Minute) -> bool:
        return self.p.session_start <= m.time < self.p.session_end

    def new_day(self, date: str) -> None:
        self.date = date
        self.vwap.reset()
        self.bucket = None
        self.building = None
        self.prev_bar = None    # 밴드가 날마다 새로 서므로 「앞 봉」도 날을 넘기지 않음
        self.day = DayState()

    @property
    def current_vwap(self) -> float | None:
        return self.vwap.vwap

    # ---- 1분봉 입력 ----
    def on_minute(self, m: Minute) -> list[Signal]:
        """1분봉 하나를 넣습니다. 이 분으로 닫힌 판정 봉이 있으면 그 Signal 들을 돌려줍니다."""
        if m.date != self.date:
            self.new_day(m.date)
        if not self.in_session(m):
            return []
        out: list[Signal] = []
        bucket = (minutes_of(m.time) - self.start_min) // self.p.bar_min
        if self.building is not None and bucket != self.bucket:
            out.append(self._close_bar())       # 빈 분이 있어 다음 묶음 분이 먼저 온 경우
        self.vwap.update(m)
        if self.building is None:
            self.bucket = bucket
            start = hhmmss_of(self.start_min + bucket * self.p.bar_min)
            self.building = SignalBar(m.date, start, start, m.open, m.high, m.low, m.close, m.volume)
        else:
            b = self.building
            b.high, b.low, b.close = max(b.high, m.high), min(b.low, m.low), m.close
            b.volume += m.volume
        self.building.end = m.time
        last_minute_of_bucket = (minutes_of(m.time) - self.start_min + 1) % self.p.bar_min == 0
        if last_minute_of_bucket:
            out.append(self._close_bar())
        return out

    def end_of_day(self) -> list[Signal]:
        """날이 끝났는데 덜 닫힌 판정 봉이 있으면 닫습니다 (판정은 하지만 진입 시간 밖)."""
        return [self._close_bar()] if self.building is not None else []

    # ---- 판정 ----
    def _close_bar(self) -> Signal:
        bar, self.building = self.building, None
        bar.atr = self.atr.update(bar.high, bar.low, bar.close)
        bar.vwap, bar.sigma = self.vwap.vwap, self.vwap.sigma
        if bar.vwap is not None and bar.sigma is not None:
            bar.upper = bar.vwap + self.p.band_k * bar.sigma
            bar.lower = bar.vwap - self.p.band_k * bar.sigma
        prev, self.prev_bar = self.prev_bar, bar
        self.day.bars_since_exit += 1
        return self._decide(bar, prev)

    def _decide(self, bar: SignalBar, prev: SignalBar | None) -> Signal:
        p = self.p
        if bar.upper is None or bar.atr is None:
            return Signal(bar, None, "지표 준비 전")

        side = None
        if p.confirm:
            if prev is not None and prev.upper is not None:
                if prev.close > prev.upper and bar.close <= bar.upper:
                    side = "sell"
                elif prev.close < prev.lower and bar.close >= bar.lower:
                    side = "buy"
        else:
            if bar.close > bar.upper:
                side = "sell"
            elif bar.close < bar.lower:
                side = "buy"
        if side is None:
            return Signal(bar, None, "밴드 조건 아님")

        stop_ticks = max(math.ceil(bar.atr * p.stop_atr / TICK - 1e-9), p.min_stop_ticks)
        # 진입 시각 = 판정 봉 다음 분
        entry_time = hhmmss_of(minutes_of(bar.end) + 1)
        if not (p.entry_start <= entry_time < p.entry_end):
            return Signal(bar, None, f"{side} 신호 · 진입 시간 밖", stop_ticks)
        if self.day.in_position:
            return Signal(bar, None, f"{side} 신호 · 보유 중", stop_ticks)
        if self.day.bars_since_exit <= p.cooldown_bars:
            return Signal(bar, None, f"{side} 신호 · 청산 뒤 대기", stop_ticks)
        edge_ticks = abs(bar.vwap - bar.close) / TICK
        if edge_ticks < p.min_edge_ticks:
            return Signal(bar, None, f"{side} 신호 · VWAP 까지 {edge_ticks:.1f}틱 < {p.min_edge_ticks}", stop_ticks)
        if self.day.stops >= p.max_stops_per_day:
            return Signal(bar, None, f"{side} 신호 · 오늘 손절 {p.max_stops_per_day}회 도달", stop_ticks)
        why = "상단 이탈 뒤 복귀" if side == "sell" else "하단 이탈 뒤 복귀"
        if not p.confirm:
            why = "상단 이탈" if side == "sell" else "하단 이탈"
        return Signal(bar, side, why, stop_ticks)

    # ---- 체결 쪽이 알려 주는 것 ----
    def on_entry(self) -> None:
        self.day.in_position = True

    def on_exit(self, pnl_won: float, stopped: bool) -> None:
        self.day.in_position = False
        self.day.realized_pnl += pnl_won
        self.day.stops += int(stopped)
        self.day.bars_since_exit = 0


def stop_price(side: str, entry: float, stop_ticks: int) -> float:
    return round(entry - stop_ticks * TICK, 1) if side == "buy" else round(entry + stop_ticks * TICK, 1)


def tick_up(price: float) -> float:
    """price 이상인 가장 가까운 호가. 이미 호가면 그대로 (부동소수 잡음은 6자리에서 버림)."""
    return round(math.ceil(round(price / TICK, 6)) * TICK, 1)


def tick_down(price: float) -> float:
    """price 이하인 가장 가까운 호가."""
    return round(math.floor(round(price / TICK, 6)) * TICK, 1)


def take_profit_price(side: str, target: float) -> float:
    """익절 목표(VWAP·중심선)를 **불리한 쪽 호가**로 맞춘 값 — 가격이 여기까지 와야 닿은 것으로 봅니다.

    매수 보유는 올림, 매도 보유는 내림. 실제 체결가는 늘 0.1원 단위라, 틱이 아닌 목표값 그대로
    체결된 것으로 세면 손익이 반 틱 경계에 걸려 계산 환경마다 1틱씩 달라집니다 (이슈 #1).
    """
    return tick_up(target) if side == "buy" else tick_down(target)


def pnl_ticks(side: str, entry: float, exit_: float) -> int:
    diff = exit_ - entry if side == "buy" else entry - exit_
    return round(diff / TICK)
