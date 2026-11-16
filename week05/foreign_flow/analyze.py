"""외국인 흐름 → 미니코스피200 다음 몇 분 수익률. 학습일로 규칙을 고르고, 시험일에서 그대로 판정.

  .venv/bin/python analyze.py --train 20261117                              # 화요일 밤: 규칙 고르기 → results/rules.json
  .venv/bin/python analyze.py --train 20261117 --test 20261118,20261119,20261120   # 금요일: 표본 밖 판정

신호 (모두 t 행까지 받은 값만, convert.py 의 시각 규칙):
  frgn_fut_k   직전 k 분 외국인 선물 순매수 변화 (코스피200 계약 + 미니/5 → 코스피200 계약 환산)
  frgn_opt_k   직전 k 분 외국인 (콜 순매수 − 풋 순매수) 변화
  ind_fut_k    직전 k 분 개인 선물 순매수 변화 (반대 방향 신호인지 보려고)
  aggr_k       직전 k 분 매수주도 − 매도주도 체결 (코스피200 + 미니/5) — 투자자 구분 없는 비교 신호
  각 신호는 학습일의 표준편차로 나눠 z 로 씀 (시험일에도 학습일 값 그대로).
목표: 미니 중간가 로그수익률 h 분 뒤 (bp). 진입 가능 시각 09:05~(15:35 − h) — 시가 동시호가 직후와 종가 단일가 전 구간 제외.
회귀: r_h = a + b·z, Newey-West 표준오차 (lag = h, numpy 로 직접 계산).
규칙: 학습일에서 |t| 가 가장 큰 (신호, k, h) 하나 + 문턱 θ (학습일 비용 후 평균 1등). 방향 = 학습 β 부호.
       |z| > θ 이면 그 방향으로 1계약, h 분 보유, 보유 중 새 진입 없음.
비용: 왕복 = 1틱(0.02pt) + 수수료 2 × FEE_KRW. 미니 1pt = 50,000원.
"""
from __future__ import annotations

import argparse
import json
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

from convert import PANEL_DIR
from record import HERE

RESULTS = HERE / "results"
KS = (1, 5, 15)
HS = (5, 15, 30)
THETAS = (0.5, 1.0, 1.5, 2.0, 3.0)
TICK_PT, PT_KRW = 0.02, 50_000
FEE_KRW = 1_500          # 미니 1계약 편도 수수료 가정 — 첫 모의 체결의 예수금 변화로 확인해서 고칠 것
START, LAST_EXIT = "09:05", "15:35"


def load(days: list[str]) -> dict[str, pd.DataFrame]:
    return {d: pd.read_csv(PANEL_DIR / f"{d}.csv", index_col="t", parse_dates=True) for d in days}


def features(p: pd.DataFrame) -> pd.DataFrame:
    f = pd.DataFrame(index=p.index)
    fut = p.get("011_frgn", 0) + p.get("051_frgn", 0) / 5
    opt = p.get("012_frgn", 0) - p.get("013_frgn", 0)
    ind = p.get("011_ind", 0) + p.get("051_ind", 0) / 5
    aggr = p.get("K200_aggr", 0) + p.get("MINI_aggr", 0) / 5
    for k in KS:
        f[f"frgn_fut_{k}"] = fut.diff(k)
        f[f"frgn_opt_{k}"] = opt.diff(k)
        f[f"ind_fut_{k}"] = ind.diff(k)
        f[f"aggr_{k}"] = aggr.rolling(k).sum()
    mid = p["MINI_mid"].where(p["MINI_spread"] > 0)
    for h in HS:
        f[f"r_{h}"] = (np.log(mid.shift(-h) / mid)) * 1e4
    f["mid"] = mid
    return f


def usable(idx: pd.DatetimeIndex, h: int) -> np.ndarray:
    hm = idx.strftime("%H:%M")
    last = (pd.Timestamp("2000-01-01 " + LAST_EXIT) - pd.Timedelta(minutes=h)).strftime("%H:%M")
    return (hm >= START) & (hm <= last)


def stack(F: dict[str, pd.DataFrame], sig: str, h: int) -> pd.DataFrame:
    parts = []
    for d, f in F.items():
        m = usable(f.index, h)
        parts.append(f.loc[m, [sig, f"r_{h}"]].assign(day=d))
    return pd.concat(parts).dropna()


def nw_ols(x: pd.Series, y: pd.Series, lag: int) -> dict:
    """OLS y = a + b x, Newey-West(Bartlett) 표준오차. (statsmodels 는 이 맥에서 scipy 바이너리가 안 올라와서 직접 씀)"""
    X = np.column_stack([np.ones(len(x)), np.asarray(x, float)])
    yv = np.asarray(y, float)
    XtXi = np.linalg.inv(X.T @ X)
    beta = XtXi @ X.T @ yv
    e = yv - X @ beta
    u = X * e[:, None]
    S = u.T @ u
    for l in range(1, lag + 1):
        w = 1 - l / (lag + 1)
        G = u[l:].T @ u[:-l]
        S += w * (G + G.T)
    V = XtXi @ S @ XtXi
    r2 = 1 - e.var() / yv.var() if yv.var() > 0 else 0.0
    return {"b": float(beta[1]), "t": float(beta[1] / np.sqrt(V[1, 1])), "r2": float(r2), "n": int(len(yv))}


def cost_bp(mid: float) -> float:
    rt_pt = TICK_PT + 2 * FEE_KRW / PT_KRW
    return rt_pt / mid * 1e4


