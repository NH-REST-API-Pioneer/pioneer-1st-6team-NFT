"""신호 계수 B 와 감쇠 Φ 추정 — 학습 구간만, 8종목을 한데 묶어서 (논문 식 31·32 방식).

  r_{t+1}/σ_t = c + Σ_k β_k f_t^k + u          (묶은 OLS, 표준오차는 날짜별 군집)
  Δf_{t+1}^k  = −φ_k f_t^k + ε                  (종목별 1 − AR(1) 의 평균)

  .venv/bin/python estimate.py          → results/params.json, results/estimate.md
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

import bars
import config
import signals


def stack(period: tuple[str, str]) -> pd.DataFrame:
    rows = []
    p = signals.build({s: bars.load(s) for s in config.SYMS})
    y = signals.target(p)
    for s in config.SYMS:
        df = pd.DataFrame({"y": y[s], **{k: p.F[k][s] for k in config.SIGNALS}}, index=p.idx)
        df["sym"], df["grp"] = s, config.ASSET[s]
        rows.append(df)
    X = pd.concat(rows).replace([np.inf, -np.inf], np.nan).dropna()
    d = X.index.strftime("%Y-%m-%d")
    return X[(d >= period[0]) & (d <= period[1])]


def ols_cluster(D: pd.DataFrame, cols) -> tuple[np.ndarray, np.ndarray]:
    A = np.column_stack([np.ones(len(D))] + [D[c].values for c in cols])
    y = D["y"].values
    b, *_ = np.linalg.lstsq(A, y, rcond=None)
    e = y - A @ b
    g = pd.Series(D.index.normalize()).factorize()[0]
    XtXi = np.linalg.inv(A.T @ A)
    S = np.zeros((A.shape[1],) * 2)
    for k in np.unique(g):
        m = g == k
        u = A[m].T @ e[m]
        S += np.outer(u, u)
    return b, b / np.sqrt(np.diag(XtXi @ S @ XtXi))


def phi(D: pd.DataFrame, col: str) -> float:
    out = []
    for _, g in D.groupby("sym"):
        x = g[col].values
        out.append(1 - np.corrcoef(x[:-1], x[1:])[0, 1])
    return float(np.mean(out))


def main() -> None:
    cols = list(config.SIGNALS)
    tr = stack(config.TRAIN)
    va = stack(config.VAL)
    b, t = ols_cluster(tr, cols)
    params = {"betas": dict(zip(cols, map(float, b[1:]))), "phis": {c: phi(tr, c) for c in cols},
              "t": dict(zip(cols, map(float, t[1:]))), "n_train": int(len(tr)), "period": config.TRAIN}
    config.RESULTS.mkdir(parents=True, exist_ok=True)
    (config.RESULTS / "params.json").write_text(json.dumps(params, indent=2, ensure_ascii=False))

    lines = ["# 신호 계수·감쇠 (학습 구간 추정)", "",
             f"학습 {config.TRAIN[0]}~{config.TRAIN[1]}, {len(config.SYMS)}종목 묶음 n = {len(tr):,}. 대상 = 다음 봉 표준화 수익률.", "",
             "| 신호 | β (×10⁻³) | t (날짜 군집) | φ (봉당) | 반감기 (봉) | 검증 β (×10⁻³) | 검증 t |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    bv, tv = ols_cluster(va, cols)
    for i, c in enumerate(cols):
        ph = params["phis"][c]
        lines.append(f"| {c} | {b[i+1]*1e3:+.2f} | {t[i+1]:+.2f} | {ph:.4f} | {np.log(2)/ph:.1f} | "
                     f"{bv[i+1]*1e3:+.2f} | {tv[i+1]:+.2f} |")
    lines += ["", "신호 하나씩 (학습 β ×10⁻³, t)", "", "| 신호 | 학습 | 검증 |", "| --- | --- | --- |"]
    for c in cols:
        b1, t1 = ols_cluster(tr, [c]); b2, t2 = ols_cluster(va, [c])
        lines.append(f"| {c} | {b1[1]*1e3:+.2f} ({t1[1]:+.2f}) | {b2[1]*1e3:+.2f} ({t2[1]:+.2f}) |")
    lines += ["", "종목별 (학습, 함께 회귀)", "", "| 종목 | " + " | ".join(f"{c} β (t)" for c in cols) + " |",
              "| --- |" + " --- |" * len(cols)]
    for g in config.SYMS:
        bg, tg = ols_cluster(tr[tr.sym == g], cols)
        lines.append(f"| {g} | " + " | ".join(f"{bg[i+1]*1e3:+.2f} ({tg[i+1]:+.1f})" for i in range(len(cols))) + " |")
    (config.RESULTS / "estimate.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
