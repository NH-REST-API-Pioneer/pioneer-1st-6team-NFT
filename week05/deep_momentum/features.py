"""특징·목표 — 날짜 t 종가까지의 정보만 씁니다 (논문 V-B).

  sigma_t   일 수익률 EWM 표준편차 (span 60)                         — 사전 변동성
  ret_N     직전 N 일 수익률 합 / (sigma_t · √N), N = 1·21·63·126·252   — 정규화 수익률
  macd_S_L  (EWM(S) − EWM(L)) / 63일 가격 표준편차 / 252일 그 값의 표준편차  — Baz 외 (2015) 식 4~6
            EWM 시간척도 S 는 반감기 log(0.5)/log(1 − 1/S)
  next_r    r_{t+1} (다음 날 수익률, 학습 목표에만 씀)
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import config


def _hl(s: int) -> float:
    return np.log(0.5) / np.log(1 - 1 / s)


def macd(px: pd.Series, s: int, l: int) -> pd.Series:
    m = px.ewm(halflife=_hl(s)).mean() - px.ewm(halflife=_hl(l)).mean()
    q = m / px.rolling(63).std()
    return q / q.rolling(252).std()


def build(d: pd.DataFrame) -> pd.DataFrame:
    r = d["r"]
    sigma = r.ewm(span=config.VOL_SPAN, min_periods=config.VOL_SPAN).std()
    px = np.exp(r.cumsum())          # 갭을 뺀 가격 (롤 점프 없음)
    f = pd.DataFrame(index=d.index)
    f["sigma"] = sigma
    for n in config.RET_WINDOWS:
        f[f"ret_{n}"] = r.rolling(n).sum() / (sigma * np.sqrt(n))
    for s, l in config.MACD_PAIRS:
        f[f"macd_{s}_{l}"] = macd(px, s, l)
    f["r"] = r
    f["next_r"] = r.shift(-1)
    f["px"] = d["px"]
    return f


FEATS = [f"ret_{n}" for n in config.RET_WINDOWS] + [f"macd_{s}_{l}" for s, l in config.MACD_PAIRS]


def panel(start: str | None = None) -> dict[str, pd.DataFrame]:
    import data

    out = {}
    for s in config.SYMS:
        f = build(data.load(s))
        out[s] = f.loc[start:] if start else f
    return out
