"""테스트용 합성 봉."""
from __future__ import annotations

import numpy as np
import pandas as pd


def raw_bars(ts: pd.DatetimeIndex, close: np.ndarray, vol: np.ndarray | None = None) -> pd.DataFrame:
    """fetch_long.py CSV 와 같은 열의 원본 표."""
    close = np.asarray(close, float)
    vol = np.full(len(ts), 100) if vol is None else vol
    return pd.DataFrame({
        "ref_dt": ts.strftime("%Y%m%d"), "exch_tm": ts.strftime("%H%M%S"),
        "open_pric": close, "high_pric": close * 1.0001, "low_pric": close * 0.9999,
        "close_pric": close, "cum_trd_qty": vol,
    })


def walk(n: int, start="2026-11-16 08:00", sd=1e-4, seed=0, p0=100.0) -> tuple[pd.DatetimeIndex, np.ndarray]:
    ts = pd.date_range(start, periods=n, freq="5min")
    r = np.random.default_rng(seed).normal(0, sd, n)
    return ts, p0 * np.exp(np.cumsum(r))