def simulate(f: pd.DataFrame, sig: str, h: int, theta: float, sign: float, sd: float) -> list[dict]:
    """하루 안에서 규칙대로 진입·청산. 보유 중엔 새 진입 없음."""
    m = usable(f.index, h)
    z = f[sig] / sd
    trades, busy_until = [], None
    for t in f.index[m]:
        if busy_until is not None and t < busy_until:
            continue
        zt, r, mid = z.get(t), f.at[t, f"r_{h}"], f.at[t, "mid"]
        if not np.isfinite(zt) or not np.isfinite(r) or abs(zt) <= theta:
            continue
        side = np.sign(zt) * sign
        trades.append({"t": t, "side": int(side), "z": float(zt), "gross_bp": float(side * r),
                       "net_bp": float(side * r - cost_bp(mid))})
        busy_until = t + pd.Timedelta(minutes=h)
    return trades


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", required=True)
    ap.add_argument("--test", default="")
    a = ap.parse_args()
    tr_days = a.train.split(",")
    te_days = [d for d in a.test.split(",") if d]
    Ftr = {d: features(p) for d, p in load(tr_days).items()}
    sigs = [c for c in next(iter(Ftr.values())).columns if not c.startswith("r_") and c != "mid"]
    RESULTS.mkdir(exist_ok=True)
    lines = [f"# 외국인 흐름 분석 — 학습 {','.join(tr_days)}" + (f" · 시험 {','.join(te_days)}" if te_days else ""), ""]

    # 1) 학습일 회귀표
    rows, sd = [], {}
    for sig, h in product(sigs, HS):
        D = stack(Ftr, sig, h)
        if len(D) < 30 or D[sig].std() == 0:
            continue
        sd[sig] = float(D[sig].std())
        res = nw_ols(D[sig] / sd[sig], D[f"r_{h}"], h)
        rows.append({"signal": sig, "h": h, **res})
    R = pd.DataFrame(rows).sort_values("t", key=np.abs, ascending=False)
    lines += ["## 학습일 회귀 (r_h bp = a + b·z, Newey-West t)", "", "| 신호 | h분 | b (bp/표준편차) | t | R² | n |",
              "| --- | --- | --- | --- | --- | --- |"]
    lines += [f"| {r.signal} | {r.h} | {r.b:+.2f} | {r.t:+.2f} | {r.r2:.4f} | {r.n} |" for r in R.itertuples()]

    # 2) 규칙 고르기 (학습일만)
    best = R.iloc[0]
    sign = float(np.sign(best.b))
    scores = []
    for th in THETAS:
        tr = [x for f in Ftr.values() for x in simulate(f, best.signal, int(best.h), th, sign, sd[best.signal])]
        mean = np.mean([x["net_bp"] for x in tr]) if tr else -np.inf
        scores.append((mean, th, len(tr)))
    m, theta, n = max(scores)
    rule = {"signal": best.signal, "h": int(best.h), "theta": theta, "sign": sign, "sd": sd[best.signal],
            "train_days": tr_days, "train_t": float(best.t), "train_net_bp_mean": float(m), "train_trades": n,
            "fee_krw": FEE_KRW, "chosen_at": pd.Timestamp.now().isoformat(timespec="seconds")}
    lines += ["", "## 규칙 (학습일에서 고름)", "", "| θ | 거래 수 | 비용 후 평균 bp |", "| --- | --- | --- |"]
    lines += [f"| {th} | {k} | {mm:+.2f} |" for mm, th, k in scores]
    lines += ["", f"→ {json.dumps(rule, ensure_ascii=False)}"]
    if not te_days:
        (RESULTS / "rules.json").write_text(json.dumps(rule, indent=2, ensure_ascii=False))
        lines += ["", "results/rules.json 에 고정. 시험일에는 이 파일을 바꾸지 않음."]

    # 3) 시험일 판정 (규칙은 rules.json 그대로)
    if te_days:
        rule = json.loads((RESULTS / "rules.json").read_text())
        Fte = {d: features(p) for d, p in load(te_days).items()}
        D = stack(Fte, rule["signal"], rule["h"])
        res = nw_ols(D[rule["signal"]] / rule["sd"], D[f"r_{rule['h']}"], rule["h"])
        te = [x for f in Fte.values() for x in simulate(f, rule["signal"], rule["h"], rule["theta"], rule["sign"], rule["sd"])]
        net = np.array([x["net_bp"] for x in te])
        tstat = net.mean() / net.std(ddof=1) * np.sqrt(len(net)) if len(net) > 1 and net.std() > 0 else np.nan
        lines += ["", "## 시험일 (표본 밖)", "", f"회귀: b {res['b']:+.2f} bp, t {res['t']:+.2f}, n {res['n']} (학습 b 부호 {rule['sign']:+.0f})",
                  f"규칙: 거래 {len(te)}건, 비용 전 평균 {np.mean([x['gross_bp'] for x in te]) if te else float('nan'):+.2f} bp, "
                  f"비용 후 평균 {net.mean() if len(net) else float('nan'):+.2f} bp, t {tstat:+.2f}, 승률 {(net > 0).mean() * 100 if len(net) else float('nan'):.0f}%"]
        pd.DataFrame(te).to_csv(RESULTS / "test_trades.csv", index=False)
    tag = "_".join(tr_days) + (("__" + "_".join(te_days)) if te_days else "")
    (RESULTS / f"analysis_{tag}.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
