"""3단계 — 롤링 β 단기 상대가치 백테스트 (대회 창 KST 15:00~18:00).

엄격한 공적분은 6개월 검정에서 NKD↔NIY 말고는 안 나왔습니다 (cointegrate_long.py). 그래서 β·평균을
고정하지 않고 매 봉 직전 L개 봉으로 다시 맞춥니다 (교재 Cartea 외 11.2 「ad hoc bands」 의 롤링판).

신호 (5분봉, 봉 t 종가 기준)
  직전 L개 봉(t 포함)으로 log y = a + β·log x 최소제곱 → 잔차 e, z = e_t / std(e)
  z > k_in  : 스프레드 매도 (y 매도, x 를 β 만큼 매수)    z < −k_in : 반대
  |z| < k_out 이면 청산, |z| > k_stop 이면 손절, 17:30 이후 신규 진입 금지, 17:55 봉에서 강제 청산
  β 는 진입 때 값으로 고정. 체결 = 다음 봉 시가 (신호 봉 종가로 체결하는 미래 참조 방지)
손익: y 명목 $1 당 수익률 (r_y − β·r_x), 비용 = 각 다리 왕복 비용(bp) × 명목. 결과는 y 명목 $10만 기준 달러.
비용: 마이크로 있으면 마이크로 (편도 $2 + 1틱), 없으면 일반 ($8 + 1틱). 11/07 가격으로 bp 환산.

학습 2026-05-01~09-30, 검증 10-01~11-07. 학습에서 페어마다 최고 조합 1개를 고르고 검증에 그대로 적용.
과적합을 보려고 「모든 조합 평균」도 같이 냅니다.

  python3 backtest_rv.py              # 기본 비용
  python3 backtest_rv.py --cost-x 2   # 비용 2배 스트레스
출력: results/rv_grid.csv (모든 조합), results/rv_summary.csv
"""
from __future__ import annotations

import argparse
import itertools
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
SRC = ROOT / "results" / "data" / "bars_5m_cont"

# 왕복 비용(bp) = (편도 수수료×2 + 1틱 $) / 1계약 명목 (11/07 종가)
#            가격       승수     수수료  틱값$   거래 계약
SPEC = {
    "ES": (7457.50, 5, 2, 1.25, "MES"), "NQ": (27358.5, 2, 2, 0.50, "MNQ"),
    "YM": (51535, 0.5, 2, 0.50, "MYM"), "RTY": (2745.5, 5, 2, 0.50, "M2K"),
    "GC": (4059.3, 10, 2, 1.00, "MGC"), "SI": (51.94, 1000, 2, 5.00, "MSI"),
    "PL": (1530.9, 50, 8, 5.00, "PL"), "CL": (90.72, 100, 2, 1.00, "MCL"),
    "HO": (4.2953, 42000, 8, 4.20, "HO"), "RB": (2.7556, 42000, 8, 4.20, "RB"),
    "NKD": (58750, 5, 8, 5.00, "NKD"), "NIY": (58665, 500 / 150, 8, 500 / 150, "NIY"),
}
PAIRS = [("ES", "NQ"), ("ES", "YM"), ("NQ", "YM"), ("ES", "RTY"), ("GC", "SI"), ("GC", "PL"), ("SI", "PL"),
         ("CL", "HO"), ("CL", "RB"), ("HO", "RB"), ("NKD", "NIY")]
GRID = {"L": [12, 24, 36, 72, 84], "k_in": [1.5, 2.0, 2.5], "k_out": [0.0, 0.5], "k_stop": [3.5, 99.0]}
TRAIN_END = pd.Timestamp("2026-10-01")
NOTIONAL = 100_000
LAST_ENTRY, FLAT_AT = "17:30", "17:55"
DAY_START = "07:00"  # 롤링 창 채우기 시작 (CME 재개장). 봉 L 개가 15시 전에 있어야 그날을 씀


def cost_bp(sym: str, mult: float) -> float:
    px, m, fee, tick, _ = SPEC[sym]
    return (2 * fee + tick) / (px * m) * 1e4 * mult


def load(sym: str) -> pd.DataFrame:
    df = pd.read_csv(SRC / f"{sym}.csv", dtype=str)
    ts = pd.to_datetime(df["ref_dt"] + df["exch_tm"].str.zfill(6), format="%Y%m%d%H%M%S")
    out = pd.DataFrame({"o": np.log(df["open_pric"].astype(float).values),
                        "c": np.log(df["close_pric"].astype(float).values)}, index=ts)
    return out[~out.index.duplicated(keep="last")]


def day_frames(y: pd.DataFrame, x: pd.DataFrame, max_l: int):
    """하루씩: DAY_START 부터(롤링 창 채우기용) 18:00 까지 두 다리가 다 있는 5분 격자."""
    j = pd.concat({"y": y, "x": x}, axis=1, sort=True).ffill(limit=2).dropna()
    for d, g in j.groupby(j.index.normalize()):
        if d.dayofweek >= 5:
            continue
        g = g.between_time(DAY_START, "17:59")
        win = g.between_time("15:00", "17:59")
        if len(win) < 30 or len(g) < len(win) + max_l:
            continue
        yield d, g


def rolling_z(yc: np.ndarray, xc: np.ndarray, L: int):
    n = len(yc)
    z = np.full(n, np.nan)
    beta = np.full(n, np.nan)
    for t in range(L - 1, n):
        ys, xs = yc[t - L + 1:t + 1], xc[t - L + 1:t + 1]
        xm, ym = xs.mean(), ys.mean()
        vx = ((xs - xm) ** 2).sum()
        if vx <= 0:
            continue
        b = ((xs - xm) * (ys - ym)).sum() / vx
        e = ys - ym - b * (xs - xm)
        sd = e.std()
        if sd > 0:
            z[t], beta[t] = e[-1] / sd, b
    return z, beta


