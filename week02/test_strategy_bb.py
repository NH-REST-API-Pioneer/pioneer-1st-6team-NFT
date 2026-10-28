"""strategy_bb.py 검사 — 지표 식이 NH 앱(indicators.py)과 같은지, 신호 규칙이 뜻대로 도는지.

실행 (week02/ 에서):  python3 -m unittest test_strategy_bb -v
"""
from __future__ import annotations

import random
import statistics
import unittest
from dataclasses import replace

from strategy import Minute
from strategy_bb import BBRSIReversion, Params, RollingBollinger, WilderRSI


def app_rsi(closes: list[float], period: int) -> list[float | None]:
    """NH 앱 backend/strategy/indicators.py 의 rsi() 를 그대로 옮긴 것 (비교 기준)."""
    made: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return made
    gains = losses = 0.0
    for at in range(1, period + 1):
        change = closes[at] - closes[at - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    gains /= period
    losses /= period
    of = lambda g, l: 100.0 if l == 0 else 100.0 - 100.0 / (1.0 + g / l)  # noqa: E731
    made[period] = of(gains, losses)
    for at in range(period + 1, len(closes)):
        change = closes[at] - closes[at - 1]
        gains = (gains * (period - 1) + max(change, 0.0)) / period
        losses = (losses * (period - 1) + max(-change, 0.0)) / period
        made[at] = of(gains, losses)
    return made


class IndicatorTest(unittest.TestCase):
    def setUp(self) -> None:
        rng = random.Random(7)
        price = 1320.0
        self.closes = []
        for _ in range(200):
            price += rng.choice([-0.3, -0.2, -0.1, 0.0, 0.1, 0.2, 0.3])
            self.closes.append(round(price, 1))

    def test_rsi_matches_app_formula(self) -> None:
        r = WilderRSI(14)
        mine = [r.update(c) for c in self.closes]
        ref = app_rsi(self.closes, 14)
        self.assertEqual([m is None for m in mine], [x is None for x in ref])
        for a, b in zip(mine, ref):
            if a is not None:
                self.assertAlmostEqual(a, b, places=9)

    def test_rsi_all_up_is_100(self) -> None:
        r = WilderRSI(3)
        vals = [r.update(c) for c in [1, 2, 3, 4, 5]]
        self.assertEqual(vals[3], 100.0)

    def test_bollinger_uses_population_std(self) -> None:
        bb = RollingBollinger(20, 2.0)
        out = [bb.update(c) for c in self.closes]
        self.assertEqual(out[18], (None, None, None))
        for at in range(19, len(self.closes)):
            window = self.closes[at - 19:at + 1]
            mid = statistics.fmean(window)
            sd = statistics.pstdev(window)
            u, m, l = out[at]
            self.assertAlmostEqual(m, mid, places=9)
            self.assertAlmostEqual(u, mid + 2 * sd, places=9)
            self.assertAlmostEqual(l, mid - 2 * sd, places=9)


def feed(p: Params, path: list[float], date: str = "20261001") -> list:
    """09:00 부터 1분봉 하나에 종가 하나씩 넣고 (시=고=저=종), 나온 Signal 을 모읍니다."""
    strat = BBRSIReversion(p)
    out = []
    for i, c in enumerate(path):
        hh, mm = 9 + i // 60, i % 60
        out += strat.on_minute(Minute(date, f"{hh:02d}{mm:02d}00", c, c, c, c, 1))
    return out


class SignalTest(unittest.TestCase):
    """1분봉 판정, 준비 봉 (= 기간×2 = 40봉) 뒤 급락 → RSI 가 낮고 하단 밖 → 매수.

    잔잔한 ±0.1 흔들림 뒤 0.2 씩 연속 하락하면 Wilder RSI(14) 는 7봉째 30.2, 9봉째 26 쯤입니다.
    """

    def _path(self) -> list[float]:
        flat = [1320.0 + (0.1 if i % 2 else -0.1) for i in range(60)]     # 60분 잔잔히 흔들림
        drop = [1320.0 - 0.2 * k for k in range(1, 11)]                   # 10분 연속 하락 (하단 밖, RSI < 30)
        back = [1318.0 + 0.2 * k for k in range(1, 4)]                    # 되돌림 (밴드 안 복귀)
        return flat + drop + back

    def test_immediate_entry_buy_with_rsi_filter(self) -> None:
        p = replace(Params(), bar_min=1, confirm=False, rsi_buy=30, entry_start="090000")
        sides = [(s.bar.end, s.side, s.reason) for s in feed(p, self._path()) if s.side]
        self.assertTrue(sides, "매수 신호가 나야 함")
        self.assertEqual(sides[0][1], "buy")
        self.assertIn("하단 이탈", sides[0][2])

    def test_rsi_filter_blocks_when_too_strict(self) -> None:
        p = replace(Params(), bar_min=1, confirm=False, rsi_buy=1, entry_start="090000")
        sigs = feed(p, self._path())
        self.assertFalse([s for s in sigs if s.side])
        self.assertTrue([s for s in sigs if "RSI" in s.reason and "걸러" in s.reason])

    def test_rsi_off_when_100(self) -> None:
        p = replace(Params(), bar_min=1, confirm=False, rsi_buy=100, entry_start="090000")
        sides = [s.side for s in feed(p, self._path()) if s.side]
        self.assertIn("buy", sides)

    def test_confirm_waits_for_reentry(self) -> None:
        p = replace(Params(), bar_min=1, confirm=True, rsi_buy=30, entry_start="090000")
        sigs = feed(p, self._path())
        buys = [s for s in sigs if s.side == "buy"]
        self.assertTrue(buys)
        self.assertIn("복귀", buys[0].reason)
        bar = buys[0].bar
        self.assertGreaterEqual(bar.close, bar.lower)   # 복귀한 봉은 밴드 안

    def test_target_is_last_closed_middle(self) -> None:
        p = replace(Params(), bar_min=1, confirm=False, entry_start="090000")
        strat = BBRSIReversion(p)
        last_mid = None
        for i, c in enumerate(self._path()):
            for s in strat.on_minute(Minute("20261001", f"09{i:02d}00" if i < 60 else f"10{i-60:02d}00", c, c, c, c, 1)):
                if s.bar.middle is not None:
                    last_mid = s.bar.middle
            self.assertEqual(strat.target, last_mid)


if __name__ == "__main__":
    unittest.main()
