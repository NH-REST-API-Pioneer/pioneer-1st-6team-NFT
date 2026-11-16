"""국면 — 예측 변동성을 그 종목의 직전 거래일 분포에서 백분위로 바꿔 고/저/중간으로 나눔.

백분위 = (직전 n_days 거래일, 당일 제외, 예측값 중 x 이하인 개수) / (그 개수 전체). 미래 정보 없음.
국면: > HI → high (돌파), < LO → low (평균회귀), 그 사이 mid (진입 안 함), 백분위 없음 → none.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import config


def percentile(pred: pd.Series, days: pd.Series, n_days: int = config.PCT_DAYS) -> pd.Series:
    """종목 하나. pred 와 days(거래일) 는 같은 길이·순서."""
    p = np.asarray(pred, float)
    d = np.asarray(days)
    out = np.full(len(p), np.nan)
    uniq, first = np.unique(d, return_index=True)
    order = np.argsort(first)
    uniq = uniq[order]
    groups = [np.flatnonzero(d == u) for u in uniq]
    vals = [np.sort(p[g][~np.isnan(p[g])]) for g in groups]
    for i in range(n_days, len(uniq)):
        pool = np.sort(np.concatenate(vals[i - n_days:i]))
        if not len(pool):
            continue
        g = groups[i]
        x = p[g]
        out[g] = np.where(np.isnan(x), np.nan, np.searchsorted(pool, x, side="right") / len(pool))
    return pd.Series(out, index=pred.index)


def label(pct: pd.Series, hi: float = config.HI, lo: float = config.LO) -> pd.Series:
    lab = np.where(pct > hi, "high", np.where(pct < lo, "low", "mid"))
    return pd.Series(np.where(pct.isna(), "none", lab), index=pct.index)


def label_two(pct: pd.Series) -> pd.Series:
    """변형: 0.5 기준 둘로만."""
    lab = np.where(pct > 0.5, "high", "low")
    return pd.Series(np.where(pct.isna(), "none", lab), index=pct.index)


def for_all(pred: pd.Series, n_days: int = config.PCT_DAYS) -> pd.Series:
    """(sym, ts) 인덱스의 예측값 → 종목별 백분위."""
    import session

    parts = []
    for sym, s in pred.groupby(level="sym", sort=False):
        ts = s.index.get_level_values("ts")
        parts.append(percentile(s, pd.Series(session.session_day(ts)), n_days))
    return pd.concat(parts).reindex(pred.index)


CLOCK_WIN = pd.Timedelta("28D")


def clock_pct(pct: pd.Series) -> pd.Series:
    """시계만 쓴 국면 기준: 같은 HH:MM 의 직전 28일(≈20거래일, 자기 자신 제외) 백분위 평균 (종목 하나, ts 인덱스).
    예측이 「시간대」 이상의 정보를 주는지 보려는 비교 대상."""
    tod = pct.index.strftime("%H:%M")
    return pct.groupby(tod).transform(lambda v: v.rolling(CLOCK_WIN, closed="left", min_periods=5).mean())
