"""MKU 상품 정보와 두 후보가 같이 쓰는 도구 — 1분봉 · 시각 계산 · ATR · 호가 맞추기 · 하루 상태.

2주차 strategy.py 에서 전략과 상관없는 부분만 떼어 와 MKU 에 맞게 고쳤습니다.

## 상품 (SGX KRW/USD Mini, NH 코드 MKU)

- 호가는 **원화 1,000원당 미국 달러** (KRW/USD) 입니다. 원달러 환율의 역수라서 환율이 오르면
  MKU 가격은 내려갑니다. MKU 를 사면 원화 강세(환율 하락)에, 팔면 원화 약세(환율 상승)에 겁니다.
- 1계약 = 25,000,000원, 1틱 = 0.0001 = US$2.50. 손익·수수료는 달러입니다.
  (NH 품목별세부정보 2026-09-09: 거래소 SGX, 통화 USD, ctr_unt 25000, tick_unt 0.0001, tick_val 2.5)
- 종목코드 = 품목 MKU + 월물 글자 + 연도 두 자리 (예: 2026년 11월물 MKUX26). 거래소코드는 FSGX.
- 시세는 "0.74350" 처럼 소수 다섯 자리 문자열로 옵니다 (마지막 자리는 늘 0, 호가는 0.0001 단위).
  (해외 모의서버 현재가·차트(분) 2026-11-03 조회)

## 시각 (모두 한국 시간 HHMMSS)

NH 장운영정보(한국 시간, 2026-09-09)로 MKU 는 1차 08:25~20:35 · 2차 20:50~익일 06:15 이고,
정산가는 20:35 에 정합니다. 해외 모의계좌가 07:00~24:00 이라 우리는 08:25~20:35 · 20:50~24:00 만 쓰고,
23:50 에 전량 청산해 날을 넘기지 않습니다. 해외 분봉 응답의 시각(exch_tm)·날짜(ref_dt)도 한국 시간이라
date 는 한국 날짜 그대로 씁니다 (영업일 glb_biz_dt 는 쓰지 않음).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

TICK = 0.0001        # 호가 단위 (US$ / 1,000원) — NH 품목별세부정보 tick_unt
TICK_VALUE = 2.5     # 1틱 가치 (US$) — tick_val. = 25,000,000원 ÷ 1,000 × 0.0001
PRICE_DECIMALS = 4   # 호가 소수 자리 (TICK 에 맞춤)

SESSION_START = "082500"
BREAK_START = "203500"   # SGX 1차 마감 · 2차 개장 사이 15분 — 이 사이 봉은 쓰지 않음
BREAK_END = "205000"
SESSION_END = "240000"   # 해외 모의계좌 운영 끝
FLATTEN_AT = "235000"    # 이 시각부터 전량 청산, 새 진입 없음


@dataclass(frozen=True)
class Minute:
    """1분봉 하나. time 은 한국 시간 HHMMSS (그 분의 시작 시각)."""
    date: str
    time: str
    open: float
    high: float
    low: float
    close: float
    volume: float


def minutes_of(hhmmss: str) -> int:
    return int(hhmmss[:2]) * 60 + int(hhmmss[2:4])


def hhmmss_of(minutes: int) -> str:
    return f"{minutes // 60:02d}{minutes % 60:02d}00"


def in_trading_hours(hhmmss: str) -> bool:
    """우리가 쓰는 시간 (08:25~20:35, 20:50~24:00) 안인가."""
    return SESSION_START <= hhmmss < BREAK_START or BREAK_END <= hhmmss < SESSION_END


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
    realized_pnl: float = 0.0       # US$
    stops: int = 0                  # 그날 손절로 끝난 거래 수
    in_position: bool = False
    bars_since_exit: int = 10**9


def ticks_between(a: float, b: float) -> int:
    """a − b 를 틱 수로 (부동소수 잡음은 반올림으로 없앰)."""
    return round((a - b) / TICK)


def stop_price(side: str, entry: float, stop_ticks: int) -> float:
    move = stop_ticks * TICK
    return round(entry - move if side == "buy" else entry + move, PRICE_DECIMALS)


def target_price(side: str, entry: float, target_ticks: int) -> float:
    move = target_ticks * TICK
    return round(entry + move if side == "buy" else entry - move, PRICE_DECIMALS)


def tick_up(price: float) -> float:
    """price 이상인 가장 가까운 호가. 이미 호가면 그대로 (부동소수 잡음은 6자리에서 버림)."""
    return round(math.ceil(round(price / TICK, 6)) * TICK, PRICE_DECIMALS)


def tick_down(price: float) -> float:
    """price 이하인 가장 가까운 호가."""
    return round(math.floor(round(price / TICK, 6)) * TICK, PRICE_DECIMALS)


def take_profit_price(side: str, target: float) -> float:
    """호가가 아닌 익절 목표(중심선 등)를 **불리한 쪽 호가**로 맞춘 값 (2주차 이슈 #1 과 같은 이유).

    매수 보유는 올림, 매도 보유는 내림.
    """
    return tick_up(target) if side == "buy" else tick_down(target)


def pnl_ticks(side: str, entry: float, exit_: float) -> int:
    return ticks_between(exit_, entry) if side == "buy" else ticks_between(entry, exit_)


def won_view(side: str) -> str:
    """MKU 매매 방향을 원달러 환율 쪽 말로 — 로그에 같이 적습니다."""
    return "원화 강세(환율 하락) 쪽" if side == "buy" else "원화 약세(환율 상승) 쪽"
