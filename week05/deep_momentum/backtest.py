"""포트폴리오 평가 — 논문 식 1·35.

  종목 i 의 비중  w_{i,t} = X_{i,t} · σ_tgt / (σ_{i,t}·√252) / N   (σ 는 일 변동성, σ_tgt 연 15%)        (X ∈ [−1, 1], 날짜 t 종가에 정함)
  포트폴리오 수익 R_{t+1} = Σ_i w_{i,t} r_{i,t+1} − Σ_i c_{i,t} |w_{i,t} − w_{i,t−1}|
  c_{i,t} = 계약당 편도 비용 / 계약 명목금액 = (수수료 $2 + 반 틱 × 포인트가치) / (가격 × 포인트가치)
  → 비중(자본 대비 명목) 1 을 바꾸는 데 드는 비용 (분수). 계약 반올림은 여기서 하지 않음 (실행기에서).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import config


def cost_rate(px: pd.DataFrame) -> pd.DataFrame:
    out = {}
    for s in px.columns:
        pv, tick = config.CONTRACT[s]
        out[s] = (config.FEE_SIDE + config.HALF_SPREAD_TICKS * tick * pv) / (px[s] * pv)
    return pd.DataFrame(out, index=px.index)


def align(P: dict[str, pd.DataFrame], col: str) -> pd.DataFrame:
    return pd.concat({s: f[col] for s, f in P.items()}, axis=1).sort_index()


def weights(X: pd.DataFrame, sigma: pd.DataFrame) -> pd.DataFrame:
    n = X.notna().sum(axis=1).replace(0, np.nan)
    return (X * config.SIGMA_TGT / (sigma * np.sqrt(252))).div(n, axis=0)   # sigma 는 일 단위


def evaluate(W: pd.DataFrame, P: dict[str, pd.DataFrame], period: tuple[str, str]) -> dict:
    """W: 날짜 t 에 정한 비중 (자본 대비 명목). 기간 안 날짜의 다음날 수익을 셈."""
    nr = align(P, "next_r").reindex(W.index)
    c = cost_rate(align(P, "px")).reindex(W.index)
    W = W.reindex(columns=nr.columns).fillna(0.0)
    turn = W.diff().abs()
    turn.iloc[0] = W.iloc[0].abs()
    gross = (W * nr.fillna(0.0)).sum(axis=1)
    cost = (turn * c.fillna(0.0)).sum(axis=1)
    m = (W.index >= period[0]) & (W.index <= period[1]) & nr.notna().any(axis=1).values
    g, k = gross[m], cost[m]
    net = g - k
    return {"gross": g, "net": net, "cost": k, "turnover": turn[m].sum(axis=1), "W": W[m]}


def stats(ret: pd.Series) -> dict:
    if len(ret) < 2 or ret.std() == 0:
        return {"ann_ret": 0.0, "ann_vol": 0.0, "sharpe": 0.0, "mdd": 0.0, "hit": 0.0}
    eq = ret.cumsum()
    return {"ann_ret": float(ret.mean() * 252), "ann_vol": float(ret.std() * np.sqrt(252)),
            "sharpe": float(ret.mean() / ret.std() * np.sqrt(252)),
            "mdd": float((eq - eq.cummax()).min()), "hit": float((ret > 0).mean())}


def summary(ev: dict) -> dict:
    sg, sn = stats(ev["gross"]), stats(ev["net"])
    return {"sharpe_gross": sg["sharpe"], "sharpe_net": sn["sharpe"], "ret_net": sn["ann_ret"],
            "vol": sn["ann_vol"], "mdd": sn["mdd"], "hit": sn["hit"],
            "cost_ann": float(ev["cost"].mean() * 252), "turnover_ann": float(ev["turnover"].mean() * 252),
            "days": int(len(ev["net"]))}
