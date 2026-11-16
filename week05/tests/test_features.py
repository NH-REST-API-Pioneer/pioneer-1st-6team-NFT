import numpy as np
import pandas as pd
import pytest

import data
import features
import session
from helpers import raw_bars, walk

N = 288 * 26   # 26거래일 연속 봉


def _bars(seed_es=1, seed_gc=2, n=N):
    ts, ces = walk(n, start="2026-05-04 08:00", seed=seed_es)
    _, cgc = walk(n, start="2026-05-04 08:00", seed=seed_gc)
    vol = np.random.default_rng(3).integers(50, 150, n)
    return {"ES": data.clean(raw_bars(ts, ces, vol)), "GC": data.clean(raw_bars(ts, cgc, vol))}


@pytest.fixture(scope="module")
def built():
    return _bars(), features.build(_bars())


def test_no_lookahead(built):
    bars, f = built
    t = bars["GC"].index[-500]
    b2 = {}
    for k, v in bars.items():
        v = v.copy()
        after = v.index > t
        v.loc[after, "r"] = np.random.default_rng(9).normal(0, 0.01, after.sum())
        v.loc[after, ["o", "h", "l", "c"]] *= 1.05
        v.loc[after, "v"] = 999
        b2[k] = v
    f2 = features.build(b2)
    for sym in ("ES", "GC"):
        x, y = f.loc[(sym, t), features.FEATURES], f2.loc[(sym, t), features.FEATURES]
        pd.testing.assert_series_equal(x.astype(float), y.astype(float), check_names=False)
        assert f.loc[(sym, t), "norm"] == f2.loc[(sym, t), "norm"]


def test_norm_uses_prior_days_only(built):
    bars, f = built
    gc = bars["GC"].copy()
    day = session.session_day(gc.index)
    today = np.asarray(day == day[-1])
    gc.loc[today, "r"] *= 10
    f2 = features.build({"ES": bars["ES"], "GC": gc})
    t = gc.index[today][0]
    assert np.isclose(f.loc[("GC", t), "norm"], f2.loc[("GC", t), "norm"])


def test_first_days_have_no_norm(built):
    _, f = built
    g = f.loc["GC"]
    assert g["norm"].iloc[:288 * 19].isna().all()
    assert g["norm"].iloc[-1] > 0


def test_target_nan_over_gap():
    ts, c = walk(N, start="2026-05-04 08:00")
    ts = ts.where(np.arange(N) < N - 100, ts + pd.Timedelta("2h"))   # 끝에서 100봉 전에 2시간 간격
    f = features.build({"GC": data.clean(raw_bars(pd.DatetimeIndex(ts), c))}).loc["GC"]
    k = N - 100   # 간격 봉
    assert np.isnan(f["y"].iloc[k - 1]) and np.isnan(f["y"].iloc[k - 12])
    assert not np.isnan(f["y"].iloc[k - 13])
    assert np.isnan(f["y"].iloc[-1])           # 뒤에 12봉이 없음


def test_target_definition(built):
    bars, f = built
    gc = bars["GC"]
    i = len(gc) - 400
    t = gc.index[i]
    want = np.log(np.sqrt((gc["r"].iloc[i + 1:i + 13] ** 2).sum()) / f.loc[("GC", t), "norm"])
    assert np.isclose(f.loc[("GC", t), "y"], want)


def test_es_feature_aligned(built):
    _, f = built
    t = f.loc["GC"].index[-300]
    assert f.loc[("GC", t), "es_rv12"] == f.loc[("ES", t), "rv12"]


def test_vratio_time_of_day():
    ts, c = walk(N, start="2026-05-04 08:00")
    vol = 10 + (ts.hour * 60 + ts.minute) % 37     # 시각마다 다르지만 날마다 같음
    f = features.build({"GC": data.clean(raw_bars(ts, c, np.asarray(vol)))}).loc["GC"]
    assert np.allclose(f["vratio"].iloc[-288:], 1.0)


def test_vratio_independent_of_history_length():
    # 06:00 봉이 날마다 있지 않은 경우(실제 ES: 49일 중 드문드문) — 앞 기간을 잘라도 마지막 날 vratio 가 같아야
    # 실행기(75일만 받음) = 백테스트(전체 기간)
    ts = pd.date_range("2026-07-01 06:00", periods=288 * 90, freq="5min")
    rng = np.random.default_rng(8)
    rare_days = set(pd.date_range("2026-07-01", periods=90)[rng.random(90) < 0.3].date)
    keep = ~((ts.hour == 6) & (ts.minute == 0)) | np.array([d in rare_days for d in ts.date])
    ts = ts[keep]
    _, c = walk(len(ts), start="2026-07-01 06:00")
    vol = rng.integers(10, 500, len(ts))
    full = data.clean(raw_bars(ts, c, vol))
    cut = full[full.index >= full.index[-1] - pd.Timedelta("45D")]
    since = cut.index[0] + pd.Timedelta("30D")           # 실행기 준비 창(28일)이 다 찬 뒤
    a = features.build({"GC": full}).loc["GC"]["vratio"]
    b = features.build({"GC": cut}).loc["GC"]["vratio"]
    a, b = a[a.index >= since], b[b.index >= since]
    assert (a.index.strftime("%H:%M") == "06:00").sum() >= 3
    assert np.allclose(a, b, equal_nan=True)
