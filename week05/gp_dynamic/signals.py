"""시간 길이별 신호 — 봉 t 가 끝난 시점까지의 정보만 씁니다.

8종목 5분봉을 같은 시각끼리 맞춰서(모두 있는 봉만):
  sig   봉당 변동성 = 직전 VOL_WIN 봉 r² 평균의 제곱근. 바닥 = 직전 VOL_FLOOR_WIN 봉 sig 중앙값 × VOL_FLOOR
  fN    직전 N 봉 수익률 합 / (sig·√N), N = 3·12·48·288 (15분·1시간·4시간·하루)
        간격 봉(휴장 건넘) 수익률은 0으로 봄. 논문 식 31 의 "과거 수익률 / 표준편차" 신호를 장중 길이로 바꾼 것
  target  r_{t+1} / sig_t (계수 추정용, 다음 봉 표준화 수익률)
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

import config


@dataclass
class Panel:
    idx: pd.DatetimeIndex
    mem: list[str]
    O: pd.DataFrame
    C: pd.DataFrame
    Craw: pd.DataFrame             # 원래 가격 (계약 수 환산용)
    R: pd.DataFrame
    sig: pd.DataFrame
    F: dict[str, pd.DataFrame]     # 신호 이름 → (봉 × 종목)


def vol(R: pd.DataFrame) -> pd.DataFrame:
    s = np.sqrt((R ** 2).rolling(config.VOL_WIN, min_periods=config.VOL_WIN // 3).mean())
    floor = s.rolling(config.VOL_FLOOR_WIN, min_periods=config.VOL_WIN).median() * config.VOL_FLOOR
    return s.where(s >= floor, floor)


def build(bars: dict[str, pd.DataFrame], mem: list[str] = config.SYMS) -> Panel:
    P = pd.concat({s: np.log(bars[s]["c"]) for s in mem}, axis=1).dropna()
    idx = P.index
    R = pd.concat({s: bars[s]["r"] for s in mem}, axis=1).reindex(idx)
    O = pd.concat({s: bars[s]["o"] for s in mem}, axis=1).reindex(idx)
    C = pd.concat({s: bars[s]["c"] for s in mem}, axis=1).reindex(idx)
    Craw = pd.concat({s: bars[s]["c_raw"] for s in mem}, axis=1).reindex(idx)
    sig = vol(R)
    R0 = R.fillna(0.0)
    F = {k: R0.rolling(n).sum() / (sig * np.sqrt(n)) for k, n in config.HORIZONS.items()}
    return Panel(idx, list(mem), O, C, Craw, R, sig, F)


def target(p: Panel) -> pd.DataFrame:
    """r_{t+1} / sig_t. 다음 봉과 간격이 길면 r 이 NaN 이라 자동으로 빠짐."""
    return p.R.shift(-1) / p.sig
