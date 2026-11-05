"""runner.py(후보 B 종이 실행기) 시험 — 서버 접속 없이.

  python3 -m unittest test_runner -v
"""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import backtest
import runner
from common import Minute


def quiet_runner(d: str) -> runner.Runner:
    j = runner.Journal(Path(d) / "journal.jsonl")
    return runner.Runner(runner.PaperBroker(j), j)


class ReplayMatchesBacktestTest(unittest.TestCase):
    def test_every_trade_same_as_backtest(self):
        # 2주차처럼: 받아 둔 1분봉으로 다시 돌린 결과가 백테스트(min1-mku-v1)와 거래 하나하나까지 같아야 함
        ms = backtest.load_minutes()
        bt = backtest.run(ms, "orb", runner.SELECTED)["trades"]
        rp = []
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()):
            for day in sorted({m.date for m in ms}):
                rp += runner.replay(day, Path(d) / day)["trades"]
        key = lambda t: (t["entry_date"], t["entry_time"], t["side"], t["entry"], t["stop"], t["target"],  # noqa: E731
                         t["exit_time"], t["exit"], t["reason"], round(t["pnl"], 2))
        self.assertEqual([key(t) for t in rp], [key(t) for t in bt])
        self.assertGreater(len(rp), 10)


class LivePriceTest(unittest.TestCase):
    """실제 실행처럼 1초 현재가만 넣을 때 (ref_low/high 없음)."""

    def day(self, d, lo=0.7440, hi=0.7450, date="20261105"):
        r = quiet_runner(d)
        r.live_date = date
        for i in range(10):     # 09:00~09:09 범위 lo~hi (10틱)
            r.on_minute_closed(Minute(date, f"09{i:02d}00", lo, hi, lo, hi if i % 2 else lo, 1))
        r.on_minute_closed(Minute(date, "091000", 0.7450, 0.7452, 0.7449, 0.7452, 1))   # 고가 + 2틱 → 매수 신호
        return r

    def test_enter_next_price_plus_slip_and_target(self):
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()):
            r = self.day(d)
            r.on_price("091103", 0.7452)
            self.assertEqual((r.pos["entry"], r.pos["stop"], r.pos["deadline"]), (0.7453, 0.7440, "101100"))
            self.assertAlmostEqual(r.pos["target"], 0.7473)              # 범위 10틱 × 2.0
            r.on_price("092000", 0.7473)
            t = r.trades[-1]
            self.assertEqual((t["reason"], t["exit"], t["ticks"]), ("익절", 0.7472, 19))   # 파는 쪽 −1틱
            self.assertAlmostEqual(t["pnl"], 19 * 2.5 - 4)

    def test_stop_and_daily_limit_and_one_entry_per_open(self):
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()):
            r = self.day(d)
            r.on_price("091103", 0.7452)
            r.on_price("091500", 0.7439)
            self.assertEqual(r.trades[-1]["reason"], "손절")
            self.assertEqual(r.trades[-1]["exit"], 0.7438)               # min(현재가, 손절가) − 1틱
            r.on_minute_closed(Minute("20261105", "092000", 0.746, 0.746, 0.746, 0.746, 1))
            r.on_price("092103", 0.746)
            self.assertIsNone(r.pos)                                      # 서울 개장은 한 번만

    def test_time_exit(self):
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()):
            r = self.day(d)
            r.on_price("091103", 0.7452)
            r.on_price("101059", 0.7455)
            self.assertIsNotNone(r.pos)
            r.on_price("101100", 0.7455)
            self.assertEqual((r.trades[-1]["reason"], r.trades[-1]["exit"]), ("시간 청산", 0.7454))

    def test_warmup_signal_not_entered(self):
        # 늦게 켰을 때 준비 봉에서 난 신호로는 진입하지 않음 (2주차와 같음)
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()):
            r = quiet_runner(d)
            r.warmup([Minute("20261105", f"09{i:02d}00", 0.744, 0.745, 0.744, 0.745, 1) for i in range(10)]
                     + [Minute("20261105", "091000", 0.745, 0.7452, 0.745, 0.7452, 1)])
            r.live_date = "20261105"
            r.on_price("091203", 0.7452)
            self.assertIsNone(r.pos)


class StartRetryTest(unittest.TestCase):
    def test_first_chart_error_is_retried_not_fatal(self):
        # 11/05 08:50 처음 차트(분) 조회가 ETR029로 한 번 실패해 실행기가 바로 죽었음 → 다시 시도해야 함
        import nh_client

        class FakeClient:
            env = "demo"
            calls = 0

            def __init__(self, env): pass
            def token(self): pass

            def chart_minute(self, *a, **k):
                FakeClient.calls += 1
                if FakeClient.calls == 1:
                    raise nh_client.NHError("/chart-minute", 400, "ETR029")
                return {"output": []}

            def price(self, *a, **k): return {"output": []}

        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(nh_client, "NHClient", FakeClient), mock.patch.object(runner.time, "sleep", side_effect=[None, KeyboardInterrupt]):   # 재시도 대기 뒤 첫 루프에서 끝냄
            res = runner.live("MKUX26", Path(d), until="235959")
            log = (Path(d) / "journal.jsonl").read_text(encoding="utf-8")
        self.assertEqual(FakeClient.calls, 2)
        self.assertIn("시작 분봉 받기 실패", log)
        self.assertEqual(res["trades"], [])


if __name__ == "__main__":
    unittest.main()
