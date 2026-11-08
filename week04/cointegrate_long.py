"""2단계 재검정 — 6개월(2026-05-01~11-07) 연결선물 5분봉으로 공적분.

연결선물은 월물이 바뀌는 날 재개장 봉에서 가격이 뜁니다 (ES 6/16·9/15 +0.9%, RB 8월 ±8~9% 등).
그래서 「재개장 갭」(앞 봉과 30분 넘게 떨어진 봉의 시가 − 앞 봉 종가)을 빼고 장중 움직임만 이어붙인
로그가격으로 검정합니다. 주말·일일 휴장 갭도 같이 빠지지만 두 레그 모두 같은 시각이라 페어 관계는 남습니다.

  A. 6개월 전체 Engle-Granger p, β, 반감기(시간)
  B. 안정성 — 20거래일 창을 5일씩 밀며 EG p<0.05 인 창 비율 (한때만 맞은 건지, 꾸준한지)
  C. 대회 창(15~18시)의 평균 교차 — 6개월 β 로 만든 스프레드가 3시간 동안 평균(앞 20일)을 몇 번 건너는지

  python3 cointegrate_long.py
출력: results/coint_long.csv
"""
from __future__ import annotations

import itertools
import math
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from statsmodels.tsa.stattools import adfuller, coint

from cointegrate import half_life, ols_beta

warnings.filterwarnings("ignore")
ROOT = Path(__file__).parent
SRC = ROOT / "results" / "data" / "bars_5m_cont"
REOPEN_GAP = pd.Timedelta("30min")
ROLL_DAYS, ROLL_STEP = 20, 5
LAGS = 6  # ADF 시차 고정 (5분봉 6개 = 30분). 기본 autolag 는 50개 넘게 다 맞춰봐서 91쌍에 10시간 걸림


def eg(y: pd.Series, x: pd.Series) -> float:
    return coint(y, x, maxlag=LAGS, autolag=None)[1]


def load_gapless(path: Path) -> pd.Series:
    df = pd.read_csv(path, dtype=str)
    ts = pd.to_datetime(df["ref_dt"] + df["exch_tm"].str.zfill(6), format="%Y%m%d%H%M%S")
    o = np.log(df["open_pric"].astype(float).values)
    c = np.log(df["close_pric"].astype(float).values)
    step = np.empty(len(c))
    step[0] = 0.0
    # 재개장 = 정해진 휴장을 건넌 봉만. 종목마다 「30분 넘게 빈 봉」으로 보면 거래 드문 NKD 의
    # 장중 공백까지 지워져 NKD↔NIY 가 깨졌음 (첫 실행). 휴장: CME 06~08시대(서머타임 따라 07/08시 재개),
    # SGX 06:15~08:30·15:30~15:55, 주말(6시간 넘는 공백).
    gap = ts.diff()
    clock = ts.dt.hour * 60 + ts.dt.minute
    at_open = clock.between(6 * 60 + 55, 8 * 60 + 35) | clock.between(15 * 60 + 50, 16 * 60)
    reopen = ((gap > REOPEN_GAP) & (at_open | (gap >= pd.Timedelta("6h")))).values
    # 이어진 봉: 종가→종가. 재개장 봉: 그 봉 안 움직임(시가→종가)만.
    step[1:] = np.where(reopen[1:], c[1:] - o[1:], c[1:] - c[:-1])
    s = pd.Series(np.cumsum(step), index=ts, name=path.stem)
    return s[~s.index.duplicated(keep="last")]


def main() -> None:
    series = {p.stem: load_gapless(p) for p in sorted(SRC.glob("*.csv"))}
    # 한 다리가 혼자서도 정상(ADF p<0.1)이면 EG 가 β≈0 으로 「공적분」을 내므로 표시해서 거릅니다.
    i1 = {k: adfuller(v.values, maxlag=LAGS, autolag=None)[1] for k, v in series.items()}
    print("단일 ADF p:", " ".join(f"{k}:{v:.2f}" for k, v in i1.items()))
    out = []
    for a, b in itertools.combinations(series, 2):
        j = pd.concat([series[a], series[b]], axis=1, join="inner").dropna()
        if len(j) < 2000:
            continue
        p_ab, p_ba = eg(j[a], j[b]), eg(j[b], j[a])
        y, x = (a, b) if p_ab <= p_ba else (b, a)
        beta = ols_beta(j[y].values, j[x].values)
        resid = j[y].values - beta * j[x].values
        hl_h = half_life(resid) * 5 / 60

        days = sorted(set(j.index.normalize()))
        passed = total = 0
        for k in range(0, len(days) - ROLL_DAYS + 1, ROLL_STEP):
            w = j[(j.index >= days[k]) & (j.index < days[k + ROLL_DAYS - 1] + pd.Timedelta("1D"))]
            if len(w) < 1000:
                continue
            total += 1
            passed += eg(w[y], w[x]) < 0.05

        # 대회 창 교차: 앞 20일 평균을 기준선으로
        spread = pd.Series(resid, index=j.index)
        crosses = []
        for i, d in enumerate(days):
            if i < ROLL_DAYS or d.dayofweek >= 5:
                continue
            ref = spread[(spread.index >= days[i - ROLL_DAYS]) & (spread.index < d)].mean()
            win = spread[d + pd.Timedelta("15h"):d + pd.Timedelta("17h55min")] - ref
            if len(win) >= 30:
                crosses.append(int(np.sum(np.diff(np.sign(win.values)) != 0)))
        out.append({
            "y": y, "x": x, "n": len(j), "p_6m": round(min(p_ab, p_ba), 4), "beta": round(beta, 3),
            "hl_hours": round(hl_h, 1) if math.isfinite(hl_h) else math.inf,
            "roll_pass": f"{passed}/{total}", "roll_ratio": round(passed / total, 2) if total else math.nan,
            "win_cross_med": float(np.median(crosses)) if crosses else math.nan,
            "win_days_crossing": round(float(np.mean([c > 0 for c in crosses])), 2) if crosses else math.nan,
            "both_I1": i1[y] >= 0.1 and i1[x] >= 0.1,
        })
    df = pd.DataFrame(out).sort_values(["both_I1", "p_6m"], ascending=[False, True])
    df.to_csv(ROOT / "results" / "coint_long.csv", index=False)
    pd.set_option("display.width", 200)
    print(df.head(25).to_string(index=False))


if __name__ == "__main__":
    main()
