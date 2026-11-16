import numpy as np
import pandas as pd

import data
import engine
from helpers import raw_bars

START = "2026-11-18 10:00"   # 수요일 장중


def hist_from(close, start=START, hl=0.0001):
    ts = pd.date_range(start, periods=len(close), freq="5min")
    raw = raw_bars(ts, np.asarray(close, float))
    raw["high_pric"] = raw["close_pric"] * (1 + hl)
    raw["low_pric"] = raw["close_pric"] * (1 - hl)
    return data.clean(raw)


def flat(n=30, p=100.0, seed=0):
    return list(p + np.random.default_rng(seed).normal(0, 0.001, n))


def run(eng, h, regime="high", upto=None):
    """봉을 하나씩 먹여 마지막 결과를 돌려줌."""
    out = None
    for i in range(engine.WARM, (upto or len(h)) + 1):
        out = eng.step(h.iloc[:i], regime)
    return out


def test_breakout_long():
    h = hist_from(flat(30) + [100.5])
    e = engine.Engine("brk")
    r = run(e, h)
    assert r["act"] == "enter" and r["side"] == 1 and r["strat"] == "brk"
    assert e.pos.side == 1 and e.pos.entry_bar == h.index[-1]


def test_breakout_short():
    h = hist_from(flat(30) + [99.5])
    r = run(engine.Engine("brk"), h)
    assert r["act"] == "enter" and r["side"] == -1


def test_breakout_exit_channel():
    h = hist_from(flat(30) + [100.5, 100.6, 100.7, 100.8, 100.9, 101.0, 101.1, 100.0])
    e = engine.Engine("brk")
    acts = [e.step(h.iloc[:i], "high")["act"] for i in range(engine.WARM, len(h) + 1)]
    assert acts.count("enter") == 1 and acts[-1] == "exit"
    assert e.last["reason"] == "channel" and e.pos is None


def test_mr_entry_and_cross():
    up = flat(30)
    h = hist_from(up + [99.7, 99.8, 99.9, 100.0, 100.05])
    e = engine.Engine("mr")
    acts = [(e.step(h.iloc[:i], "low")) for i in range(engine.WARM, len(h) + 1)]
    ent = [a for a in acts if a["act"] == "enter"]
    ex = [a for a in acts if a["act"] == "exit"]
    assert ent and ent[0]["side"] == 1 and ent[0]["strat"] == "mr"
    assert ex and ex[0]["reason"] == "mean"


def test_mr_stop():
    h = hist_from(flat(30) + [99.7, 99.0, 98.0, 96.0])
    e = engine.Engine("mr")
    acts = [e.step(h.iloc[:i], "low") for i in range(engine.WARM, len(h) + 1)]
    assert [a["act"] for a in acts if a["act"]][:2] == ["enter", "exit"]
    assert [a for a in acts if a["act"] == "exit"][0]["reason"] == "stop"


def test_max_hold():
    close = flat(30) + [100.5] + list(100.5 + np.arange(1, 30) * 0.01)   # 천천히 올라 채널 이탈 없음
    h = hist_from(close)
    e = engine.Engine("brk")
    res = [e.step(h.iloc[:i], "high") for i in range(engine.WARM, len(h) + 1)]
    ei = next(i for i, r in enumerate(res) if r["act"] == "enter")
    xi = next(i for i, r in enumerate(res) if r["act"] == "exit")
    assert xi - ei == 24 and res[xi]["reason"] == "time"


def test_flatten_2345():
    close = flat(30) + [100.5] + [100.6] * 3
    h = hist_from(close, start="2026-11-18 21:05")          # 진입 봉 23:35? → 계산: 30봉 뒤 23:35
    e = engine.Engine("brk", )
    res = [e.step(h.iloc[:i], "high") for i in range(engine.WARM, len(h) + 1)]
    # 23:35 봉 마감 신호 → 체결 23:40 은 진입 시간(23:30) 지남 → 진입 없음
    assert all(r["act"] is None for r in res)
    h2 = hist_from(flat(30) + [100.5] + [100.6] * 5, start="2026-11-18 20:55")   # 진입 봉 23:25 → 체결 23:30
    e2 = engine.Engine("brk")
    res2 = {h2.index[i - 1]: e2.step(h2.iloc[:i], "high") for i in range(engine.WARM, len(h2) + 1)}
    assert res2[pd.Timestamp("2026-11-18 23:25")]["act"] == "enter"
    assert res2[pd.Timestamp("2026-11-18 23:45")]["act"] == "exit"
    assert res2[pd.Timestamp("2026-11-18 23:45")]["reason"] == "flat"


def test_no_entry_over_gap():
    ts1 = pd.date_range(START, periods=25, freq="5min")
    ts2 = pd.date_range(ts1[-1] + pd.Timedelta("1h"), periods=6, freq="5min")
    close = flat(30) + [100.5]
    raw = raw_bars(ts1.append(ts2), np.asarray(close))
    h = data.clean(raw)
    assert run(engine.Engine("brk"), h)["act"] is None


def test_switch_regimes():
    h = hist_from(flat(30) + [100.5])
    assert run(engine.Engine("switch"), h, "mid")["act"] is None
    assert run(engine.Engine("switch"), h, "none")["act"] is None
    r = run(engine.Engine("switch"), h, "high")
    assert r["act"] == "enter" and r["strat"] == "brk"
    h2 = hist_from(flat(30) + [99.7])
    r2 = run(engine.Engine("switch"), h2, "low")
    assert r2["act"] == "enter" and r2["strat"] == "mr" and r2["side"] == 1


def test_regime_change_keeps_rules():
    h = hist_from(flat(30) + [100.5, 100.6, 100.7, 100.8, 100.9, 101.0, 101.1, 100.0])
    e = engine.Engine("switch")
    regs = ["high"] * (31 - engine.WARM + 1) + ["low"] * 20
    res = [e.step(h.iloc[:i], regs[i - engine.WARM]) for i in range(engine.WARM, len(h) + 1)]
    assert res[-1]["act"] == "exit" and res[-1]["reason"] == "channel" and res[-1]["strat"] == "brk"


def test_state_roundtrip():
    h = hist_from(flat(30) + [100.5])
    e = engine.Engine("switch")
    run(e, h)
    e2 = engine.Engine("switch")
    e2.load_state(e.state())
    assert e2.pos == e.pos
