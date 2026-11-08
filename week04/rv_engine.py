"""금·은 롤링 β 상대가치 — 판단 엔진. 실행기(rv_runner.py)와 리플레이 테스트가 같이 씁니다.

채택값 (backtest_rv_full.py --news, 2026-11-08): L72 · 진입 3.5σ · 청산 0σ · 손절 1.5σ + 미국 지표 발표 금지.
  선택: 손절을 필수로 두고(설계 원칙) 학습 이웃 평균 상위 중, 실행 기록을 남길 만큼 거래가 나오는 값
  (하루 약 0.6건). 근거 문서 6-4절.

봉 t 가 끝났을 때 (5분봉, 봉 시각 = 봉 시작, KST)
  직전 L개 봉(t 포함)으로 log GC = a + β·log SI → z = 잔차_t / σ
  창(L개 봉)이 휴장·주말을 건너면(시간 폭 > (L−1)·5 + 15분) 그 봉 신호는 버림
  포지션 없음: z > k_in → 스프레드 매도(금 매도·은 매수), z < −k_in → 스프레드 매수. 진입 때 β·σ·스프레드 고정
  포지션 있음: 다음 중 하나면 청산
    - z 가 k_out 을 건넘 (매도였으면 z < k_out, 매수였으면 z > −k_out)
    - 손절: 진입 β 로 고정한 스프레드가 진입 때보다 불리하게 σ_진입 × stop_m 움직임
    - 미국 지표 발표 구간 (뉴욕 08:15~09:00) — 진입도 안 함
    - 휴장 직전 봉 (brk) — 진입도 안 함. 포지션을 휴장·주말 너머로 안 들고 감
  청산한 봉에서는 새로 진입하지 않음 (백테스트와 같음)
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class RVParams:
    L: int = 72
    k_in: float = 3.5
    k_out: float = 0.0
    stop_m: float = 1.5
    news: bool = True


def news_blackout(ts: pd.Timestamp) -> bool:
    """뉴욕 현지 08:15~09:00 이면 True. ts 는 KST(시간대 없음)."""
    ny = ts.tz_localize("Asia/Seoul").tz_convert("America/New_York")
    m = ny.hour * 60 + ny.minute
    return 8 * 60 + 15 <= m < 9 * 60


def fit_last(yc: np.ndarray, xc: np.ndarray):
    """창 전체로 최소제곱 → 마지막 봉의 (z, β, σ). backtest_rv_full.rolling_zs 와 같은 식."""
    xm, ym = xc.mean(), yc.mean()
    vx = ((xc - xm) ** 2).sum()
    if vx <= 0:
        return np.nan, np.nan, np.nan
    b = ((xc - xm) * (yc - ym)).sum() / vx
    e = yc - ym - b * (xc - xm)
    sd = e.std()
    if sd <= 0:
        return np.nan, np.nan, np.nan
    return e[-1] / sd, b, sd


@dataclass
class Position:
    side: int            # +1 스프레드 매수(금 매수·은 매도), −1 스프레드 매도
    beta: float
    s0: float            # 진입 신호 봉의 log GC − β·log SI
    sd0: float
    entry_bar: pd.Timestamp
    extra: dict = field(default_factory=dict)  # 실행기가 체결가·수량 등을 붙임


class RVEngine:
    def __init__(self, p: RVParams | None = None):
        self.p = p or RVParams()
        self.pos: Position | None = None

    def step(self, ts: pd.DatetimeIndex, yc: np.ndarray, xc: np.ndarray, brk: bool) -> dict:
        """마지막 봉(t)이 끝났을 때 판단. yc·xc 는 log 종가, 길이 ≥ L. brk = 이 봉 다음이 휴장.

        돌려주는 값: {"act": "enter"|"exit"|None, ...판단 근거}
        """
        p = self.p
        t = len(yc) - 1
        info = {"bar": ts[t], "act": None}
        if len(yc) >= p.L:
            z, b, sd = fit_last(yc[-p.L:], xc[-p.L:])
            span_min = (ts[t] - ts[t - p.L + 1]).total_seconds() / 60
            ok = span_min <= (p.L - 1) * 5 + 15
        else:
            z = b = sd = np.nan
            ok = False
        blk = p.news and news_blackout(ts[t])
        info.update(z=z, beta=b, sd=sd, ok=ok, blk=blk, brk=brk)

        if self.pos is not None:
            q = self.pos
            adverse = -q.side * ((yc[t] - q.beta * xc[t]) - q.s0)
            info["adverse_sd"] = adverse / q.sd0
            reason = None
            if brk:
                reason = "휴장 전 정리"
            elif blk:
                reason = "지표 발표 구간"
            elif adverse > p.stop_m * q.sd0:
                reason = "손절"
            elif np.isnan(z):
                reason = "z 계산 불가"
            elif (q.side == -1 and z < p.k_out) or (q.side == 1 and z > -p.k_out):
                reason = "평균 복귀"
            if reason:
                info.update(act="exit", reason=reason, side=q.side)
            return info

        if brk or blk or not ok or np.isnan(z):
            return info
        side = -1 if z > p.k_in else 1 if z < -p.k_in else 0
        if side:
            info.update(act="enter", side=side, s0=yc[t] - b * xc[t])
        return info

    def filled_entry(self, info: dict, **extra) -> None:
        self.pos = Position(info["side"], info["beta"], info["s0"], info["sd"], info["bar"], dict(extra))

    def filled_exit(self) -> Position:
        q, self.pos = self.pos, None
        return q
