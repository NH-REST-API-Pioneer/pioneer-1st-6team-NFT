"""ws_trader.Engine 판단 시험 — 접속·주문 없이 가짜 시세를 넣어 본다.

  python3 -m unittest test_ws_trader -v
"""
import unittest

from ws_trader import TICK, Engine, LiveParams


def engine(**kw) -> Engine:
    return Engine(LiveParams(lookback_s=30, move_won=0.5, rho_max=0.5, hold_s=60, sl_ticks=3, cooldown_s=10, stale_s=60, **kw))


class EngineTest(unittest.TestCase):
    def warm(self, e: Engine, krx=1340.0, bid=0.7450, ask=0.7451, t0=0.0):
        e.on_quote(bid, ask, t0, "100000")
        e.on_krx(krx, t0, "100000")

    def test_krx_up_mku_lagging_sells_mku(self):
        e = engine()
        self.warm(e)
        e.on_quote(0.7450, 0.7451, 20, "100020")            # MKU 그대로
        a = e.on_krx(1340.8, 31, "100031")                    # 31초 뒤 KRX +0.8원
        self.assertEqual(a["act"], "enter")
        self.assertEqual(a["side"], -1)                       # KRX 상승 = MKU 매도
        self.assertEqual(a["ref"], 0.7450)                    # 매도는 매수호가에 체결
        self.assertLess(a["rho"], 0.5)

    def test_mku_already_followed_no_entry(self):
        e = engine()
        self.warm(e)
        # KRX +0.8원 ≈ MKU 4.4틱 하락에 해당. MKU 가 4틱 이미 내려옴 → 반영률 0.9
        e.on_quote(0.7446, 0.7447, 20, "100020")
        self.assertIsNone(e.on_krx(1340.8, 31, "100031"))

    def test_small_move_or_wide_spread_no_entry(self):
        e = engine()
        self.warm(e)
        self.assertIsNone(e.on_krx(1340.3, 31, "100031"))     # 0.3원 < 기준
        e2 = engine()
        self.warm(e2, bid=0.7450, ask=0.7454)                 # 호가 4틱
        self.assertIsNone(e2.on_krx(1340.8, 31, "100031"))

    def test_outside_entry_hours(self):
        e = engine()
        e.on_quote(0.7450, 0.7451, 0, "152100")
        e.on_krx(1340.0, 0, "152100")
        self.assertIsNone(e.on_krx(1340.8, 31, "152131"))

    def test_stop_loss_and_cooldown_and_daily_limit(self):
        e = engine(max_stops_per_day=1)
        self.warm(e)
        a = e.on_krx(1340.8, 31, "100031")
        e.filled_entry(a["side"], a["ref"], 31, "100031")
        self.assertIsNone(e.on_quote(0.7451, 0.7452, 35, "100035"))   # 매도 포지션, 청산호가 0.7452 → -2틱
        x = e.on_quote(0.7452, 0.7453, 36, "100036")                   # -3틱 → 손절
        self.assertEqual((x["act"], x["reason"], x["side"]), ("exit", "손절", 1))
        r = e.filled_exit(x["ref"], 36, x["reason"])
        self.assertAlmostEqual(r["gross_ticks"], -3.0)
        self.assertAlmostEqual(r["net_ticks"], -3.0 - 1.6)
        # 손절 1회 한도 → 이후 신호 무시
        e.on_krx(1340.0, 50, "100050")
        self.assertIsNone(e.on_krx(1341.0, 90, "100130"))

    def test_time_exit_and_flatten(self):
        e = engine()
        self.warm(e)
        a = e.on_krx(1340.8, 31, "100031")
        e.filled_entry(a["side"], a["ref"], 31, "100031")
        self.assertIsNone(e.on_timer(60, "100100"))
        self.assertEqual(e.on_timer(92, "100132")["reason"], "시간")
        e2 = engine()
        e2.on_quote(0.7450, 0.7451, 0, "153900")
        e2.filled_entry(1, 0.7451, 0, "153900")
        self.assertEqual(e2.on_timer(5, "154000")["reason"], "마감")

    def test_stale_krx_gap_no_fake_shock(self):
        e = engine()
        self.warm(e)
        e.on_quote(0.7450, 0.7451, 100, "100140")
        # KRX 가 100초 끊겼다가 +0.8원 위에서 다시 들어옴 → 끊기기 전 값과 비교하지 않음
        self.assertIsNone(e.on_krx(1340.8, 100, "100140"))
        self.assertIsNone(e.on_krx(1340.8, 120, "100200"))
        # 다시 lookback 만큼 쌓인 뒤의 진짜 충격은 잡음
        e.on_quote(0.7450, 0.7451, 140, "100220")
        self.assertEqual(e.on_krx(1341.6, 140, "100220")["act"], "enter")

    def test_no_double_entry_while_pending(self):
        e = engine()
        self.warm(e)
        self.assertIsNotNone(e.on_krx(1340.8, 31, "100031"))
        self.assertIsNone(e.on_krx(1341.0, 32, "100032"))              # 주문 대기 중


if __name__ == "__main__":
    unittest.main()
