"""strategy_orb.py 검사 — 기준 범위 · 돌파 방향 · 진입 막기 · 청산 시각이 뜻대로 도는지.

실행 (week03/ 에서):  python3 -m unittest test_strategy_orb -v
"""
from __future__ import annotations

import unittest
from dataclasses import replace

from common import TICK, Minute, hhmmss_of, in_trading_hours, minutes_of
from strategy_orb import Params, SessionBreakout

BASE = 0.6990   # 범위 아래 끝. 범위 위 끝은 BASE + 10틱 = 0.7000


def px(ticks: int) -> float:
    """BASE 에서 ticks 틱 떨어진 값을 일부러 부동소수 덧셈으로 만듭니다 (반올림 잡음까지 검사)."""
    return BASE + ticks * TICK


def minute(time: str, close: float, high: float | None = None, low: float | None = None,
           date: str = "20261103") -> Minute:
    return Minute(date, time, close, close if high is None else high, close if low is None else low, close, 1)


def build_range(strat: SessionBreakout, open_: str = "090000", width: int = 10, date: str = "20261103") -> None:
    """open_ 부터 15분 동안 BASE ~ BASE+width 틱 사이에서 움직입니다."""
    start = minutes_of(open_)
    for i in range(15):
        c = px(width // 2)
        hi = px(width) if i == 3 else c
        lo = px(0) if i == 7 else c
        assert strat.on_minute(minute(hhmmss_of(start + i), c, hi, lo, date)) == []


class BreakoutTest(unittest.TestCase):
    def setUp(self) -> None:
        self.s = SessionBreakout(Params())
        build_range(self.s)

    def test_buy_above_range(self) -> None:
        [sig] = self.s.on_minute(minute("091500", px(12)))
        self.assertEqual(sig.side, "buy")
        self.assertEqual(sig.window, "서울")
        self.assertEqual((sig.range_low, sig.range_high), (px(0), px(10)))
        self.assertEqual(sig.stop_ticks, 12)        # 종가에서 범위 아래 끝까지
        self.assertEqual(sig.target_ticks, 10)      # 범위 폭 × 1.0
        self.assertEqual(sig.exit_by, "170000")     # 다음 개장 (런던)
        self.assertIn("원화 강세", sig.reason)

    def test_sell_below_range(self) -> None:
        [sig] = self.s.on_minute(minute("091500", px(-1)))
        self.assertEqual(sig.side, "sell")
        self.assertEqual(sig.stop_ticks, 11)
        self.assertIn("원화 약세", sig.reason)

    def test_touching_edge_is_not_breakout(self) -> None:
        [sig] = self.s.on_minute(minute("091500", px(10)))
        self.assertIsNone(sig.side)
        self.assertEqual(sig.reason, "범위 안")

    def test_range_minutes_never_signal(self) -> None:
        s = SessionBreakout(Params())
        self.assertEqual(s.on_minute(minute("090500", px(50))), [])   # 범위 쌓는 중에는 판단 안 함

    def test_one_entry_per_window(self) -> None:
        [sig] = self.s.on_minute(minute("091500", px(12)))
        self.s.on_entry(sig.window)
        self.s.on_exit(-30.0, stopped=False)
        [again] = self.s.on_minute(minute("092000", px(13)))
        self.assertIsNone(again.side)
        self.assertIn("이미 진입함", again.reason)

    def test_blocked_while_in_position(self) -> None:
        self.s.on_entry("런던")           # 다른 개장에서 들고 있는 포지션
        [sig] = self.s.on_minute(minute("091500", px(12)))
        self.assertIsNone(sig.side)
        self.assertIn("보유 중", sig.reason)

    def test_no_signal_after_entry_window(self) -> None:
        self.assertEqual(self.s.on_minute(minute("101500", px(20))), [])   # 09:15 + 60분

    def test_daily_stop_limit(self) -> None:
        self.s.day.stops = 2
        [sig] = self.s.on_minute(minute("091500", px(12)))
        self.assertIsNone(sig.side)
        self.assertIn("손절 2회", sig.reason)


class RangeFilterTest(unittest.TestCase):
    def test_narrow_range_is_noise(self) -> None:
        s = SessionBreakout(Params())
        build_range(s, width=2)
        [sig] = s.on_minute(minute("091500", px(4)))
        self.assertIsNone(sig.side)
        self.assertIn("잡음", sig.reason)

    def test_wide_range_already_moved(self) -> None:
        s = SessionBreakout(Params())
        build_range(s, width=40)
        [sig] = s.on_minute(minute("091500", px(41)))
        self.assertIsNone(sig.side)
        self.assertIn("이미 움직임", sig.reason)

    def test_stop_too_wide(self) -> None:
        s = SessionBreakout(replace(Params(), max_stop_ticks=15))
        build_range(s)
        [sig] = s.on_minute(minute("091500", px(20)))   # 손절까지 20틱
        self.assertIsNone(sig.side)
        self.assertIn("손절 폭", sig.reason)

    def test_buffer_ticks(self) -> None:
        s = SessionBreakout(replace(Params(), buffer_ticks=2))
        build_range(s)
        [sig] = s.on_minute(minute("091500", px(12)))   # 2틱 위 — 3틱이 필요
        self.assertIsNone(sig.side)
        [sig] = s.on_minute(minute("091600", px(13)))
        self.assertEqual(sig.side, "buy")

    def test_no_range_minutes(self) -> None:
        s = SessionBreakout(Params())
        [sig] = s.on_minute(minute("091500", px(12)))   # 09:00~09:14 에 봉이 하나도 없었음
        self.assertIsNone(sig.side)
        self.assertIn("범위 봉 없음", sig.reason)


class ScheduleTest(unittest.TestCase):
    def test_trading_hours(self) -> None:
        self.assertFalse(in_trading_hours("082400"))
        self.assertTrue(in_trading_hours("082500"))
        self.assertTrue(in_trading_hours("203400"))
        self.assertFalse(in_trading_hours("203500"))
        self.assertFalse(in_trading_hours("204900"))
        self.assertTrue(in_trading_hours("205000"))
        self.assertTrue(in_trading_hours("235900"))

    def test_before_first_open_and_break_ignored(self) -> None:
        s = SessionBreakout(Params())
        self.assertEqual(s.on_minute(minute("083000", px(5))), [])
        self.assertEqual(s.on_minute(minute("204000", px(5))), [])

    def test_london_exit_by_new_york(self) -> None:
        s = SessionBreakout(Params())
        build_range(s, "170000")
        [sig] = s.on_minute(minute("171500", px(12)))
        self.assertEqual((sig.window, sig.side, sig.exit_by), ("런던", "buy", "223000"))

    def test_new_york_exit_by_flatten(self) -> None:
        s = SessionBreakout(Params())
        build_range(s, "223000")
        [sig] = s.on_minute(minute("224500", px(12)))
        self.assertEqual((sig.window, sig.side, sig.exit_by), ("뉴욕", "buy", "235000"))

    def test_entry_at_flatten_blocked(self) -> None:
        s = SessionBreakout(replace(Params(), entry_min=70))
        build_range(s, "223000")
        [sig] = s.on_minute(minute("234900", px(12)))   # 진입 시각 23:50 = 청산 시각
        self.assertIsNone(sig.side)
        self.assertIn("매매 시간 밖", sig.reason)

    def test_new_day_resets(self) -> None:
        s = SessionBreakout(Params())
        build_range(s)
        [sig] = s.on_minute(minute("091500", px(12)))
        s.on_entry(sig.window)
        s.on_exit(-30.0, stopped=True)
        build_range(s, date="20261104")
        [sig] = s.on_minute(minute("091500", px(12), date="20261104"))
        self.assertEqual(sig.side, "buy")
        self.assertEqual(s.day.stops, 0)

    def test_windows_must_be_sorted(self) -> None:
        with self.assertRaises(ValueError):
            SessionBreakout(replace(Params(), windows=(("런던", "170000"), ("서울", "090000"))))


if __name__ == "__main__":
    unittest.main()
