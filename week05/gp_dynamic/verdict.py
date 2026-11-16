"""판정 — 논문 Table II 처럼 GP · Markowitz · 정적 부분 매매를 비용 전·후로 나란히.

  .venv/bin/python verdict.py --period trainval            # 학습·검증
  .venv/bin/python verdict.py --period test --confirm      # 시험 구간, 한 번만 (잠금 파일)

학습 구간에서 고르는 것 (표에 그대로 남김):
  k  — K_GRID 중 학습 순손익 1등. config.K 와 다르면 경고만 하고 config.K 를 씀 (시험 전에 config 를 고쳐야 함)
  w  — 정적 전략의 매매 속도, STATIC_W_GRID 중 학습 순손익 1등 (논문도 정적 전략은 가장 좋은 속도와 비교)
비교·변형: 신호 하나씩만 쓴 GP, γ 를 ½·2배로 바꾼 GP, 종목별 손익.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime

import numpy as np
import pandas as pd

import backtest
import bars
import config
import engine
import signals

LOCK = config.RESULTS / "verdict" / "TEST_RUN.lock"


def params(**kw) -> engine.Params:
    P = json.loads((config.RESULTS / "params.json").read_text())
    return engine.Params(P["betas"], P["phis"], **kw)


def pick(panel, cov, grid, make) -> tuple[float, list[tuple[float, float]]]:
    scores = [(g, backtest.run(panel, make(g), config.TRAIN, cov).net) for g in grid]
    return max(scores, key=lambda x: x[1])[0], scores


def strategies(k: float, w: float) -> dict[str, engine.Params]:
    return {
        "GP (채택)": params(k=k),
        "Markowitz (비용 무시)": params(mode="markowitz"),
        f"정적 부분 매매 w={w}": params(mode="static", static_w=w),
        **{f"GP {c}만": params(k=k, signals=(c,)) for c in config.SIGNALS},
        "GP γ×½ (2배 크기)": params(k=k, gamma=config.GAMMA / 2),
        "GP γ×2 (½ 크기)": params(k=k, gamma=config.GAMMA * 2),
    }


def config_hash() -> str:
    return hashlib.sha256((config.HERE / "config.py").read_bytes()
                          + (config.RESULTS / "params.json").read_bytes()).hexdigest()[:16]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", choices=["trainval", "test"], default="trainval")
    ap.add_argument("--confirm", action="store_true")
    a = ap.parse_args()
    if a.period == "test":
        if not a.confirm:
            sys.exit("시험 구간은 --confirm 을 붙여야 돕니다 (한 번만).")
        if LOCK.exists():
            sys.exit(f"이미 돌렸습니다: {LOCK.read_text()}")
        LOCK.parent.mkdir(parents=True, exist_ok=True)
        LOCK.write_text(json.dumps({"run_at": datetime.now().isoformat(timespec="seconds"),
                                    "config_hash": config_hash()}, ensure_ascii=False))
    periods = {"학습": config.TRAIN, "검증": config.VAL} if a.period == "trainval" else {"시험": config.TEST}

    out = config.RESULTS / "verdict" / a.period
    out.mkdir(parents=True, exist_ok=True)
    lines = [f"# 판정 — {a.period}", "", f"생성 {datetime.now().isoformat(timespec='seconds')} · "
             f"config {config_hash()}", ""]

    pm = signals.build({s: bars.load(s) for s in config.SYMS})
    cm = backtest.cov_stack(pm)
    k_best, k_scores = pick(pm, cm, config.K_GRID, lambda k: params(k=k))
    w_best, w_scores = pick(pm, cm, config.STATIC_W_GRID, lambda w: params(mode="static", static_w=w))
    if k_best != config.K:
        print(f"경고: 학습 1등 k={k_best} 인데 config.K={config.K}", flush=True)
    lines += ["## 학습 구간에서 고른 값", "",
              "| k = λ/γ | " + " | ".join(str(k) for k, _ in k_scores) + " |",
              "| --- |" + " --- |" * len(k_scores),
              "| 학습 순손익 $ | " + " | ".join(f"{v:,.0f}" for _, v in k_scores) + " |", "",
              f"→ k = {config.K} (학습 1등 {k_best})", "",
              "| 정적 w | " + " | ".join(str(w) for w, _ in w_scores) + " |",
              "| --- |" + " --- |" * len(w_scores),
              "| 학습 순손익 $ | " + " | ".join(f"{v:,.0f}" for _, v in w_scores) + " |", "",
              f"→ w = {w_best}", ""]

    S = strategies(config.K, w_best)
    for pname, per in periods.items():
        lines += [f"## {pname} {per[0]}~{per[1]} — {len(config.SYMS)}종목 ({'·'.join(config.SYMS)})", "",
                  "| 전략 | 비용 전 $ | 비용 $ | 순손익 $ | 계약 수 | 일수 | 연환산 샤프(순) | t(일별 순) | 평균 |계약| " + "·".join(config.SYMS) + " |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        res = {}
        for name, prm in S.items():
            r = backtest.run(pm, prm, per, cm)
            res[name] = r
            s = r.summary()
            lines.append(f"| {name} | {s['gross']:,.0f} | {s['cost']:,.0f} | {s['net']:,.0f} | {s['contracts']:,} | "
                         f"{s['days']} | {s['sharpe_net']:+.2f} | {s['t_net']:+.2f} | "
                         f"{' · '.join(f'{x:.2f}' for x in s['avg_abs_pos'])} |")
        gp_d = res["GP (채택)"].daily
        st_d = res[f"정적 부분 매매 w={w_best}"].daily
        diff = (gp_d - st_d.reindex(gp_d.index).fillna(0.0))
        tdiff = diff.mean() / diff.std() * np.sqrt(len(diff)) if diff.std() > 0 else 0.0
        lines += ["", f"GP − 정적 일별 순손익 차이: 평균 ${diff.mean():,.1f}/일, t = {tdiff:+.2f}", ""]
        tr = res["GP (채택)"].trades
        if len(tr):
            lines += ["GP 종목별: " + " · ".join(f"{s} 계약 {int(g.dq.abs().sum())} 비용 ${g.cost.sum():,.0f}"
                                               for s, g in tr.groupby("sym")), ""]
        tag = {"학습": "train", "검증": "val", "시험": "test"}[pname]
        res["GP (채택)"].daily.rename("net").to_csv(out / f"gp_daily_{tag}.csv")
        res["GP (채택)"].trades.to_csv(out / f"gp_trades_{tag}.csv", index=False)

    (out / "summary.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
