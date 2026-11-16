"""백테스트 — engine.Stepper 를 봉마다 돌림 (실행기와 같은 판단 코드).

체결: 봉 t 마감 판단 → 봉 t+1 시가. t+1 이 t 바로 다음 봉(간격 ≤ 15분)이고 모의계좌 체결 시간이어야 매매.
매매 못 하는 봉(0~7시, CME 휴장)에는 포지션을 그대로 들고 감. 금요일 23:50 체결로 전부 정리.
비용: 계약당 편도 (수수료 $2 + 반 틱). 손익은 롤 점프를 뺀 가격(bars.clean)으로 계산.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

import config
import engine
import session
import signals


@dataclass
class Result:
    daily: pd.Series        # 날짜별 순손익 $
    trades: pd.DataFrame    # 체결 기록
    gross: float
    cost: float
    contracts: int
    exposure: pd.DataFrame  # 봉별 계약 수

    @property
    def net(self) -> float:
        return self.gross - self.cost

    def summary(self) -> dict:
        d = self.daily
        sd = d.std()
        return {"gross": round(self.gross, 1), "cost": round(self.cost, 1), "net": round(self.net, 1),
                "contracts": int(self.contracts), "days": int(len(d)),
                "sharpe_net": round(float(d.mean() / sd * np.sqrt(252)) if sd > 0 else 0.0, 2),
                "t_net": round(float(d.mean() / sd * np.sqrt(len(d))) if sd > 0 else 0.0, 2),
                "avg_abs_pos": [round(float(x), 2) for x in self.exposure.abs().mean().values]}


def cov_stack(p: signals.Panel) -> np.ndarray:
    Rr = p.R.fillna(0.0)
    n = len(p.mem)
    return Rr.rolling(config.COV_WIN, min_periods=config.VOL_WIN).cov().values.reshape(len(p.idx), n, n)


def run(p: signals.Panel, params: engine.Params, period: tuple[str, str], cov3: np.ndarray | None = None) -> Result:
    cov3 = cov_stack(p) if cov3 is None else cov3
    st = engine.Stepper(p.mem, params)
    mult = np.array([config.CONTRACT[s][0] for s in p.mem], float)
    tick = np.array([config.CONTRACT[s][1] for s in p.mem], float)
    per_side = config.FEE_SIDE + config.HALF_SPREAD_TICKS * tick * mult
    idx = p.idx
    d = idx.strftime("%Y-%m-%d")
    O, C, Craw, sig = p.O.values, p.C.values, p.Craw.values, p.sig.values
    F = {k: v.values for k, v in p.F.items()}
    in_per = (d >= period[0]) & (d <= period[1])
    daily: dict[str, float] = {}
    trades, expo = [], []
    pos = np.zeros(len(p.mem))
    gross = cost = 0.0
    ncon = 0
    for t in range(len(idx) - 1):
        if not in_per[t]:
            continue
        ts, fill = idx[t], idx[t + 1]
        new = pos
        if fill - ts <= config.GAP_MAX and session.can_trade(fill):
            new = st.step({k: F[k][t] for k in params.signals}, sig[t], cov3[t], Craw[t],
                          flat=session.weekend_flat(fill))
        dq = new - pos
        px = O[t + 1]
        step = float(np.sum(pos * mult * (px - C[t])) + np.sum(new * mult * (C[t + 1] - px)))
        c = float(np.sum(np.abs(dq) * per_side))
        gross += step
        cost += c
        daily[d[t]] = daily.get(d[t], 0.0) + step - c
        if np.any(dq != 0):
            ncon += int(np.abs(dq).sum())
            for i, s in enumerate(p.mem):
                if dq[i] != 0:
                    trades.append({"fill_ts": fill, "sym": s, "dq": int(dq[i]), "pos": int(new[i]),
                                   "px": float(px[i]), "cost": float(abs(dq[i]) * per_side[i])})
        expo.append(new.copy())
        pos = new
    ex = pd.DataFrame(expo, columns=p.mem) if expo else pd.DataFrame(columns=p.mem)
    return Result(pd.Series(daily, dtype=float), pd.DataFrame(trades), gross, cost, ncon, ex)
