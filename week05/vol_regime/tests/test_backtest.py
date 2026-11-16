import numpy as np
import pandas as pd

import backtest
import config
import engine
from test_engine import flat, hist_from


def _bars():
    rng = np.random.default_rng(5)
    close = 6000 * np.exp(np.cumsum(rng.normal(0, 0.0015, 600)))
    return hist_from(close, start="2026-11-16 08:00", hl=0.0008)


def test_fill_next_open():
    b = _bars()
    tr = backtest.run(b, pd.Series("none", index=b.index), "brk", "ES")
    assert len(tr) > 3
    for _, x in tr.iterrows():
        i = b.index.get_loc(x["entry_bar"])
        assert x["entry_fill_ts"] == b.index[i + 1] and x["entry_px"] == b["o"].iloc[i + 1]
        j = b.index.get_loc(x["exit_bar"])
        if x["reason"] == "end":          # 데이터 끝에서 정리: 마지막 봉 종가
            assert j == len(b) - 1 and x["exit_px"] == b["c"].iloc[-1]
        else:
            assert x["exit_px"] == b["o"].iloc[j + 1]


def test_cost_formula():
    want = 0.25 / 6000 * 1e4 + 2 * config.FEE_SIDE / (6000 * 50 / 10) * 1e4
    assert np.isclose(backtest.cost_bp("ES", 6000.0), want)


def test_net_is_gross_minus_cost():
    b = _bars()
    tr = backtest.run(b, pd.Series("none", index=b.index), "mr", "ES")
    g = tr["side"] * (tr["exit_px"] / tr["entry_px"] - 1) * 1e4
    assert np.allclose(tr["gross_bp"], g) and np.allclose(tr["net_bp"], tr["gross_bp"] - tr["cost_bp"])


def test_matches_engine():
    b = _bars()
    lab = pd.Series(np.where(np.arange(len(b)) % 50 < 25, "high", "low"), index=b.index)
    tr = backtest.run(b, lab, "switch", "ES")
    e = engine.Engine("switch")
    ents = []
    for i in range(engine.WARM, len(b)):     # 마지막 봉은 다음 봉 시가가 없어 백테스트도 안 봄
        r = e.step(b.iloc[:i], lab.iloc[i - 1])
        if r["act"] == "enter":
            ents.append((r["bar"], r["side"], r["strat"]))
    got = list(zip(tr["entry_bar"], tr["side"], tr["strat"]))
    assert got == ents[:len(got)] and len(ents) - len(got) <= 1


def test_regime_tag_at_entry():
    b = _bars()
    tr = backtest.run(b, pd.Series("none", index=b.index), "brk", "ES")
    lab = pd.Series(np.where(np.arange(len(b)) % 2 == 0, "high", "low"), index=b.index)
    tagged = backtest.tag(tr, lab)
    assert (tagged["regime"] == lab.loc[tr["entry_bar"]].values).all()


def test_cost_6j_quoted_units():
    # 6J 는 100엔당 달러(×100)로 표시됨 (중앙값 ≈ 0.63) → 틱 0.00005, 포인트가치 125,000 (week04/screen.py 66줄)
    want = 0.00005 / 0.63 * 1e4 + 2 * config.FEE_SIDE / (0.63 * 125000 / 10) * 1e4
    assert np.isclose(backtest.cost_bp("6J", 0.63), want)
    assert 5 < backtest.cost_bp("6J", 0.63) < 7
