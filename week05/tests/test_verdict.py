import numpy as np
import pandas as pd
import pytest

import verdict


def _trades(strat, labels, n=600, eff=5.0, seed=0, sign=1):
    rng = np.random.default_rng(seed)
    pos = np.sort(rng.choice(len(labels), n, replace=False))
    lab = labels.to_numpy()[pos]
    base = np.where(lab == "high", eff, np.where(lab == "low", -eff, 0.0)) * sign
    return pd.DataFrame({"sym": "ES", "strat": strat, "entry_bar": labels.index[pos], "regime": lab,
                         "net_bp": base + rng.normal(0, 3, n), "gross_bp": base + 1 + rng.normal(0, 3, n)})


def _labels(days=60, seed=11):
    """하루 단위로 high/low/mid 를 무작위로 (날마다 다름)."""
    ts = pd.date_range("2026-06-01 08:00", periods=288 * days, freq="5min")
    per_day = np.random.default_rng(seed).integers(0, 3, days)
    return pd.Series(np.array(["high", "low", "mid"])[per_day[np.arange(len(ts)) // 288]], index=ts)


def _clock_labels(days=60):
    """국면이 시각으로만 정해짐 (밤 high, 낮 low) — 예측이 시계 이상을 못 하는 경우."""
    ts = pd.date_range("2026-06-01 08:00", periods=288 * days, freq="5min")
    lab = np.where(ts.hour >= 20, "high", np.where((ts.hour >= 10) & (ts.hour < 16), "low", "mid"))
    return pd.Series(lab, index=ts)


def test_interaction_formula():
    brk = pd.DataFrame({"regime": ["high", "high", "low"], "net_bp": [4.0, 2.0, -1.0], "gross_bp": [5, 3, 0]})
    mr = pd.DataFrame({"regime": ["high", "low", "low"], "net_bp": [-2.0, 1.0, 3.0], "gross_bp": [-1, 2, 4]})
    t2 = verdict.table2x2(brk, mr)
    assert t2.loc["brk", "net_high"] == 3.0 and t2.loc["mr", "net_low"] == 2.0
    assert verdict.interaction(t2) == (3.0 - (-1.0)) - (-2.0 - 2.0)
    assert verdict.interaction(t2, "gross") == (4 - 0) - (-1 - 3)


def test_permute_detects_effect():
    lab = _labels()
    brk = _trades("brk", lab, seed=1)
    mr = _trades("mr", lab, seed=2, sign=-1)
    res = verdict.permute(brk, mr, {"ES": lab}, n=500, seed=0)
    assert res["obs"] > 15 and res["p"] < 0.01 and res["switch_p"] < 0.01


def test_permute_null_uniformish():
    lab = _labels()
    brk = _trades("brk", lab, seed=3, eff=0.0)
    mr = _trades("mr", lab, seed=4, eff=0.0)
    res = verdict.permute(brk, mr, {"ES": lab}, n=500, seed=0)
    assert res["p"] > 0.05


def test_permute_reproducible():
    lab = _labels()
    brk, mr = _trades("brk", lab, seed=1), _trades("mr", lab, seed=2, sign=-1)
    a = verdict.permute(brk, mr, {"ES": lab}, n=200, seed=7)
    b = verdict.permute(brk, mr, {"ES": lab}, n=200, seed=7)
    assert a == b


def test_test_lock(tmp_path):
    lock = tmp_path / "TEST_RUN.lock"
    verdict.acquire_test_lock(lock)
    assert lock.exists() and "config_sha256" in lock.read_text()
    with pytest.raises(SystemExit):
        verdict.acquire_test_lock(lock)


def test_period_filter():
    tr = pd.DataFrame({"entry_bar": pd.to_datetime(["2026-09-30 23:00", "2026-10-01 08:00"]), "net_bp": [1, 2]})
    assert list(verdict.period(tr, "trainval")["net_bp"]) == [1]
    assert list(verdict.period(tr, "test")["net_bp"]) == [2]
    lab = pd.Series(["high", "low"], index=pd.to_datetime(["2026-09-30 23:00", "2026-10-01 08:00"]))
    assert list(verdict.period_labels(lab, "test")) == ["low"]


def test_permute_keeps_time_of_day():
    # 손익이 시각에만 달려 있고 국면도 시각으로만 정해지면, 날짜만 미는 귀무분포에서는 우연과 구별 안 됨
    lab = _clock_labels()
    brk = _trades("brk", lab, seed=1)
    mr = _trades("mr", lab, seed=2, sign=-1)
    res = verdict.permute(brk, mr, {"ES": lab}, n=300, seed=0)
    assert res["obs"] > 15 and res["p"] > 0.5


def test_permute_common_shift_wider():
    lab = _labels()
    brk = pd.concat([_trades("brk", lab, seed=1, eff=1.0), _trades("brk", lab, seed=1, eff=1.0).assign(sym="NQ")])
    mr = pd.concat([_trades("mr", lab, seed=2, eff=1.0, sign=-1), _trades("mr", lab, seed=2, eff=1.0, sign=-1).assign(sym="NQ")])
    own = verdict.permute(brk, mr, {"ES": lab, "NQ": lab}, n=400, seed=0, common=False)
    com = verdict.permute(brk, mr, {"ES": lab, "NQ": lab}, n=400, seed=0, common=True)
    assert com["null_q95"] - com["null_q05"] > own["null_q95"] - own["null_q05"]
