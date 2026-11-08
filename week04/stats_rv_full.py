"""채택값(하루 전체, L72·진입3.5σ·청산0·손절1.5σ·발표 금지)의 거래 통계와 유의성.

  1. 거래 요약: 건수·승률·평균 보유·청산 사유·최대 낙폭·최악 거래 (학습/검증)
  2. 부트스트랩: 거래당 손익(bp) 평균이 0 보다 큰지, 비용 전·후, 10,000번
  3. z 예측 회귀: h 봉 뒤 스프레드 변화(진입 β 고정, σ 로 나눔) ~ z_t, Newey-West. 기울기 < 0 이면 되돌림
  4. 비용 2배

  python3 stats_rv_full.py
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import statsmodels.api as sm

import backtest_rv as B
import backtest_rv_full as F
from rv_engine import RVParams

rng = np.random.default_rng(0)


def trades_detail(j, p: RVParams, cost_mult: float):
    """simulate 와 같은 규칙인데 청산 사유·보유 시간까지."""
    idx = j.index
    yc, xc = j[("y", "c")].values, j[("x", "c")].values
    yo, xo = j[("y", "o")].values, j[("x", "o")].values
    z, beta, sd = F.rolling_zs(yc, xc, p.L)
    ok = (idx.to_series().diff(p.L - 1).dt.total_seconds() / 60).values <= (p.L - 1) * 5 + 15
    blk = F.news_blackout(idx)
    brk = np.r_[(np.diff(idx.values) > F.BREAK.to_timedelta64()), True]
    cy, cx = B.cost_bp("GC", cost_mult), B.cost_bp("SI", cost_mult)
    out, pos = [], 0
    for t in range(len(j) - 1):
        if pos:
            t0, b, y0, x0, s0, sd0 = e
            adverse = -pos * ((yc[t] - b * xc[t]) - s0)
            why = ("휴장 전" if brk[t] else "발표 구간" if blk[t] else "손절" if adverse > p.stop_m * sd0
                   else "z 없음" if np.isnan(z[t]) else
                   "평균 복귀" if (pos == -1 and z[t] < p.k_out) or (pos == 1 and z[t] > -p.k_out) else None)
            if why:
                py, px = (yc[t], xc[t]) if brk[t] else (yo[t + 1], xo[t + 1])
                gross = pos * ((py - y0) - b * (px - x0)) * 1e4
                c = cy + abs(b) * cx
                out.append({"entry": idx[t0], "exit": idx[t + 1] if not brk[t] else idx[t], "side": pos,
                            "beta": b, "gross_bp": gross, "net_bp": gross - c, "reason": why})
                pos = 0
            continue
        if brk[t] or blk[t] or not ok[t] or np.isnan(z[t]):
            continue
        pos = -1 if z[t] > p.k_in else 1 if z[t] < -p.k_in else 0
        if pos:
            e = (t + 1, beta[t], yo[t + 1], xo[t + 1], yc[t] - beta[t] * xc[t], sd[t])
    df = pd.DataFrame(out)
    df["hold_min"] = (df.exit - df.entry).dt.total_seconds() / 60
    df["net_usd"] = df.net_bp * B.NOTIONAL / 1e4
    df["va"] = df.entry >= B.TRAIN_END
    return df


def summary(df: pd.DataFrame) -> dict:
    eq = df.net_usd.cumsum()
    return {"n": len(df), "net_usd": round(df.net_usd.sum(), 1), "win": round((df.net_usd > 0).mean(), 2),
            "avg_bp": round(df.net_bp.mean(), 2), "gross_bp": round(df.gross_bp.mean(), 2),
            "hold_med_min": round(df.hold_min.median()), "worst_usd": round(df.net_usd.min(), 1),
            "best_usd": round(df.net_usd.max(), 1), "max_dd_usd": round(float((eq.cummax() - eq).max()), 1)}


def boot(x: np.ndarray, n: int = 10_000):
    m = rng.choice(x, size=(n, len(x)), replace=True).mean(axis=1)
    return round(x.mean(), 2), np.round(np.percentile(m, [2.5, 97.5]), 2), round(float((m <= 0).mean()), 3)


def z_regression(j, L: int, h: int):
    yc, xc = j[("y", "c")].values, j[("x", "c")].values
    idx = j.index
    z, beta, sd = F.rolling_zs(yc, xc, L)
    ok = (idx.to_series().diff(L - 1).dt.total_seconds() / 60).values <= (L - 1) * 5 + 15
    blk = F.news_blackout(idx)
    span = (idx.to_series().shift(-h) - idx.to_series()).dt.total_seconds().values / 60
    X, Y = [], []
    for t in range(len(j) - h):
        if not ok[t] or blk[t] or np.isnan(z[t]) or span[t] > h * 5 + 15:
            continue
        b = beta[t]
        Y.append(((yc[t + h] - b * xc[t + h]) - (yc[t] - b * xc[t])) / sd[t])
        X.append(z[t])
    res = sm.OLS(np.array(Y), sm.add_constant(np.array(X))).fit(cov_type="HAC", cov_kwds={"maxlags": h})
    return round(res.params[1], 3), round(res.tvalues[1], 2), len(X)


def main() -> None:
    j = F.prepare()
    p = RVParams()
    df = trades_detail(j, p, 1.0)
    print("1) 거래 요약 (금 명목 $10만 기준)")
    for name, part in (("학습", df[~df.va]), ("검증", df[df.va]), ("전체", df)):
        print(f"  {name}: {summary(part)}")
    print("  청산 사유:", df.reason.value_counts().to_dict())
    print("  진입 방향: 스프레드 매수", int((df.side == 1).sum()), "/ 매도", int((df.side == -1).sum()),
          "· 진입 β 중앙값", round(df.beta.median(), 2))
    df.to_csv(B.ROOT / "results" / "rv_final_trades.csv", index=False)

    print("\n2) 부트스트랩 — 거래당 bp 평균, 95% 구간, P(평균 ≤ 0)")
    for name, part in (("전체", df), ("검증", df[df.va])):
        print(f"  {name} 비용 전 {boot(part.gross_bp.values)} · 비용 후 {boot(part.net_bp.values)}")

    print("\n3) z 예측 회귀 (기울기, t, 표본) — 음수면 벌어진 만큼 되돌아옴")
    for L in (36, 72):
        for h in (6, 12):
            print(f"  L{L} {h * 5}분 뒤: {z_regression(j, L, h)}")

    d2 = trades_detail(j, p, 2.0)
    print("\n4) 비용 2배:", {k: summary(v) for k, v in (("학습", d2[~d2.va]), ("검증", d2[d2.va]))})


if __name__ == "__main__":
    main()