def simulate(g: pd.DataFrame, z, beta, k_in, k_out, k_stop, cost_y, cost_x):
    """그날 거래 목록 [(진입시각, 방향, 순손익 $)]. 체결은 다음 봉 시가."""
    idx = g.index
    clock = idx.strftime("%H:%M")
    yo, xo = g[("y", "o")].values, g[("x", "o")].values
    trades, pos, entry = [], 0, None
    for t in range(len(g) - 1):
        if clock[t] < "15:00":
            continue
        nxt = t + 1
        if pos != 0:
            zt = z[t]
            done = clock[nxt] >= FLAT_AT or np.isnan(zt) or abs(zt) < k_out or abs(zt) > k_stop \
                or (pos == -1 and zt < k_out) or (pos == 1 and zt > -k_out)
            if done:
                t0, b, y0, x0 = entry
                ret = pos * ((yo[nxt] - y0) - b * (xo[nxt] - x0))
                cost = (cost_y + abs(b) * cost_x) / 1e4
                trades.append((idx[t0], pos, (ret - cost) * NOTIONAL, abs(zt) > k_stop))
                pos = 0
            continue
        if clock[t] >= LAST_ENTRY or np.isnan(z[t]):
            continue
        if z[t] > k_in:
            pos = -1
        elif z[t] < -k_in:
            pos = 1
        if pos:
            entry = (nxt, beta[t], yo[nxt], xo[nxt])
    if pos != 0:  # 데이터 끝 — 마지막 봉 시가로 정리
        t0, b, y0, x0 = entry
        ret = pos * ((yo[-1] - y0) - b * (xo[-1] - x0))
        trades.append((idx[t0], pos, (ret - (cost_y + abs(b) * cost_x) / 1e4) * NOTIONAL, False))
    return trades


def stats(tr: list) -> dict:
    if not tr:
        return {"trades": 0, "pnl": 0.0, "per_trade": np.nan, "win": np.nan, "stops": 0}
    p = np.array([t[2] for t in tr])
    return {"trades": len(p), "pnl": round(p.sum(), 1), "per_trade": round(p.mean(), 2),
            "win": round((p > 0).mean(), 2), "stops": int(sum(t[3] for t in tr))}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cost-x", type=float, default=1.0, help="비용 배수 (스트레스 테스트)")
    args = ap.parse_args()
    data = {s: load(s) for s in {p for pair in PAIRS for p in pair}}
    rows, summary = [], []
    for ys, xs in PAIRS:
        cy, cx = cost_bp(ys, args.cost_x), cost_bp(xs, args.cost_x)
        days = list(day_frames(data[ys], data[xs], max(GRID["L"])))
        res = {}
        for L in GRID["L"]:
            zb = [(d, g, *rolling_z(g[("y", "c")].values, g[("x", "c")].values, L)) for d, g in days]
            for k_in, k_out, k_stop in itertools.product(GRID["k_in"], GRID["k_out"], GRID["k_stop"]):
                tr_train, tr_val = [], []
                for d, g, z, b in zb:
                    tr = simulate(g, z, b, k_in, k_out, k_stop, cy, cx)
                    (tr_train if d < TRAIN_END else tr_val).extend(tr)
                key = (L, k_in, k_out, k_stop)
                res[key] = (stats(tr_train), stats(tr_val))
                rows.append({"pair": f"{ys}-{xs}", "L": L, "k_in": k_in, "k_out": k_out, "k_stop": k_stop,
                             **{f"tr_{k}": v for k, v in res[key][0].items()},
                             **{f"va_{k}": v for k, v in res[key][1].items()}})
        best = max(res, key=lambda k: res[k][0]["pnl"])
        all_tr = np.array([v[0]["pnl"] for v in res.values()])
        all_va = np.array([v[1]["pnl"] for v in res.values()])
        n_train = sum(1 for d, _ in days if d < TRAIN_END)
        summary.append({
            "pair": f"{ys}-{xs}", "legs": f"{SPEC[ys][4]}/{SPEC[xs][4]}", "cost_bp": round(cy + cx, 2),
            "days_tr": n_train, "days_va": len(days) - n_train,
            "best": f"L{best[0]} in{best[1]} out{best[2]} stop{best[3]:g}",
            "tr_pnl": res[best][0]["pnl"], "tr_trades": res[best][0]["trades"],
            "va_pnl": res[best][1]["pnl"], "va_trades": res[best][1]["trades"], "va_win": res[best][1]["win"],
            "grid_tr_pos": round(float((all_tr > 0).mean()), 2), "grid_va_pos": round(float((all_va > 0).mean()), 2),
            "grid_va_mean": round(float(all_va.mean()), 1),
        })
        print(summary[-1], flush=True)
    suffix = "" if args.cost_x == 1 else f"_cost{args.cost_x:g}x"
    pd.DataFrame(rows).to_csv(ROOT / "results" / f"rv_grid{suffix}.csv", index=False)
    s = pd.DataFrame(summary)
    s.to_csv(ROOT / "results" / f"rv_summary{suffix}.csv", index=False)
    pd.set_option("display.width", 220)
    print("\n" + s.to_string(index=False))


if __name__ == "__main__":
    main()
