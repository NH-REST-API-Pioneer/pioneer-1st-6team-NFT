"""2단계 — 움직임/비용이 높은 품목끼리 공적분 페어 찾기.

두 가지로 봅니다.
  A. 전 기간 (10/19~11/07, 모든 시간, 5분봉 로그가격): Engle-Granger 검정 p값, 헤지비율 β, 반감기.
     「길게 보면 같이 가나」.
  B. 대회 창 (평일 15:00~18:00, 1분봉): 날마다 그 전 5거래일 5분봉으로 β 를 추정(그날 데이터 안 씀)하고
     그 β 로 만든 스프레드가 그날 3시간 안에 평균회귀하는지 — ADF p<0.05 인 날 비율, 반감기(분),
     평균 교차 횟수, 스프레드 3시간 표준편차를 달러로 바꿔 왕복 비용과 비교.
     「대회 3시간 안에 실제로 왔다 갔다 하나」. 전략에 직접 쓰는 건 이쪽입니다.

스프레드 s = log A − β·log B. A 1계약에 B 를 β·(A 명목)/(B 명목) 계약 붙이면 달러 중립.
  python3 cointegrate.py
출력: results/coint_pairs_<봉>.csv  (python3 cointegrate.py --bar 5min)
"""
from __future__ import annotations

import itertools
import math
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from statsmodels.tsa.stattools import adfuller, coint

from screen import MICRO, load_specs

warnings.filterwarnings("ignore")
ROOT = Path(__file__).parent
BARS = ROOT / "results" / "data" / "bars"
UNIVERSE = ["NQZ26", "ESZ26", "YMZ26", "RTYZ26", "NKDZ26", "NIYZ26", "SNKZ26", "TWNX26",
            "GCZ26", "SIZ26", "HGZ26", "PLF27", "HOZ26", "RBZ26", "CLZ26"]
WIN_START, WIN_END = "15:00", "17:59"
BETA_DAYS = 5
WIN_BAR = "1min"  # --bar 로 바꿈


def load_close(sym: str) -> pd.Series:
    df = pd.read_csv(BARS / f"{sym}.csv", dtype={"ref_dt": str, "exch_tm": str})
    ts = pd.to_datetime(df["ref_dt"] + df["exch_tm"].str.zfill(6), format="%Y%m%d%H%M%S")
    s = pd.Series(df["close_pric"].astype(float).values, index=ts, name=sym).sort_index()
    s = s[~s.index.duplicated(keep="last")]
    return s[s.index.dayofweek < 5]


def half_life(x: np.ndarray) -> float:
    """Δx = a + b·x(t−1) 의 b 로 반감기 (단위 = 표본 간격)."""
    if len(x) < 10:
        return math.nan
    b = np.polyfit(x[:-1], np.diff(x), 1)[0]
    return -math.log(2) / math.log(1 + b) if -1 < b < 0 else math.inf


def ols_beta(y: np.ndarray, x: np.ndarray) -> float:
    return float(np.polyfit(x, y, 1)[0])


def main() -> None:
    specs = load_specs()
    closes = {s: load_close(s) for s in UNIVERSE}
    logp5 = {s: np.log(c.resample("5min").last().dropna()) for s, c in closes.items()}
    # 1분 격자 (거래 없는 분은 직전 가격), 창 안만
    grid1 = {s: np.log(c.resample(WIN_BAR).last().ffill()) for s, c in closes.items()}
    days = sorted({d for d in closes["ESZ26"].index.normalize()})

    def usd_pt(sym: str) -> float:
        sp = specs[sym[:-3]]
        return sp["tick_val"] / sp["tick"]

    def cost(sym: str) -> float:
        sp = specs[sym[:-3]]
        return 2 * (2.0 if sym[:-3] in MICRO else 8.0) + sp["tick_val"]

    out = []
    for a, b in itertools.combinations(UNIVERSE, 2):
        j5 = pd.concat([logp5[a], logp5[b]], axis=1, join="inner").dropna()
        if len(j5) < 500:
            continue
        # A. 전 기간 — 방향 둘 다 해보고 p 작은 쪽
        p_ab = coint(j5[a], j5[b])[1]
        p_ba = coint(j5[b], j5[a])[1]
        y, x = (a, b) if p_ab <= p_ba else (b, a)
        beta_full = ols_beta(j5[y].values, j5[x].values)
        resid = j5[y].values - beta_full * j5[x].values
        hl_full = half_life(resid) * 5 / 60  # 시간

        # B. 대회 창, 표본 밖 β
        stat_days, hls, cross, sd_usd, costs, used = 0, [], [], [], [], 0
        for i, d in enumerate(days):
            if i < BETA_DAYS:
                continue
            hist = j5[(j5.index >= days[i - BETA_DAYS]) & (j5.index < d)]
            if len(hist) < 200:
                continue
            beta = ols_beta(hist[y].values, hist[x].values)
            w0, w1 = d + pd.Timedelta(WIN_START + ":00"), d + pd.Timedelta(WIN_END + ":00")
            wy, wx = grid1[y][w0:w1], grid1[x][w0:w1]
            win = pd.concat([wy, wx], axis=1, join="inner").dropna()
            if len(win) < 150 * 60 // pd.Timedelta(WIN_BAR).seconds:
                continue
            used += 1
            s = win[y].values - beta * win[x].values
            s = s - s.mean()
            if adfuller(s, maxlag=5, autolag=None)[1] < 0.05:
                stat_days += 1
            hls.append(half_life(s))
            cross.append(int(np.sum(np.diff(np.sign(s)) != 0)))
            px_y, px_x = math.exp(win[y].iloc[-1]), math.exp(win[x].iloc[-1])
            notional_y = px_y * usd_pt(y)
            n_x = beta * notional_y / (px_x * usd_pt(x))  # A 1계약당 B 계약 수
            sd_usd.append(float(np.std(s)) * notional_y)
            costs.append(cost(y) + abs(n_x) * cost(x))
        if not used:
            continue
        hl_med = float(np.median([h for h in hls if math.isfinite(h)])) if any(map(math.isfinite, hls)) else math.inf
        out.append({
            "y": y, "x": x, "p_full": round(min(p_ab, p_ba), 4), "beta_full": round(beta_full, 3),
            "hl_full_h": round(hl_full, 1), "days": used, "stat_day_ratio": round(stat_days / used, 2),
            "hl_win_min": round(hl_med * pd.Timedelta(WIN_BAR).seconds / 60, 1), "cross_per_day": round(float(np.median(cross)), 1),
            "sd_usd": round(float(np.median(sd_usd)), 1), "cost_usd": round(float(np.median(costs)), 1),
            "sd_to_cost": round(float(np.median(sd_usd)) / float(np.median(costs)), 2),
            "x_per_y": round(n_x, 3),
        })
    df = pd.DataFrame(out).sort_values(["stat_day_ratio", "sd_to_cost"], ascending=False)
    df.to_csv(ROOT / "results" / f"coint_pairs_{WIN_BAR}.csv", index=False)
    pd.set_option("display.width", 200)
    print(df.head(30).to_string(index=False))


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--bar", default="1min", help="대회 창 검정 봉 간격 (1min, 5min ...)")
    WIN_BAR = ap.parse_args().bar
    main()
