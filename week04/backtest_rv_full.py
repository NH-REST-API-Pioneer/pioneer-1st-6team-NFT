"""금·은 롤링 β 상대가치 — 하루 전체(CME 거래 시간 전부)로 파라미터 다시 정하기.

backtest_rv.py 는 15~18시만 봤습니다. 실제로 돌릴 때는 시간을 묶을 이유가 없어 하루 전체로 넓혔고,
그 창에서 고른 값(L84·2.0·0.5·3.5)을 하루 전체에 그대로 쓰면 학습 −$8,200 이라 처음부터 다시 정합니다.

규칙 (5분봉, 봉 t 종가 기준, 다음 봉 시가 체결)
  직전 L개 봉으로 log GC = a + β·log SI → z = 잔차/표준편차. L개 봉이 휴장·주말을 건너면 그 봉 신호는 버림
  z > k_in 매도 / z < −k_in 매수, z 가 k_out 을 건너면 청산, |z| > k_stop 손절, β 는 진입 때 고정
  --news: 미국 지표 발표 전후(뉴욕 08:15~09:00) 신규 진입 금지, 들고 있으면 그 봉에서 청산
  휴장(다음 봉까지 30분 넘게 빔) 직전 봉에서 무조건 정리 → 포지션을 휴장·주말 너머로 들고 가지 않음
  비용 = MGC·MSI 마이크로 (편도 $2 + 1틱) bp, 손익은 금 명목 $10만 기준 달러

선택 규칙 (검증을 보기 전에 정함): 학습 구간 순손익의 「이웃 평균」(L·k_in 한 칸씩 옆 조합 평균,
k_out·k_stop 은 같은 값) 1등. 3주차 week03-전략-설계.md 와 같은 방식.

  python3 backtest_rv_full.py
  python3 backtest_rv_full.py --cost-x 2
출력: results/rv_full_grid[_cost2x].csv
"""
from __future__ import annotations

import argparse
import itertools
from pathlib import Path

import numpy as np
import pandas as pd

import backtest_rv as B

ROOT = Path(__file__).parent
GRID = {"L": [12, 24, 36, 48, 72, 84], "k_in": [1.5, 2.0, 2.5, 3.0, 3.5, 4.0], "k_out": [0.0, 0.5], "stop_m": [1.0, 1.5, 2.0, 99.0]}  # 손절 = 진입 β·σ 고정 스프레드가 σ×m 불리, 99 = 없음
BREAK = pd.Timedelta("30min")


def prepare():
    y, x = B.load("GC"), B.load("SI")
    j = pd.concat({"y": y, "x": x}, axis=1, sort=True).ffill(limit=2).dropna()
    return j[j.index.dayofweek < 5]


def rolling_zs(yc: np.ndarray, xc: np.ndarray, L: int):
    """B.rolling_z 와 같고 잔차 σ 도 돌려줌 (손절 기준용)."""
    n = len(yc)
    z, beta, sd = np.full(n, np.nan), np.full(n, np.nan), np.full(n, np.nan)
    for t in range(L - 1, n):
        ys, xs = yc[t - L + 1:t + 1], xc[t - L + 1:t + 1]
        xm, ym = xs.mean(), ys.mean()
        vx = ((xs - xm) ** 2).sum()
        if vx <= 0:
            continue
        b = ((xs - xm) * (ys - ym)).sum() / vx
        e = ys - ym - b * (xs - xm)
        sdv = e.std()
        if sdv > 0:
            z[t], beta[t], sd[t] = e[-1] / sdv, b, sdv
    return z, beta, sd


def news_blackout(idx: pd.DatetimeIndex) -> np.ndarray:
    """미국 지표 발표(뉴욕 08:30) 전후 — 뉴욕 현지 08:15~09:00 인 봉. 서머타임은 zoneinfo 가 맞춰 줌.
    KST 로는 여름 21:15~22:00, 겨울 22:15~23:00."""
    ny = idx.tz_localize("Asia/Seoul").tz_convert("America/New_York")
    m = ny.hour * 60 + ny.minute
    return np.asarray((m >= 8 * 60 + 15) & (m < 9 * 60), dtype=bool)


