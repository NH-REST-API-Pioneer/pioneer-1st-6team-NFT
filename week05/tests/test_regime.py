import numpy as np
import pandas as pd

import regime


def _series(n_days=25, per_day=10, seed=0):
    rng = np.random.default_rng(seed)
    days = pd.Series(np.repeat(pd.date_range("2026-06-01", periods=n_days), per_day))
    pred = pd.Series(rng.normal(0, 1, len(days)))
    return pred, days


def test_needs_20_days():
    pred, days = _series()
    pct = regime.percentile(pred, days, 20)
    assert pct.iloc[:200].isna().all() and pct.iloc[200:].notna().all()


def test_percentile_excludes_today():
    pred, days = _series()
    p1 = regime.percentile(pred, days, 20)
    pred2 = pred.copy()
    pred2.iloc[221:230] = 100.0          # 21일째의 나머지 값을 바꿔도
    p2 = regime.percentile(pred2, days, 20)
    assert p1.iloc[220] == p2.iloc[220]  # 그날 첫 봉 백분위는 그대로


def test_percentile_range():
    pred, days = _series()
    pred.iloc[230] = 99.0
    pred.iloc[231] = -99.0
    pct = regime.percentile(pred, days, 20)
    assert pct.iloc[230] == 1.0 and pct.iloc[231] == 0.0


def test_percentile_value():
    days = pd.Series(np.repeat(pd.date_range("2026-06-01", periods=3), 4))
    pred = pd.Series([1, 2, 3, 4, 5, 6, 7, 8, 4.5, 0, 9, 8])
    pct = regime.percentile(pred, days, 2)
    assert pct.iloc[8] == 0.5            # 직전 2일 [1..8] 중 4.5 이하 4개 / 8
    assert pct.iloc[11] == 1.0


def test_labels():
    lab = regime.label(pd.Series([0.71, 0.70, 0.29, 0.30, np.nan]))
    assert list(lab) == ["high", "mid", "low", "mid", "none"]
    two = regime.label_two(pd.Series([0.5, 0.51, np.nan]))
    assert list(two) == ["low", "high", "none"]


def test_nan_pred_excluded_from_pool():
    pred, days = _series()
    pred.iloc[0:5] = np.nan
    pct = regime.percentile(pred, days, 20)
    assert pct.iloc[200:].notna().all()


def test_for_all_per_symbol():
    ts = pd.date_range("2026-06-01 08:00", periods=288 * 22, freq="5min")
    a = pd.Series(np.random.default_rng(1).normal(0, 1, len(ts)))
    idx = pd.MultiIndex.from_arrays([["ES"] * len(ts) + ["GC"] * len(ts), ts.append(ts)], names=["sym", "ts"])
    pred = pd.Series(np.concatenate([a, a + 50]), index=idx)   # GC 는 수준이 달라도
    pct = regime.for_all(pred)
    assert np.allclose(pct.loc["ES"].dropna(), pct.loc["GC"].dropna())  # 종목 자기 분포 기준이라 같음


def test_percentile_pool_unsorted_days():
    days = pd.Series(np.repeat(pd.date_range("2026-06-01", periods=3), 4))
    pred = pd.Series([5, 6, 7, 8, 1, 2, 3, 4, 4.5, 0, 9, 8])   # 앞날이 더 큼 → 이어 붙이면 정렬 안 됨
    pct = regime.percentile(pred, days, 2)
    assert pct.iloc[8] == 0.5 and pct.iloc[9] == 0.0 and pct.iloc[11] == 1.0


def test_stationary_shares():
    pred, days = _series(n_days=120, per_day=50, seed=3)
    lab = regime.label(regime.percentile(pred, days, 20))
    share = lab[lab != "none"].value_counts(normalize=True)
    assert abs(share["high"] - 0.30) < 0.02 and abs(share["low"] - 0.30) < 0.02


def test_clock_pct_prior_same_time():
    ts = pd.date_range("2026-06-01 08:00", periods=288 * 40, freq="5min")
    pct = pd.Series(np.random.default_rng(2).random(len(ts)), index=ts)
    cp = regime.clock_pct(pct)
    t = ts[-1]
    same = pct[(pct.index.strftime("%H:%M") == t.strftime("%H:%M")) & (pct.index < t)
               & (pct.index >= t - pd.Timedelta("28D"))]
    assert np.isclose(cp.iloc[-1], same.mean())
    pct2 = pct.copy(); pct2.iloc[-1] = 99
    assert regime.clock_pct(pct2).iloc[-1] == cp.iloc[-1]     # 자기 자신은 안 씀
