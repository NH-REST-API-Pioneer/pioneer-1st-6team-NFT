"""strategy_bb.py (MKU 로 옮긴 후보 A) 검사 — 바꾼 부분(시간 · 호가 크기)만 봅니다.

지표 식(볼린저 · RSI)은 2주차와 한 글자도 다르지 않아 week02/test_strategy_bb.py 가 검사합니다.
실행 (week03/ 에서):  python3 -m unittest test_strategy_bb -v
"""
from __future__ import annotations

import unittest
from dataclasses import replace

from common import Minute, hhmmss_of, minutes_of
from strategy_bb import BBRSIReversion, Params


def feed(p: Params, path: list[float], start: str, date: str = "20261103") -> list:
    """start 부터 1분봉 하나에 종가 하나씩 넣고 (시=고=저=종), 나온 Signal 을 모읍니다."""
    strat = BBRSIReversion(p)
    out = []
    for i, c in enumerate(path):
        out += strat.on_minute(Minute(date, hhmmss_of(minutes_of(start) + i), c, c, c, c, 1))
    return out


def mku_path() -> list[float]:
    """2주차 검사 경로를 MKU 크기로 줄인 것 (0.1원 → 0.0001). 잔잔히 흔들린 뒤 연속 하락, 되돌림."""
    flat = [0.7000 + (0.0001 if i % 2 else -0.0001) for i in range(60)]
    drop = [0.7000 - 0.0002 * k for k in range(1, 11)]
    back = [0.6980 + 0.0002 * k for k in range(1, 4)]
    return flat + drop + back


class MKUScheduleTest(unittest.TestCase):
    def test_evening_buy_signal(self) -> None:
        """국내 장이 끝난 18:00 이후에도 판단하고, MKU 틱 크기에서 신호가 납니다."""
        p = replace(Params(), bar_min=1, confirm=False, rsi_buy=30)
        buys = [s for s in feed(p, mku_path(), "180000") if s.side == "buy"]
        self.assertTrue(buys)
        self.assertIn("하단 이탈", buys[0].reason)
        self.assertIn("원화 강세", buys[0].reason)
        self.assertGreater(buys[0].stop_ticks, 0)

    def test_break_minutes_skipped(self) -> None:
        p = replace(Params(), bar_min=5)
        sigs = feed(p, [0.7000] * 80, "200000")    # 20:00 ~ 21:19
        starts = [s.bar.start for s in sigs]
        self.assertIn("203000", starts)             # 20:30~20:34 봉까지는 1차 세션
        self.assertIn("205000", starts)              # 휴장 뒤 첫 봉은 20:50 에 시작
        self.assertFalse([t for t in starts if "203500" <= t < "205000"])

    def test_entry_blocked_at_flatten(self) -> None:
        """하락 구간이 23:43~23:52 에 오도록 22:43 에 시작 — 진입 시각이 23:50 이후인 신호는 막혀야 함."""
        p = replace(Params(), bar_min=1, confirm=False, rsi_buy=30, entry_end="240000")
        sigs = feed(p, mku_path(), "224300")
        late = [s for s in sigs if hhmmss_of(minutes_of(s.bar.end) + 1) >= p.flatten_at]
        self.assertTrue([s for s in late if "진입 시간 밖" in s.reason])
        self.assertFalse([s for s in late if s.side])


if __name__ == "__main__":
    unittest.main()
