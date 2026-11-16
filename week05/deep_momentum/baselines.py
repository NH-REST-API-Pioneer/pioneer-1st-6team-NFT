"""논문의 기준선 신호 X ∈ [−1, 1] (날짜 × 종목).

  long_only   X = 1
  tsmom       X = sgn(지난 252일 수익률)                       — Moskowitz·Ooi·Pedersen (2012)
  macd        X = 평균_k φ(Y_k), φ(y) = y·exp(−y²/4)/0.89      — Baz 외 (2015), (S,L) = (8,24)(16,48)(32,96)
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import backtest
import config


def long_only(P) -> pd.DataFrame:
    return backtest.align(P, "sigma").notna().astype(float).where(backtest.align(P, "sigma").notna())


def tsmom(P) -> pd.DataFrame:
    return np.sign(backtest.align(P, "ret_252"))


def phi(y):
    return y * np.exp(-(y ** 2) / 4) / 0.89


def macd(P) -> pd.DataFrame:
    cols = [f"macd_{s}_{l}" for s, l in config.MACD_PAIRS]
    return sum(phi(backtest.align(P, c)) for c in cols) / len(cols)
