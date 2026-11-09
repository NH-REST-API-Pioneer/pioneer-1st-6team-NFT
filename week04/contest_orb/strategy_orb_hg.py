"""런던 개장 돌파 (구리 HG) — 11/13 대회용 앱 1분봉 전략. 3주차 후보 B 규칙을 런던 08:00 개장에 맞춘 것.

앱 「전략주문 › 파이썬 전략」 에 올리는 파일 하나입니다. 표준 라이브러리만 씁니다(옆 파일 import 없음).
규칙은 `orb_backtest.py` 와 같습니다 (`Rule(london_open="0800")`, 3주차 채택값):

- 개장: 런던 현지 08:00. 한국 시간은 영국 서머타임이면 16:00, 아니면 17:00 (10/25 끝, 대회 날 11/13 은 17:00).
  날짜마다 계산합니다(3월·10월 마지막 일요일 규칙) — 시간대 DB 없이.
- 기준 범위: 개장부터 `range_min` 분(17:00~17:09) 1분봉 최고·최저. 폭이 `min_range_ticks` 틱 미만이면 그날 진입 안 함.
- 진입: 범위가 정해진 뒤 `entry_min` 분 안(늦어도 17:56 봉)에 1분봉 **종가**가 범위 밖으로 1틱 이상 → 그 방향.
  앱이 다음 분 시가에 진입합니다(종이 체결은 불리한 쪽 1틱). 하루 한 번.
- 손절: 범위 반대편 끝. 익절: 진입가 ± 범위 폭 × `target_r`. 같은 봉에서 둘 다 닿으면 손절.
- 시간 청산: 진입한 봉 + `hold_min` 분 봉, 늦어도 `last_bar`(17:58) 봉이 끝날 때. 앱의 `flatten_at`(17:59)이 마지막 안전망.

`exit_reason` 의 시각: 앱은 리플레이에서 한 분에 두 번 묻습니다 — 시가(`low`·`high` None, 그 분 시작)와 종가·고저
(그 분 끝). 그래서 고저를 받은 물음은 「그 분이 끝난 시각」(+1분)으로 봅니다. 장중엔 현재가 하나로만 물어
지금 시각 그대로입니다. 이렇게 하면 「17:58 봉 종가에 정리」가 리플레이는 17:58 봉 종가, 장중은 17:59:00 가격이 됩니다.

백테스트와 다른 점: 익절도 앱 종이 체결은 불리한 쪽 1틱을 뺍니다(백테스트는 익절 지정가로 슬리피지 없음).
실제 모의 주문은 앱이 언제나 시장가로 냅니다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

TITLE = "런던 개장 돌파 (구리 HGZ26)"
NAME = "orb_hg"
TICK = 0.0005          # HG 호가 단위 (USD/lb)
TICK_VALUE = 12.5      # 1틱 = US$12.5 (25,000 lb)
WINDOW = "런던"        # 개장 이름 — 앱이 같은 날 다시 세운 실행에 「이미 진입함」을 이어 줌


@dataclass(frozen=True)
class Params:
    london_open: str = "0800"      # 런던 현지 HHMM
    range_min: int = 10            # 기준 범위 분
    entry_min: int = 60            # 범위가 정해진 뒤 진입 신호를 보는 분
    min_range_ticks: int = 4       # 범위 폭이 이보다 좁으면 그날 쉼
    breakout_ticks: int = 1        # 종가가 범위 밖으로 이만큼 나가야 돌파
    target_r: float = 2.0          # 익절 = 진입가 ± 범위 폭 × target_r
    hold_min: int = 60             # 진입한 봉 + 이만큼 분 봉의 끝에 시간 청산
    last_bar: str = "175800"       # 이 봉이 끝나면 정리 (대회 끝 18:00)
    # 앱이 읽는 시간 칸 (한국시간)
    session_start: str = "160000"  # 서머타임 기간 개장(16:00)도 덮게
    session_end: str = "180000"
    entry_start: str = "160000"
    entry_end: str = "175800"      # 17:56 봉 신호 → 17:57 시가 진입까지
    flatten_at: str = "175900"     # 앱의 장 마감 정리 (전략의 17:58 봉 정리 뒤 안전망)


@dataclass(frozen=True)
class Signal:
    minute: object                 # 판정 1분봉 (앱이 기록·차트에 씀)
    side: str | None
    reason: str
    stop_ticks: int = 0            # 앱 표시용 손절 폭(판정 봉 종가 기준 어림). 실제 손절은 exit_reason 의 범위 끝
    window: str = WINDOW
    range_high: float = 0.0
    range_low: float = 0.0
    range_ticks: int = 0
    target_ticks: int = 0


@dataclass
class Day:
    """하루 상태. 앱이 `stops`(앞선 실행 손절 수)·`in_position` 을 읽고 씁니다."""

    date: str
    open_kst: str
    stops: int = 0
    in_position: bool = False
    entered: bool = False          # 오늘 진입 신호를 냈거나 진입함 (하루 한 번)
    hi: float | None = None
    lo: float | None = None
    range_done: bool = False
    skip: str = ""


# ---------------------------------------------------------------- 시각 계산
def minutes_of(hhmmss: str) -> int:
    return int(hhmmss[:2]) * 60 + int(hhmmss[2:4])


def hhmmss_of(minutes: int) -> str:
    return f"{minutes // 60:02d}{minutes % 60:02d}00"


def _last_sunday(year: int, month: int) -> date:
    first_next = date(year + (month == 12), month % 12 + 1, 1)
    last = first_next - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - 6) % 7)


def london_to_kst_hours(day: str) -> int:
    """런던 현지 → 한국 시간 시차. 영국 서머타임(3월 마지막 일요일 ~ 10월 마지막 일요일 전날)이면 8, 아니면 9."""
    d = date(int(day[:4]), int(day[4:6]), int(day[6:]))
    bst = _last_sunday(d.year, 3) <= d < _last_sunday(d.year, 10)
    return 8 if bst else 9


def kst_open(day: str, london_hhmm: str) -> str:
    return hhmmss_of((int(london_hhmm[:2]) + london_to_kst_hours(day)) * 60 + int(london_hhmm[2:]))


# ---------------------------------------------------------------- 전략
class LondonOrbHG:
    """런던 08:00 개장 10분 범위 돌파, 범위 반대편 손절 · 폭 × 2 익절 · 60분 / 17:58 정리."""

    def __init__(self, p: Params):
        self.p = p
        self.day: Day | None = None

    def _new_day(self, d: str) -> Day:
        return Day(date=d, open_kst=kst_open(d, self.p.london_open))

    def on_minute(self, m) -> list[Signal]:
        if self.day is None or self.day.date != m.date:
            self.day = self._new_day(m.date)
        day, p = self.day, self.p
        t0 = minutes_of(day.open_kst)
        t = minutes_of(m.time)
        range_end = t0 + p.range_min
        if t < t0:
            return []
        if t < range_end:                               # 기준 범위 쌓기
            day.hi = m.high if day.hi is None else max(day.hi, m.high)
            day.lo = m.low if day.lo is None else min(day.lo, m.low)
            return []
        out: list[Signal] = []
        if not day.range_done:                          # 범위 끝난 뒤 첫 봉에서 한 번 정함
            day.range_done = True
            if day.hi is None:
                day.skip = "개장 범위 봉 없음"
            else:
                width = round((day.hi - day.lo) / TICK)
                if width < p.min_range_ticks:
                    day.skip = f"범위 {width}틱 < {p.min_range_ticks}틱"
            out.append(self._info(m, day.skip or "기준 범위 확정"))
        if day.skip or day.entered or day.in_position:
            return out
        entry_last = min(range_end + p.entry_min, minutes_of(p.last_bar) - 2)
        if t > entry_last:
            return out
        step = p.breakout_ticks * TICK
        side = ("buy" if m.close >= day.hi + step - 1e-12 else
                "sell" if m.close <= day.lo - step + 1e-12 else None)
        if side is None:
            return out
        day.entered = True
        width = round((day.hi - day.lo) / TICK)
        stop = day.lo if side == "buy" else day.hi
        stop_ticks = max(1, round(abs(m.close - stop) / TICK) + 1)
        out.append(Signal(minute=m, side=side, reason=f"범위 {'위' if side == 'buy' else '아래'}로 돌파 (종가 {m.close:g})",
                          stop_ticks=stop_ticks, range_high=day.hi, range_low=day.lo, range_ticks=width,
                          target_ticks=round(width * p.target_r)))
        return out

    def _info(self, m, reason: str) -> Signal:
        day = self.day
        width = 0 if day.hi is None else round((day.hi - day.lo) / TICK)
        return Signal(minute=m, side=None, reason=reason, range_high=day.hi or 0.0, range_low=day.lo or 0.0,
                      range_ticks=width, target_ticks=round(width * self.p.target_r))

    # ---- 앱이 부르는 것
    def on_entry(self, window: str = WINDOW) -> None:
        if self.day is not None:
            self.day.entered = True
            self.day.in_position = True

    def on_exit(self, pnl, stopped: bool) -> None:
        if self.day is not None:
            self.day.in_position = False
            self.day.stops += bool(stopped)

    @staticmethod
    def _levels(pos: dict) -> tuple[float | None, float | None]:
        """(손절가, 익절가) — 진입 신호의 범위 끝과 진입가 ± 목표 틱. 신호를 모르면 앱 손절가, 익절 없음."""
        s = pos.get("signal")
        if s is None:
            return pos.get("stop"), None
        buy = pos["side"] == "buy"
        stop = s.range_low if buy else s.range_high
        target = round(pos["entry"] + (1 if buy else -1) * s.target_ticks * TICK, 4)
        return stop, target

    def _deadline_end(self, pos: dict) -> int:
        """시간 청산 시각(분) = 기한 봉이 끝나는 때. 기한 봉 = min(진입한 봉 + hold_min, last_bar)."""
        bar = min(minutes_of(pos["entry_time"]) + self.p.hold_min, minutes_of(self.p.last_bar))
        return bar + 1

    def exit_reason(self, clock: str, price: float, low: float | None, high: float | None, pos: dict):
        single = low is None or high is None
        now = minutes_of(clock) + (0 if single else 1)   # 고저를 받은 물음 = 그 분의 끝
        stop, target = self._levels(pos)
        buy = pos["side"] == "buy"
        lo, hi = (price, price) if single else (low, high)
        if stop is not None and (lo <= stop if buy else hi >= stop):
            return ("손절", price if single else stop)
        if target is not None and (hi >= target if buy else lo <= target):
            return ("익절", price if single else target)
        if now >= self._deadline_end(pos):
            return "시간 청산"
        return None

    def exit_levels(self, pos: dict) -> dict:
        stop, target = self._levels(pos)
        return {"stop": stop, "target": target}


STRATEGY = LondonOrbHG
