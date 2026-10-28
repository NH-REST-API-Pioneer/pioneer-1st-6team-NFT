"""볼린저 밴드 + RSI 평균회귀 전략 — 판정 규칙 한 벌 (2주차 후보 2).

VWAP 후보(strategy.py)와 같은 구조입니다. 1분봉을 넣으면 판정 봉을 쌓고, 판정 봉이 닫힐 때
한 번 판단합니다. 체결(어떤 값에 사고팔았나)은 여기서 다루지 않습니다 — backtest_bb.py 가
VWAP 후보와 **같은 체결 모형(min1-v2)** 으로 셉니다. 1분봉·ATR·손절가·손익 틱 계산은
strategy.py 의 것을 그대로 가져다 씁니다.

## 규칙 요약 (값은 Params)

- 판정 봉: 1분봉을 09:00 기준으로 bar_min 분씩 묶은 봉, 봉 마감 때 한 번.
- 볼린저 밴드: 판정 봉 종가의 SMA(bb_period) ± band_k × σ. σ 는 **모집단 표준편차**
  (NH 앱 indicators.py 와 같은 식). 날을 넘겨 이어 셉니다 (앱이 봉을 이어 붙이는 것과 같음).
- RSI: 판정 봉 종가의 Wilder RSI(rsi_period). 첫 값은 처음 rsi_period 개 변화의 단순 평균,
  그 뒤 1/n 평활 (앱과 같은 식). 날을 넘겨 이어 셉니다.
- 준비: 앱과 같이 지표 기간 × 2 개의 판정 봉이 쌓이기 전에는 판단하지 않습니다.
- 매수 진입: 종가가 하단 밖 **그리고** RSI ≤ rsi_buy.
  (confirm) 앞 봉이 그 조건(밖 + RSI)이고 이번 봉 종가가 하단 안으로 복귀.
  (confirm 아님) 이번 봉이 그 조건.
  매도 진입: 상단·RSI ≥ 100 − rsi_buy 로 대칭. rsi_buy = 100 이면 RSI 필터를 끈 것.
- 익절: 가격이 **중심선(SMA)** 에 닿음 (마지막으로 닫힌 판정 봉의 중심선).
- 손절: 진입가에서 불리하게 max(ATR × stop_atr, min_stop_ticks 틱). ATR 은 판정 봉의 Wilder ATR.
- 시간 청산: 진입 뒤 hold_min 분.
- 진입 막기: 시간(entry_start~entry_end) · 보유 중 · 청산 뒤 cooldown_bars 봉 ·
            중심선까지 거리 < min_edge_ticks · 그날 손절이 max_stops_per_day 번 났음.
- 장 마감 정리: flatten_at 부터 전량 청산. 마감 단일가(15:35~) 봉은 쓰지 않습니다.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

from strategy import (TICK, TICK_VALUE, DayState, Minute, WilderATR, hhmmss_of, minutes_of,  # noqa: F401
                      pnl_ticks, stop_price)

#: 앱 indicators.WARMUP_FACTOR 와 같은 값 — 지표 기간의 몇 배를 모아야 값을 믿을지.
WARMUP_FACTOR = 2


@dataclass(frozen=True)
class Params:
    """기본값 = 채택한 값 (2026-10-27, 이웃 평균 1위). 정석 출발값은 backtest_bb.STANDARD."""
    bar_min: int = 5            # 판정 봉 (분)                       — 최적화
    bb_period: int = 20         # 볼린저 기간 (정석, 고정)
    band_k: float = 1.5         # 밴드 폭 (σ 배수)                   — 최적화
    rsi_period: int = 14        # RSI 기간 (정석, 고정)
    rsi_buy: float = 35.0       # 매수 RSI 상한. 매도는 100 − 이 값. 100 이면 필터 없음 — 최적화
    confirm: bool = False       # True 면 밴드 밖 → 안 복귀를 확인하고 진입, False 면 이탈 즉시 — 최적화
    stop_atr: float = 1.5       # 손절 = ATR × 이 값                  — 최적화
    atr_period: int = 14
    min_stop_ticks: int = 3     # 손절 바닥 (안전장치)
    min_edge_ticks: int = 3     # 중심선까지 이만큼은 남아야 진입 (왕복 슬리피지 2틱보다 크게)
    hold_min: int = 120         # 시간 청산 (분)                      — 최적화
    cooldown_bars: int = 1      # 청산 뒤 쉬는 판정 봉 수
    max_stops_per_day: int = 2  # 일일 한도 = 손절 2회 (VWAP 후보와 같은 규칙)
    session_start: str = "090000"
    session_end: str = "153500"   # 이 시각부터는 마감 단일가 — 쓰지 않음
    entry_start: str = "093000"
    entry_end: str = "150000"
    flatten_at: str = "153000"

    @property
    def rsi_sell(self) -> float:
        return 100.0 - self.rsi_buy


@dataclass
class BBBar:
    """판정 봉 하나와, 마감 때 본 지표값."""
    date: str
    start: str
    end: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    middle: float | None = None
    upper: float | None = None
    lower: float | None = None
    rsi: float | None = None
    atr: float | None = None


@dataclass
class Signal:
    """판정 봉 마감 때의 판단. side 가 None 이면 진입 신호 없음."""
    bar: BBBar
    side: str | None            # "buy" / "sell" / None
    reason: str                 # 신호 근거 또는 진입하지 않은 까닭
    stop_ticks: int | None = None


class RollingBollinger:
    """판정 봉 종가의 SMA ± k×σ (모집단 표준편차). 기간이 차기 전에는 None."""

    def __init__(self, period: int, k: float) -> None:
        self.period, self.k = period, k
        self.window: deque[float] = deque(maxlen=period)

    def update(self, close: float) -> tuple[float | None, float | None, float | None]:
        self.window.append(close)
        if len(self.window) < self.period:
            return None, None, None
        # fsum: 정확히 반올림한 합이라 파이썬 버전(3.12 의 sum() 변경)과 상관없이 같은 값 — 이슈 #1
        mid = math.fsum(self.window) / self.period
        spread = math.sqrt(math.fsum((v - mid) ** 2 for v in self.window) / self.period)
        return mid + self.k * spread, mid, mid - self.k * spread


class WilderRSI:
    """RSI. 첫 값은 처음 period 개 변화의 단순 평균, 그 뒤 Wilder 평활 (앱 indicators.rsi 와 같은 식)."""

    def __init__(self, period: int) -> None:
        self.period = period
        self.prev_close: float | None = None
        self.gains: list[float] = []
        self.losses: list[float] = []
        self.avg_gain: float | None = None
        self.avg_loss: float | None = None
        self.value: float | None = None

    def update(self, close: float) -> float | None:
        if self.prev_close is None:
            self.prev_close = close
            return None
        change = close - self.prev_close
        self.prev_close = close
        gain, loss = max(change, 0.0), max(-change, 0.0)
        if self.avg_gain is None:
            self.gains.append(gain)
            self.losses.append(loss)
            if len(self.gains) < self.period:
                return None
            self.avg_gain = math.fsum(self.gains) / self.period
            self.avg_loss = math.fsum(self.losses) / self.period
        else:
            self.avg_gain = (self.avg_gain * (self.period - 1) + gain) / self.period
            self.avg_loss = (self.avg_loss * (self.period - 1) + loss) / self.period
        self.value = 100.0 if self.avg_loss == 0 else 100.0 - 100.0 / (1.0 + self.avg_gain / self.avg_loss)
        return self.value


class BBRSIReversion:
    """1분봉을 넣으면 판정 봉을 쌓고, 판정 봉이 닫힐 때 Signal 을 돌려줍니다."""

    def __init__(self, params: Params) -> None:
        self.p = params
        self.bb = RollingBollinger(params.bb_period, params.band_k)
        self.rsi = WilderRSI(params.rsi_period)
        self.atr = WilderATR(params.atr_period)
        self.bars_seen = 0
        self.warmup = WARMUP_FACTOR * max(params.bb_period, params.rsi_period, params.atr_period)
        self.date: str | None = None
        self.bucket: int | None = None
        self.building: BBBar | None = None
        self.prev_bar: BBBar | None = None
        self.target: float | None = None    # 마지막으로 닫힌 판정 봉의 중심선 = 익절 목표
        self.day = DayState()
        self.start_min = minutes_of(params.session_start)

    # ---- 날과 세션 ----
    def in_session(self, m: Minute) -> bool:
        return self.p.session_start <= m.time < self.p.session_end

    def new_day(self, date: str) -> None:
        """지표(밴드·RSI·ATR)는 날을 넘겨 이어 세고, 「앞 봉」복귀 판정만 날을 넘기지 않습니다."""
        self.date = date
        self.bucket = None
        self.building = None
        self.prev_bar = None
        self.day = DayState()

    # ---- 1분봉 입력 (묶음 규칙은 strategy.py 와 같음) ----
    def on_minute(self, m: Minute) -> list[Signal]:
        if m.date != self.date:
            self.new_day(m.date)
        if not self.in_session(m):
            return []
        out: list[Signal] = []
        bucket = (minutes_of(m.time) - self.start_min) // self.p.bar_min
        if self.building is not None and bucket != self.bucket:
            out.append(self._close_bar())       # 빈 분이 있어 다음 묶음 분이 먼저 온 경우
        if self.building is None:
            self.bucket = bucket
            start = hhmmss_of(self.start_min + bucket * self.p.bar_min)
            self.building = BBBar(m.date, start, start, m.open, m.high, m.low, m.close, m.volume)
        else:
            b = self.building
            b.high, b.low, b.close = max(b.high, m.high), min(b.low, m.low), m.close
            b.volume += m.volume
        self.building.end = m.time
        if (minutes_of(m.time) - self.start_min + 1) % self.p.bar_min == 0:
            out.append(self._close_bar())
        return out

    def end_of_day(self) -> list[Signal]:
        return [self._close_bar()] if self.building is not None else []

    # ---- 판정 ----
    def _close_bar(self) -> Signal:
        bar, self.building = self.building, None
        self.bars_seen += 1
        bar.atr = self.atr.update(bar.high, bar.low, bar.close)
        bar.upper, bar.middle, bar.lower = self.bb.update(bar.close)
        bar.rsi = self.rsi.update(bar.close)
        if bar.middle is not None:
            self.target = bar.middle
        prev, self.prev_bar = self.prev_bar, bar
        self.day.bars_since_exit += 1
        return self._decide(bar, prev)

    def _outside(self, bar: BBBar) -> str | None:
        """밴드 밖이면서 RSI 조건까지 맞으면 그 방향, 밴드 밖인데 RSI 가 거르면 'rsi', 아니면 None."""
        p = self.p
        if bar.close > bar.upper:
            return "sell" if bar.rsi >= p.rsi_sell else "rsi"
        if bar.close < bar.lower:
            return "buy" if bar.rsi <= p.rsi_buy else "rsi"
        return None

    def _decide(self, bar: BBBar, prev: BBBar | None) -> Signal:
        p = self.p
        if bar.upper is None or bar.rsi is None or bar.atr is None or self.bars_seen < self.warmup:
            return Signal(bar, None, "지표 준비 전")

        side = None
        if p.confirm:
            if prev is not None and prev.upper is not None and prev.rsi is not None:
                state = self._outside(prev)
                if state == "sell" and bar.close <= bar.upper:
                    side = "sell"
                elif state == "buy" and bar.close >= bar.lower:
                    side = "buy"
                elif state == "rsi" and (
                        (prev.close > prev.upper and bar.close <= bar.upper)
                        or (prev.close < prev.lower and bar.close >= bar.lower)):
                    return Signal(bar, None, f"복귀 신호 후보 · RSI {prev.rsi:.0f} 가 걸러 진입 안 함")
        else:
            state = self._outside(bar)
            if state in ("buy", "sell"):
                side = state
            elif state == "rsi":
                return Signal(bar, None, f"이탈 신호 후보 · RSI {bar.rsi:.0f} 가 걸러 진입 안 함")
        if side is None:
            return Signal(bar, None, "밴드 조건 아님")

        stop_ticks = max(math.ceil(bar.atr * p.stop_atr / TICK - 1e-9), p.min_stop_ticks)
        entry_time = hhmmss_of(minutes_of(bar.end) + 1)      # 진입 시각 = 판정 봉 다음 분
        if not (p.entry_start <= entry_time < p.entry_end):
            return Signal(bar, None, f"{side} 신호 · 진입 시간 밖", stop_ticks)
        if self.day.in_position:
            return Signal(bar, None, f"{side} 신호 · 보유 중", stop_ticks)
        if self.day.bars_since_exit <= p.cooldown_bars:
            return Signal(bar, None, f"{side} 신호 · 청산 뒤 대기", stop_ticks)
        edge_ticks = abs(bar.middle - bar.close) / TICK
        if edge_ticks < p.min_edge_ticks:
            return Signal(bar, None, f"{side} 신호 · 중심선까지 {edge_ticks:.1f}틱 < {p.min_edge_ticks}", stop_ticks)
        if self.day.stops >= p.max_stops_per_day:
            return Signal(bar, None, f"{side} 신호 · 오늘 손절 {p.max_stops_per_day}회 도달", stop_ticks)
        where = "상단" if side == "sell" else "하단"
        why = f"{where} 이탈 뒤 복귀" if p.confirm else f"{where} 이탈"
        rsi_seen = prev.rsi if p.confirm else bar.rsi
        return Signal(bar, side, f"{why} · RSI {rsi_seen:.0f}", stop_ticks)

    # ---- 체결 쪽이 알려 주는 것 ----
    def on_entry(self) -> None:
        self.day.in_position = True

    def on_exit(self, pnl_won: float, stopped: bool) -> None:
        self.day.in_position = False
        self.day.realized_pnl += pnl_won
        self.day.stops += int(stopped)
        self.day.bars_since_exit = 0