def simulate(j, z, beta, ok, k_in, k_out, stop_m, cy, cx, blackout=None, sd=None, z_stop=False):
    """stop_m: 진입 β 로 고정한 스프레드가 진입 때보다 불리하게 진입 σ × stop_m 움직이면 손절 (99 = 없음).
    z_stop=True 면 옛 방식: 롤링 |z| > k_in + stop_m 이면 손절 (근거 문서 6-1·6-2 재현용)."""
    idx = j.index
    blk = blackout if blackout is not None else np.zeros(len(j), bool)
    yo, xo = j[("y", "o")].values, j[("x", "o")].values
    yc, xc = j[("y", "c")].values, j[("x", "c")].values
    brk = np.r_[(np.diff(idx.values) > BREAK.to_timedelta64()), True]  # 이 봉 다음이 휴장
    trades, pos, e = [], 0, None
    for t in range(len(j) - 1):
        if pos:
            zt = z[t]
            t0, b, y0, x0, s0, sd0 = e
            adverse = -pos * ((yc[t] - b * xc[t]) - s0)  # 양수면 불리
            stopped = (abs(zt) > k_in + stop_m) if z_stop else adverse > stop_m * sd0
            if brk[t] or blk[t] or stopped or np.isnan(zt) or (pos == -1 and zt < k_out) or (pos == 1 and zt > -k_out):
                # 휴장 직전이면 그 봉 종가에 정리 (다음 봉 시가는 휴장 뒤라 못 씀)
                py, px = (yc[t], xc[t]) if brk[t] else (yo[t + 1], xo[t + 1])
                r = pos * ((py - y0) - b * (px - x0))
                trades.append((idx[t0], (r - (cy + abs(b) * cx) / 1e4) * B.NOTIONAL, bool(stopped)))
                pos = 0
            continue
        if brk[t] or blk[t] or not ok[t] or np.isnan(z[t]):
            continue
        if z[t] > k_in:
            pos = -1
        elif z[t] < -k_in:
            pos = 1
        if pos:
            e = (t + 1, beta[t], yo[t + 1], xo[t + 1], yc[t] - beta[t] * xc[t], sd[t])
    return trades


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cost-x", type=float, default=1.0)
    ap.add_argument("--stop-mode", choices=["pos", "z"], default="pos",
                    help="손절 기준: pos=진입 β·σ 고정 스프레드(채택), z=롤링 z (옛 방식, 문서 6-1·6-2)")
    ap.add_argument("--news", action="store_true", help="미국 지표 발표 전후(뉴욕 08:15~09:00) 진입 금지·보유 청산")
    args = ap.parse_args()
    j = prepare()
    cy, cx = B.cost_bp("GC", args.cost_x), B.cost_bp("SI", args.cost_x)
    idx = j.index.to_series()
    blackout = news_blackout(j.index) if args.news else None
    rows = []
    for L in GRID["L"]:
        z, beta, sd = rolling_zs(j[("y", "c")].values, j[("x", "c")].values, L)
        span = (idx.diff(L - 1).dt.total_seconds() / 60).values
        ok = span <= (L - 1) * 5 + 15  # 창 안에 휴장이 없을 때만 (봉 몇 개 빠진 건 허용)
        stops = [1.5, 99.0] if args.stop_mode == "z" else GRID["stop_m"]
        for k_in, k_out, stop_m in itertools.product(GRID["k_in"], GRID["k_out"], stops):
            tr = simulate(j, z, beta, ok, k_in, k_out, stop_m, cy, cx, blackout, sd, args.stop_mode == "z")
            d = pd.DatetimeIndex([t[0] for t in tr])
            p = np.array([t[1] for t in tr])
            m = np.asarray(d < B.TRAIN_END, dtype=bool)
            rows.append({"L": L, "k_in": k_in, "k_out": k_out, "stop_m": stop_m,
                         "tr_n": int(m.sum()), "tr_pnl": round(p[m].sum(), 1),
                         "tr_win": round(float((p[m] > 0).mean()), 2) if m.any() else np.nan,
                         "va_n": int((~m).sum()), "va_pnl": round(p[~m].sum(), 1),
                         "va_win": round(float((p[~m] > 0).mean()), 2) if (~m).any() else np.nan,
                         "stops": int(sum(t[2] for t in tr))})
        print(f"L{L} done", flush=True)
    g = pd.DataFrame(rows)
    Ls, ks = GRID["L"], GRID["k_in"]

    def nb(r):
        li, ki = Ls.index(r.L), ks.index(r.k_in)
        m = g[g.L.isin(Ls[max(li - 1, 0):li + 2]) & g.k_in.isin(ks[max(ki - 1, 0):ki + 2])
              & (g.k_out == r.k_out) & (g.stop_m == r.stop_m)]
        return m.tr_pnl.mean()

    g["tr_nb"] = g.apply(nb, axis=1).round(1)
    suffix = ("" if args.cost_x == 1 else f"_cost{args.cost_x:g}x") + ("_news" if args.news else "") + ("_zstop" if args.stop_mode == "z" else "")
    g.to_csv(ROOT / "results" / f"rv_full_grid{suffix}.csv", index=False)
    pd.set_option("display.width", 200)
    print(f"\n학습 플러스 조합 {(g.tr_pnl > 0).mean():.0%}, 검증 플러스 {(g.va_pnl > 0).mean():.0%} (총 {len(g)}개)")
    print("\n[선택] 학습 이웃 평균 상위 5:")
    print(g.sort_values("tr_nb", ascending=False).head(5).to_string(index=False))
    print("\n[참고] 학습 단일 1등:")
    print(g.sort_values("tr_pnl", ascending=False).head(1).to_string(index=False))


if __name__ == "__main__":
    main()
