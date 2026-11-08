"""판단 엔진(rv_engine)이 백테스트(backtest_rv_full.simulate)와 같은 거래를 내는지 — 6개월 5분봉 리플레이.

  python3 -m unittest test_rv_engine -v
"""
from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

import backtest_rv as B
import backtest_rv_full as F
from rv_engine import RVEngine, RVParams, news_blackout


def replay(j: pd.DataFrame, p: RVParams):
    """엔진을 봉마다 돌려 백테스트와 같은 회계로 거래를 만듭니다 (다음 봉 시가 체결, 휴장 전은 그 봉 종가)."""
    idx = j.index
    yc, xc = j[("y", "c")].values, j[("x", "c")].values
    yo, xo = j[("y", "o")].values, j[("x", "o")].values
    brk = np.r_[(np.diff(idx.values) > F.BREAK.to_timedelta64()), True]
    cy, cx = B.cost_bp("GC", 1), B.cost_bp("SI", 1)
    eng = RVEngine(p)
    trades, entry = [], None
    for t in range(p.L - 1, len(j) - 1):
        lo = t - p.L + 1
        info = eng.step(idx[lo:t + 1], yc[lo:t + 1], xc[lo:t + 1], bool(brk[t]))
        if info["act"] == "enter":
            eng.filled_entry(info)
            entry = (t + 1, yo[t + 1], xo[t + 1])
        elif info["act"] == "exit":
            q = eng.filled_exit()
            py, px = (yc[t], xc[t]) if brk[t] else (yo[t + 1], xo[t + 1])
            t0, y0, x0 = entry
            r = q.side * ((py - y0) - q.beta * (px - x0))
            trades.append((idx[t0], (r - (cy + abs(q.beta) * cx) / 1e4) * B.NOTIONAL))
    return trades


class ReplayMatchesBacktest(unittest.TestCase):
    def test_same_trades(self):
        j = F.prepare()
        p = RVParams()
        idx = j.index.to_series()
        z, beta, sd = F.rolling_zs(j[("y", "c")].values, j[("x", "c")].values, p.L)
        ok = (idx.diff(p.L - 1).dt.total_seconds() / 60).values <= (p.L - 1) * 5 + 15
        bt = F.simulate(j, z, beta, ok, p.k_in, p.k_out, p.stop_m, B.cost_bp("GC", 1), B.cost_bp("SI", 1),
                        F.news_blackout(j.index), sd)
        rp = replay(j, p)
        self.assertEqual(len(bt), len(rp))
        for a, b in zip(bt, rp):
            self.assertEqual(a[0], b[0])
            self.assertAlmostEqual(a[1], b[1], places=6)
        # 채택값의 학습·검증 숫자 (backtest_rv_full --news 표와 같아야 함)
        tr = sum(p_ for t, p_ in rp if t < B.TRAIN_END)
        va = sum(p_ for t, p_ in rp if t >= B.TRAIN_END)
        self.assertAlmostEqual(tr, 2915.2, delta=0.5)
        self.assertAlmostEqual(va, 462.5, delta=0.5)


class Blackout(unittest.TestCase):
    def test_dst(self):
        # 여름(EDT): 뉴욕 08:30 = KST 21:30, 겨울(EST): KST 22:30
        self.assertTrue(news_blackout(pd.Timestamp("2026-07-15 21:30")))
        self.assertFalse(news_blackout(pd.Timestamp("2026-07-15 22:30")))
        self.assertTrue(news_blackout(pd.Timestamp("2026-11-10 22:30")))
        self.assertFalse(news_blackout(pd.Timestamp("2026-11-10 23:00")))
        self.assertTrue(news_blackout(pd.Timestamp("2026-11-10 22:15")))


class StopRule(unittest.TestCase):
    def test_stop_uses_entry_beta(self):
        eng = RVEngine(RVParams(L=5, k_in=1.0, stop_m=1.5, news=False))
        ts = pd.date_range("2026-11-10 10:00", periods=5, freq="5min")
        yc = np.log(np.array([100, 100.1, 99.9, 100.0, 103.0]))
        xc = np.log(np.array([50, 50.05, 49.95, 50.0, 50.0]))
        info = eng.step(ts, yc, xc, brk=False)
        self.assertEqual(info["act"], "enter")
        self.assertEqual(info["side"], -1)   # 금이 혼자 뛰어 z>0 → 스프레드 매도
        eng.filled_entry(info)
        # 금이 더 뛰면 진입 β·σ 기준으로 불리 → 손절
        ts2 = pd.date_range("2026-11-10 10:05", periods=5, freq="5min")
        yc2 = np.r_[yc[1:], np.log(110.0)]
        xc2 = np.r_[xc[1:], np.log(50.0)]
        info2 = eng.step(ts2, yc2, xc2, brk=False)
        self.assertEqual(info2["act"], "exit")
        self.assertEqual(info2["reason"], "손절")


if __name__ == "__main__":
    unittest.main()
