"""Gârleanu·Pedersen (2013) 일봉 버전 — 비교 기준.

논문 VI 절처럼 신호 = 과거 수익률을 변동성으로 나눈 값. 논문은 5일·1년·5년인데 데이터가 2004년부터라
첫 학습 블록(2005~2010)에 5년 신호가 들어가지 않아 5일·3개월·1년으로 바꿈.
  f_k     = 직전 n_k 일 수익률 합 / (σ·√n_k),  n = 5·63·252
  추정    r_{t+1}/σ_t = Σ β_k f_k (학습 블록, 8종목 묶음),  φ_k = 1 − AR(1) (종목 평균)
  공분산  직전 252일 일 수익률, 상관을 0 쪽으로 50%
  포지션  aim = (γΣ)⁻¹ Σ_k β_k σ f_k / (1 + φ_k a/γ),   x_t = x_{t−1} + (a/λ)(aim − x_{t−1})    [식 10·15]
  γ       학습 구간 실현 변동성이 TSMOM 과 같아지게 (포지션 크기만 정함, a/λ·a/γ 는 k=λ/γ 에만 달림)
  k       학습 블록 마지막 10%(검증)에서 비용 후 샤프 1등 (DMN 과 같은 분할)
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import backtest
import config
import gp

HORIZ = {"g5": 5, "g63": 63, "g252": 252}
K_GRID = (0.1, 0.3, 1, 3, 10, 30, 100, 300)


def signals(P) -> dict[str, pd.DataFrame]:
    r = backtest.align(P, "r")
    sig = backtest.align(P, "sigma")
    return {k: r.fillna(0).rolling(n).sum() / (sig * np.sqrt(n)) for k, n in HORIZ.items()}


def fit(P, F, end: str) -> tuple[dict, dict]:
    nr = backtest.align(P, "next_r")
    sig = backtest.align(P, "sigma")
    y = (nr / sig).loc[:end]
    cols = list(F)
    X = pd.concat({k: F[k].loc[:end].stack() for k in cols}, axis=1)
    X["y"] = y.stack()
    X = X.replace([np.inf, -np.inf], np.nan).dropna()
    A = np.column_stack([np.ones(len(X))] + [X[c].values for c in cols])
    b, *_ = np.linalg.lstsq(A, X["y"].values, rcond=None)
    betas = dict(zip(cols, b[1:]))
    phis = {}
    for k in cols:
        v = []
        for s in F[k].columns:
            x = F[k][s].loc[:end].dropna().values
            v.append(1 - np.corrcoef(x[:-1], x[1:])[0, 1])
        phis[k] = float(np.mean(v))
    return betas, phis


def run(P, F, betas, phis, k: float, mode: str = "gp") -> pd.DataFrame:
    """비중 (γ=1 기준, 나중에 크기 맞춤)."""
    r = backtest.align(P, "r")
    sig = backtest.align(P, "sigma")
    cov = r.fillna(0).rolling(252, min_periods=126).cov()
    names = list(F)
    disc = gp.discount([phis[c] for c in names], k) if mode == "gp" else np.ones(len(names))
    rate = gp.rates(k)[1] if mode == "gp" else 1.0
    wvec = np.array([betas[c] for c in names]) * disc
    idx, cols = r.index, r.columns
    Fv = np.stack([F[c].reindex(idx)[cols].values for c in names])     # (신호, 날짜, 종목)
    S3 = cov.values.reshape(len(idx), len(cols), len(cols))
    sv = sig.values
    x = np.zeros(len(cols))
    out = np.full((len(idx), len(cols)), np.nan)
    for t in range(len(idx)):
        f = Fv[:, t, :]
        S = S3[t]
        ok = np.isfinite(f).all(axis=0) & np.isfinite(sv[t]) & np.isfinite(np.diag(S))
        if ok.sum() == 0:
            continue
        mu = np.zeros(len(cols))
        mu[ok] = sv[t, ok] * (wvec @ f[:, ok])
        Ss = gp.shrink(S[np.ix_(ok, ok)], config.COV_SHRINK if hasattr(config, "COV_SHRINK") else 0.5)
        aim = np.zeros(len(cols))
        aim[ok] = np.linalg.solve(Ss, mu[ok])
        x = x + rate * (aim - x)
        x[~ok] = 0.0
        out[t] = x
    return pd.DataFrame(out, index=idx, columns=cols)


def scale_to(W: pd.DataFrame, P, ref_vol: float, period) -> tuple[pd.DataFrame, float]:
    ev = backtest.evaluate(W, P, period)
    v = ev["gross"].std() * np.sqrt(252)
    s = ref_vol / v if v > 0 else 0.0
    return W * s, s
