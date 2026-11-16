import numpy as np
import pandas as pd

import data
from helpers import raw_bars, walk


def test_gap_return_is_nan():
    ts = pd.DatetimeIndex(["2026-11-14 06:50", "2026-11-14 06:55", "2026-11-16 08:00", "2026-11-16 08:05"])
    df = data.clean(raw_bars(ts, [100, 100.1, 101, 101.1]))
    assert bool(df.loc["2026-11-16 08:00", "gap"])
    assert np.isnan(df.loc["2026-11-16 08:00", "r"])
    assert not bool(df.loc["2026-11-16 08:05", "gap"])
    assert np.isclose(df.loc["2026-11-16 08:05", "r"], np.log(101.1 / 101))


def test_roll_bar_excluded():
    # 롤: 휴장 직후 첫 봉에서 가격 수준이 뜀 (실제 ES 6/15·9/14 07:00 봉에서 확인)
    ts1 = pd.date_range("2026-11-16 08:00", periods=300, freq="5min")
    ts2 = pd.date_range(ts1[-1] + pd.Timedelta("65min"), periods=300, freq="5min")
    ts = ts1.append(ts2)
    _, c = walk(600)
    c[300:] *= 1.03
    df = data.clean(raw_bars(ts, c))
    assert bool(df["roll"].iloc[300]) and bool(df["gap"].iloc[300])
    assert np.isnan(df["r"].iloc[300])
    assert df["roll"].sum() == 1


def test_news_bar_kept():
    # 장중 단발 급변(지표 발표)은 진짜 변동성 → 수익률을 지우지 않음
    ts, c = walk(600)
    c[400:] *= 1.03
    df = data.clean(raw_bars(ts, c))
    assert df["roll"].sum() == 0
    assert np.isclose(df["r"].iloc[400], np.log(c[400] / c[399]))


def test_duplicates_dropped():
    ts, c = walk(5)
    raw = raw_bars(ts, c)
    df = data.clean(pd.concat([raw, raw.iloc[[2]]], ignore_index=True))
    assert len(df) == 5 and df.index.is_monotonic_increasing


def test_load_all_syms():
    import config
    for s in config.SYMS:
        df = data.load_bars(s)
        assert df.index.is_monotonic_increasing and df.index.is_unique
        assert df.index[0].strftime("%Y-%m-%d") == "2026-05-01"
        assert list(df.columns[:5]) == ["o", "h", "l", "c", "v"]
        assert {"r", "gap", "roll"} <= set(df.columns)
