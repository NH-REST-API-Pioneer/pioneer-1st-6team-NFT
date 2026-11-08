"""실행기의 두 레그 처리 — 가짜 브로커로 (API 안 부름).

  python3 -m unittest test_rv_runner -v
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

import rv_runner as R
from rv_engine import RVEngine, RVParams


class FakeBroker:
    """leg(key, side, qty) 결과를 미리 정한 순서대로 돌려줌. True=체결, False=실패."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def leg(self, key, side, qty, ref, why):
        ok = self.script.pop(0) if self.script else True
        self.calls.append((key, side, qty, why, ok))
        return ok, (ref if ok else None), "1"


class Rec:
    def __init__(self):
        self.events, self.trades = [], []

    def event(self, kind, **kw):
        self.events.append((kind, kw))

    def trade(self, **kw):
        self.trades.append(kw)


def make_runner(script):
    r = R.Runner.__new__(R.Runner)
    r.args = mock.Mock(orders=True, y_qty=3)
    r.rec = Rec()
    r.bk = FakeBroker(script)
    r.eng = RVEngine(RVParams())
    r.held = {"y": 0, "x": 0}
    r.exit_pending, r.halt, r.stops_today, r.last_bar = None, None, {}, None
    return r


INFO = {"act": "enter", "side": 1, "beta": 0.5, "sd": 0.001, "s0": 1.0, "z": -3.8,
        "bar": pd.Timestamp("2026-11-10 16:00")}


class TwoLegs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.multiple(R, LIVE=Path(self.tmp.name), STATE=Path(self.tmp.name) / "state.json")
        self.patch.start()
        mock.patch("time.sleep").start()

    def tearDown(self):
        mock.patch.stopall()
        self.tmp.cleanup()

    def test_entry_sizes_and_sides(self):
        r = make_runner([True, True])
        r.enter(dict(INFO), py=4060.0, px=52.0)
        # 은 = round(0.5 × 3 × 4060×10 / (52×1000)) = round(1.17) = 1
        self.assertEqual(r.bk.calls[0][:3], ("y", "buy", 3))
        self.assertEqual(r.bk.calls[1][:3], ("x", "sell", 1))
        self.assertEqual(r.held, {"y": 3, "x": -1})
        self.assertIsNotNone(r.eng.pos)

    def test_leg_failure_unwinds_filled_leg(self):
        # 금 체결, 은 실패·재시도 실패 → 금 되돌림
        r = make_runner([True, False, False, True])
        r.enter(dict(INFO), py=4060.0, px=52.0)
        self.assertEqual(r.bk.calls[-1][:3], ("y", "sell", 3))
        self.assertEqual(r.held, {"y": 0, "x": 0})
        self.assertIsNone(r.eng.pos)
        self.assertIn("레그실패", [e[0] for e in r.rec.events])
        self.assertIsNone(r.halt)

    def test_leg_failure_unwind_fails_halts(self):
        r = make_runner([True, False, False, False])
        r.enter(dict(INFO), py=4060.0, px=52.0)
        self.assertEqual(r.held, {"y": 3, "x": 0})
        self.assertIsNotNone(r.halt)

    def test_exit_retries_then_pending_then_done(self):
        r = make_runner([True, True])
        r.enter(dict(INFO), py=4060.0, px=52.0)
        # 청산: 금 OK, 은 3번 실패 → 청산 대기
        r.bk.script = [True, False, False, False]
        r.exit({"reason": "평균 복귀"}, py=4062.0, px=52.0)
        self.assertIsNotNone(r.exit_pending)
        self.assertEqual(r.held, {"y": 0, "x": -1})
        self.assertEqual(r.rec.trades, [])
        # 다음 봉: 은만 다시 → 끝
        r.bk.script = [True]
        r.finish_exit(4062.0, 52.0, retry_only=True)
        self.assertIsNone(r.exit_pending)
        self.assertEqual(r.held, {"y": 0, "x": 0})
        t = r.rec.trades[0]
        # 금 매수 3 × (4062−4060) × 10 = +60, 은 매도 1 × 0 = 0, 수수료 2×2×4 = 16
        self.assertAlmostEqual(t["pnl_usd"], 60.0)
        self.assertEqual(t["fee_usd"], 16.0)

    def test_zero_silver_qty_skips(self):
        r = make_runner([])
        info = dict(INFO, beta=0.05)
        r.enter(info, py=4060.0, px=52.0)
        self.assertEqual(r.bk.calls, [])
        self.assertIsNone(r.eng.pos)


class Schedule(unittest.TestCase):
    def test_pre_break(self):
        # 겨울(EST): 뉴욕 17:00 = KST 07:00. 06:50 봉이 마지막 판단 봉(06:55 에 정리)
        self.assertTrue(R.pre_break(pd.Timestamp("2026-11-10 06:50")))
        self.assertFalse(R.pre_break(pd.Timestamp("2026-11-10 06:45")))

    def test_market_open(self):
        self.assertFalse(R.market_open(pd.Timestamp("2026-11-10 07:30")))   # 일일 휴장
        self.assertTrue(R.market_open(pd.Timestamp("2026-11-10 08:05")))
        self.assertFalse(R.market_open(pd.Timestamp("2026-11-08 22:00")))   # 일요일 KST = 일요일 아침 뉴욕
        self.assertTrue(R.market_open(pd.Timestamp("2026-11-09 08:05")))    # 월 08:05 KST = 일 18:05 뉴욕


if __name__ == "__main__":
    unittest.main()
