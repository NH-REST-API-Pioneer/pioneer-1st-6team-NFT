"""판정 — 논문 Table 2·Exhibit 8 처럼 기준선 · GP · DMN 을 비용 전·후로 나란히.

  .venv/bin/python verdict.py --period dev                # 개발용 표본 밖 블록 2011~2015, 2016~2020
  .venv/bin/python verdict.py --period test --confirm     # 시험 2021-01~2026-11, 한 번만 (잠금 파일)

블록마다 그 시작 전 데이터(2005-01~)로 GP 추정·DMN 학습을 처음부터 다시 함 (논문의 5년 재학습).
비교 (모두 같은 비용: 실제 편도 비용률):
  매수 보유 · TSMOM sgn(12개월) · MACD (논문 기준선)
  Markowitz · GP (k 는 학습 마지막 10% 검증에서 고름)
  DMN 검증 1등 (논문 방식) · DMN 앙상블 (채택: 상위 3설정 × 시드 3) · 같은 앙상블을 비용 벌점 없이(c=0) 학습
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime

import numpy as np
import pandas as pd
import torch

import backtest
import baselines
import config
import dmn
import features
import gp_daily

LOCK = config.RESULTS / "TEST_RUN.lock"


def config_hash() -> str:
    h = hashlib.sha256()
    for f in ["config.py", "features.py", "data.py", "dmn.py", "gp_daily.py", "backtest.py", "baselines.py"]:
        h.update((config.HERE / f).read_bytes())
    return h.hexdigest()[:16]


def train_end(b0: str) -> str:
    return (pd.Timestamp(b0) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")


def run_block(P, A, F, b0: str, b1: str, out, log) -> dict[str, pd.DataFrame]:
    end = train_end(b0)
    tr = ("2005-01-01", end)
    sig = backtest.align(P, "sigma")
    days = sig.loc[tr[0]:end].index
    vstart = days[int(len(days) * (1 - config.VAL_FRAC))].strftime("%Y-%m-%d")
    W = {}
    W["매수 보유"] = backtest.weights(baselines.long_only(P), sig)
    W["TSMOM sgn(12개월)"] = backtest.weights(baselines.tsmom(P), sig)
    W["MACD (Baz 외)"] = backtest.weights(baselines.macd(P), sig)
    ref_vol = backtest.stats(backtest.evaluate(W["TSMOM sgn(12개월)"], P, tr)["gross"])["ann_vol"]

    betas, phis = gp_daily.fit(P, F, end)
    scores = []
    for k in gp_daily.K_GRID:
        Wk, _ = gp_daily.scale_to(gp_daily.run(P, F, betas, phis, k), P, ref_vol, tr)
        scores.append((backtest.summary(backtest.evaluate(Wk, P, (vstart, end)))["sharpe_net"], k, Wk))
    sv, k_best, Wgp = max(scores, key=lambda x: x[0])
    W["Markowitz"] = gp_daily.scale_to(gp_daily.run(P, F, betas, phis, 1, mode="mark"), P, ref_vol, tr)[0]
    W[f"GP (k={k_best})"] = Wgp
    log(f"  GP β {dict((a, round(b*1e3, 2)) for a, b in betas.items())} φ {dict((a, round(b, 4)) for a, b in phis.items())} → k={k_best} (검증 샤프 {sv:+.2f})")

    info = {"gp": {"betas": betas, "phis": phis, "k": k_best, "val_sharpe": sv}}
    for tag, zero_cost in [("dmn", False), ("dmn_nocost", True)]:
        A2 = A if not zero_cost else dmn.Arrays(A.dates, A.X, A.scale, A.nr, np.zeros_like(A.c), A.ok)
        log(f"  {tag} 학습 {tr[0]}~{end} (검증 {vstart}~)")
        net, inf = dmn.fit_block(A2, tr[0], end, log=log)
        nets = dmn.fit_ensemble(A2, inf, tr[0], end)
        for m, n_ in enumerate([net] + nets):
            torch.save(n_.state_dict(), out / f"{tag}_{b0[:4]}_{m}.pt")
        info[tag] = inf
        Xe = dmn.predict_ensemble(nets, A, b0, b1)
        if not zero_cost:
            Xb = dmn.predict(net, A, b0, b1)
            W["DMN 검증 1등 (논문 방식)"] = backtest.weights(Xb, sig.loc[Xb.index])
            W["DMN 앙상블 (채택)"] = backtest.weights(Xe, sig.loc[Xe.index])
            Xe.to_csv(out / f"dmn_positions_{b0[:4]}.csv")
        else:
            W["DMN 앙상블, 비용 벌점 없음"] = backtest.weights(Xe, sig.loc[Xe.index])
    (out / f"info_{b0[:4]}.json").write_text(json.dumps(info, indent=2, ensure_ascii=False, default=float))
    return W


def table(rows: list[tuple[str, dict]]) -> list[str]:
    L = ["| 전략 | 샤프 비용 전 | 샤프 비용 후 | 연수익 (후) | 연변동 | 최대낙폭 | 비용 bp/년 | 회전/년 | 승률 |",
         "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for n, s in rows:
        L.append(f"| {n} | {s['sharpe_gross']:+.2f} | {s['sharpe_net']:+.2f} | {s['ret_net']*100:+.1f}% | "
                 f"{s['vol']*100:.1f}% | {s['mdd']*100:.1f}% | {s['cost_ann']*1e4:.0f} | {s['turnover_ann']:.1f} | "
                 f"{s['hit']*100:.0f}% |")
    return L


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", choices=["dev", "test"], default="dev")
    ap.add_argument("--confirm", action="store_true")
    a = ap.parse_args()
    if a.period == "test":
        if not a.confirm:
            sys.exit("시험 구간은 --confirm 을 붙여야 돕니다 (한 번만).")
        if LOCK.exists():
            sys.exit(f"이미 돌렸습니다: {LOCK.read_text()}")
        LOCK.parent.mkdir(parents=True, exist_ok=True)
        LOCK.write_text(json.dumps({"run_at": datetime.now().isoformat(timespec="seconds"),
                                    "code_hash": config_hash()}, ensure_ascii=False))
    blocks = config.BLOCKS if a.period == "dev" else [config.TEST]
    out = config.RESULTS / a.period
    out.mkdir(parents=True, exist_ok=True)
    logf = open(out / "log.txt", "w")

    def log(msg):
        print(msg, flush=True)
        logf.write(msg + "\n")

    P = features.panel()
    A = dmn.arrays(P)
    F = gp_daily.signals(P)
    lines = [f"# 판정 — {a.period}", "", f"생성 {datetime.now().isoformat(timespec='seconds')} · 코드 {config_hash()}",
             "", "비용 = 실제 편도 비용률 (수수료 $2 + 반 틱, 마이크로 계약 기준). 비중 = X·15%/σ/N.", ""]
    allnet, daily = {}, {}
    for b0, b1 in blocks:
        log(f"== 블록 {b0}~{b1}")
        W = run_block(P, A, F, b0, b1, out, log)
        rows = []
        for n, w in W.items():
            ev = backtest.evaluate(w, P, (b0, b1))
            rows.append((n, backtest.summary(ev)))
            allnet.setdefault(n, []).append(ev["net"])
            daily.setdefault(b0[:4], {})[n] = ev["net"]
        lines += [f"## {b0[:4]}~{b1[:4]} (표본 밖)", ""] + table(rows) + [""]
    if len(blocks) > 1:
        lines += ["## 블록 합친 비용 후 샤프", "", "| 전략 | 샤프 (후) | t |", "| --- | --- | --- |"]
        for n, parts in allnet.items():
            if len(parts) < len(blocks):     # 이름에 k 가 붙은 GP 는 블록마다 달라서 따로 묶음
                continue
            r = pd.concat(parts)
            sh = r.mean() / r.std() * np.sqrt(252)
            t = r.mean() / r.std() * np.sqrt(len(r))
            lines.append(f"| {n} | {sh:+.2f} | {t:+.2f} |")
    for y, d in daily.items():
        pd.DataFrame(d).to_csv(out / f"daily_net_{y}.csv")     # 전략별 일별 비용 후 수익 (열 = 전략)
    (out / "summary.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
