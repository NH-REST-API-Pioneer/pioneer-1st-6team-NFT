import numpy as np
import pandas as pd
import pytest

import features
import model


def _frame(n_days=200, seed=0):
    """합성: y = 0.5·log rv48 + 0.3·[hour<8] + 잡음 → 시간대를 쓰는 LGBM 이 지속 예측보다 나아야 함."""
    rng = np.random.default_rng(seed)
    ts = pd.date_range("2026-05-01", periods=n_days * 48, freq="30min")
    n = len(ts)
    rv48 = np.exp(rng.normal(0, 0.5, n))
    d = pd.DataFrame({c: np.exp(rng.normal(0, 0.3, n)) for c in features.FEATURES}, index=ts)
    d["rv48"] = rv48
    d["hour"], d["minute"], d["dow"] = ts.hour, ts.minute, ts.dayofweek
    d["sym_code"] = rng.integers(0, 3, n)
    d["y"] = 0.5 * np.log(rv48) + 0.3 * (ts.hour < 8) + rng.normal(0, 0.2, n)
    d["norm"] = 1.0
    d.index = pd.MultiIndex.from_arrays([np.where(d["sym_code"] == 0, "ES", "GC"), ts], names=["sym", "ts"])
    return d


def test_qlike_zero_when_perfect():
    y = pd.Series([0.1, -0.2, 0.3])
    m = model.metrics(y, y)
    assert m["qlike"] == 0 and m["mse"] == 0


def test_qlike_penalizes_underprediction_more():
    y = pd.Series([0.0])
    assert model.metrics(y, y - 0.5)["qlike"] > model.metrics(y, y + 0.5)["qlike"]


def test_split_no_overlap():
    tr, va, te = model.split(_frame())
    ts = lambda d: d.index.get_level_values("ts")
    assert ts(tr).max() < pd.Timestamp("2026-09-01") <= ts(va).min()
    assert ts(va).max() < pd.Timestamp("2026-10-01") <= ts(te).min()
    assert ts(tr).min() >= pd.Timestamp("2026-05-01")


def test_fit_uses_train_only(monkeypatch):
    seen = {}
    real = model.lgb.train

    def spy(params, train_set, *a, **kw):
        seen["n_train"] = len(train_set.data)
        seen["n_valid"] = [len(v.data) for v in kw.get("valid_sets", [])]
        return real(params, train_set, *a, **kw)

    monkeypatch.setattr(model.lgb, "train", spy)
    d = _frame()
    tr, va, _ = model.split(d)
    model.fit(d)
    assert seen["n_train"] == len(tr) and seen["n_valid"] == [len(va)]


def test_beats_persist_on_synthetic():
    d = _frame()
    b = model.fit(d)
    _, _, te = model.split(d)
    assert model.metrics(te["y"], model.predict(b, te))["mse"] < \
        model.metrics(te["y"], model.baseline_persist(te))["mse"]


def test_har_fits_on_train(tmp_path):
    d = _frame()
    tr, _, te = model.split(d)
    p = model.baseline_har(tr, te)
    assert len(p) == len(te) and np.isfinite(p).all()


def test_model_roundtrip(tmp_path):
    d = _frame()
    b = model.fit(d)
    path = tmp_path / "m.txt"
    b.save_model(str(path))
    b2 = model.load(path)
    _, _, te = model.split(d)
    assert np.allclose(model.predict(b, te), model.predict(b2, te))


def test_persist_floor():
    d = pd.DataFrame({"rv12": [0.0, 1e-9, 1.0]})
    p = model.baseline_persist(d)
    assert np.isclose(p.iloc[0], np.log(model.RV_FLOOR)) and np.isclose(p.iloc[2], 0.0)
