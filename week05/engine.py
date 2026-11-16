"""돌파·평균회귀 판단 엔진 — 종목 하나의 상태 기계. 백테스트와 실행기가 같은 코드를 씁니다.

봉 t 가 끝났을 때 step() 을 부르고, 결과 주문은 t+5분 봉 시가에 체결된다고 봅니다.
  돌파(brk)   진입: c_t > 직전 12봉 최고가 → 매수, c_t < 직전 12봉 최저가 → 매도
              청산: 매수면 c_t < 직전 6봉 최저가, 매도면 c_t > 직전 6봉 최고가 ("channel")
  평균회귀(mr) z = (c_t − 24봉 평균) / 24봉 표준편차 (t 포함)
              진입: z < −2 → 매수, z > 2 → 매도
              청산: z 가 0 을 건넘 ("mean"), 보유 방향으로 |z| > 3.5 ("stop")
  공통 청산:  강제 정리 시각·휴장 직전 ("flat"), 24봉 보유 ("time")
  진입 조건:  체결 시각이 진입 가능(session.entry_ok), 창(전략 길이 + 현재 봉) 안에 gap·roll 없음,
              이 봉에서 청산하지 않았음.
  mode:       "brk"/"mr" 는 국면과 상관없이 그 전략만 (판정용), "switch" 는 high→brk, low→mr, 그 외 진입 안 함.
  보유 중 국면이 바뀌어도 진입한 전략의 청산 규칙을 따릅니다.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

import config
import session

WARM = max(config.BRK_N, config.MR_N) + 1   # step 에 필요한 최소 봉 수
_BAR = pd.Timedelta("5min")


@dataclass
class Position:
    side: int
    strat: str
    entry_bar: pd.Timestamp
    regime: str
    bars_held: int = 0


class Engine:
    def __init__(self, mode: str, mock_hours: bool = True):
        if mode not in ("brk", "mr", "switch"):
            raise ValueError(mode)
        self.mode, self.mock_hours = mode, mock_hours
        self.pos: Position | None = None
        self.last: dict | None = None

    # -- 실행기 재시작용
    def state(self) -> dict | None:
        if self.pos is None:
            return None
        d = asdict(self.pos)
        d["entry_bar"] = str(self.pos.entry_bar)
        return d

    def load_state(self, d: dict | None) -> None:
        self.pos = None if d is None else Position(d["side"], d["strat"], pd.Timestamp(d["entry_bar"]),
                                                   d["regime"], d["bars_held"])

    # -- 한 봉
    def step(self, hist: pd.DataFrame, regime: str) -> dict:
        """hist: data.clean 형식, 마지막 행 = 방금 끝난 봉."""
        tail = hist.iloc[-WARM:]
        t = tail.index[-1]
        flat = session.must_flatten(t, self.mock_hours)
        can_enter = session.entry_ok(t + _BAR, self.mock_hours)
        bad = (tail["gap"] | tail["roll"]).to_numpy()
        return self.step_arr(t, tail["h"].to_numpy(), tail["l"].to_numpy(), tail["c"].to_numpy(), bad,
                             regime, flat, can_enter)

    def step_arr(self, t, h: np.ndarray, l: np.ndarray, c: np.ndarray, bad: np.ndarray, regime: str,
                 flat: bool, can_enter: bool) -> dict:
        """배열판 (백테스트가 빠르게 부름). 배열 끝 = 봉 t, 길이 ≥ WARM."""
        res = {"act": None, "side": 0, "strat": "", "reason": "", "regime": regime, "bar": t}
        if self.pos is not None:
            p = self.pos
            p.bars_held += 1
            why = self._exit_reason(p, h, l, c, flat)
            if why:
                res.update(act="exit", side=p.side, strat=p.strat, reason=why, regime=p.regime)
                self.pos = None
            self.last = res
            return res
        if flat or not can_enter:
            self.last = res
            return res
        strat = {"brk": "brk", "mr": "mr"}.get(self.mode) or {"high": "brk", "low": "mr"}.get(regime)
        if strat:
            side = self._entry(strat, h, l, c, bad)
            if side:
                self.pos = Position(side, strat, t, regime)
                res.update(act="enter", side=side, strat=strat, reason="signal")
        self.last = res
        return res

    @staticmethod
    def _z(c: np.ndarray) -> float:
        w = c[-config.MR_N:]
        sd = w.std()
        return np.nan if sd <= 0 else (w[-1] - w.mean()) / sd

    def _entry(self, strat: str, h, l, c, bad) -> int:
        n = config.BRK_N if strat == "brk" else config.MR_N
        if bad[-(n + 1):].any():
            return 0
        if strat == "brk":
            if c[-1] > h[-n - 1:-1].max():
                return 1
            if c[-1] < l[-n - 1:-1].min():
                return -1
            return 0
        z = self._z(c)
        if np.isnan(z):
            return 0
        return 1 if z < -config.MR_IN else (-1 if z > config.MR_IN else 0)

    def _exit_reason(self, p: Position, h, l, c, flat: bool) -> str:
        if flat:
            return "flat"
        if p.strat == "brk":
            k = config.BRK_EXIT
            if (p.side == 1 and c[-1] < l[-k - 1:-1].min()) or (p.side == -1 and c[-1] > h[-k - 1:-1].max()):
                return "channel"
        else:
            z = self._z(c)
            if not np.isnan(z):
                if p.side * z < -config.MR_STOP:
                    return "stop"
                if p.side * z >= 0:
                    return "mean"
        if p.bars_held >= config.MAX_HOLD:
            return "time"
        return ""
