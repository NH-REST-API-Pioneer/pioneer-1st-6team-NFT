"""시험 주간 요약 표 (보고서 5절용) → results/week_summary_<날짜들>.md

  .venv/bin/python diag/week_summary.py 20261123,20261124,20261125,20261126,20261127

  1. 날별 실제 결과: order(모의계좌) · order_norisk · paper 의 거래·계약·손익·장중 최저, 막힌 분
  2. 위험관리 전·후, 같은 조건(1분 표 재생): 끔 / 한도만 / 손절만 / 2계약만 / 전부
  3. 실제로 걸린 위험관리: 손절·2계약 거래와 같은 시각 order_norisk 거래
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, ".")
import analyze  # noqa: E402

LIVE = analyze.RESULTS / "live"
NAMES = ("order", "order_norisk", "paper")


def krw(x) -> str:
    return f"{x:+,.0f}"


def trades(day: str, name: str) -> pd.DataFrame:
    p = LIVE / f"{day}_{name}_trades.csv"
    if not p.exists():
        return pd.DataFrame(columns=["entry_t", "exit_t", "side", "n", "pnl_krw", "why"])
    t = pd.read_csv(p)
    if "n" not in t:
        t["n"] = 1                                  # 11/23 은 수량 규칙 전 (1계약)
    return t


def blocked(day: str, name: str) -> tuple[int, int]:
    p = LIVE / f"{day}_{name}.jsonl"
    if not p.exists():
        return 0, 0
    m = [json.loads(x) for x in p.read_text().splitlines() if '"minute"' in x]
    return len(m), sum(1 for r in m if r.get("stale"))


def main() -> None:
    days = sys.argv[1].split(",")
    L = [f"# 6주차 시험 주간 요약 — {', '.join(days)}", ""]

    # 1. 실제
    L += ["## 1. 날별 실제 결과", "",
          "| 날 | order (모의계좌) | order_norisk (종이) | paper (종이) | 기록된 분 / 막힌 분 |",
          "| --- | --- | --- | --- | --- |"]
    tot = {n: [0, 0, 0.0] for n in NAMES}
    for d in days:
        cells = []
        for n in NAMES:
            t = trades(d, n)
            s = float(t.pnl_krw.sum()) if len(t) else 0.0
            low = float(t.pnl_krw.cumsum().min()) if len(t) else 0.0
            tot[n][0] += len(t); tot[n][1] += int(t.n.sum()); tot[n][2] += s
            c = f"{len(t)}건" + (f"({int(t.n.sum())}계약)" if int(t.n.sum()) != len(t) else "")
            cells.append(f"{c} {krw(s)}원" + (f", 장중 최저 {krw(low)}" if n == "order" and len(t) else ""))
        mins, st = blocked(d, "order")
        L.append(f"| {d[4:6]}/{d[6:]} | " + " | ".join(cells) + f" | {mins} / {st} |")
    L.append("| 합계 | " + " | ".join(
        f"{tot[n][0]}건" + (f"({tot[n][1]}계약)" if tot[n][1] != tot[n][0] else "") + f" {krw(tot[n][2])}원" for n in NAMES) + " | |")

    # 2. 같은 조건 재생
    R = json.loads((analyze.RULE_DIR / "order.json").read_text())
    r0 = R["risk"]
    V = {"위험관리 끔": analyze.NO_RISK,
         "하루 한도만": {"daily_loss_krw": r0["daily_loss_krw"]},
         "손절만": {"stop_vol_k": r0["stop_vol_k"], "vol_fallback_pt": r0["vol_fallback_pt"]},
         "2계약만": {"size_theta2": r0["size_theta2"], "size_n2": r0["size_n2"]},
         "전부 (order.json)": r0}
    F = {d: analyze.features(p) for d, p in analyze.load(days).items()}
    L += ["", "## 2. 위험관리 전·후, 같은 조건 (1분 표 재생: 중간가 ± 반 스프레드, 수수료율)", "",
          "| 켠 것 | 거래 | 계약 | 합계 | 끔 대비 | 손절 걸림 | 장중 최저 | " + " | ".join(f"{d[6:]}일" for d in days) + " |",
          "| --- | --- | --- | --- | --- | --- | --- | " + " | ".join("---" for _ in days) + " |"]
    base = None
    for k, v in V.items():
        tr = pd.DataFrame([dict(x, day=d) for d, f in F.items() for x in analyze.simulate_rule(f, dict(R, risk=v))])
        s = float(tr.krw.sum())
        base = s if base is None else base
        per = tr.groupby("day").krw.sum().reindex(days).fillna(0)
        low = tr.groupby("day").krw.apply(lambda x: x.cumsum().min()).min()
        L.append(f"| {k} | {len(tr)} | {int(tr.n.sum())} | {krw(s)} | {krw(s - base)} | {(tr.why == 'stop').sum()} | {krw(low)} | "
                 + " | ".join(krw(x) for x in per) + " |")

    # 3. 실제로 걸린 것
    L += ["", "## 3. 실제로 걸린 위험관리 (order) 와 같은 시각 order_norisk", "",
          "| 날 | 진입 | 청산 | 방향 | 계약 | 걸린 것 | order 손익 | order_norisk 같은 진입 |", "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for d in days:
        o, nr = trades(d, "order"), trades(d, "order_norisk")
        for _, x in o.iterrows():
            what = []
            if int(x.n) > 1:
                what.append(f"{int(x.n)}계약")
            if str(x.why).startswith("손절"):
                what.append(str(x.why).split(" (")[0])
            if (pd.Timestamp(x.exit_t) - pd.Timestamp(x.entry_t)) > pd.Timedelta(minutes=int(R["h"]) + 1):
                what.append("청산 지연")
            if not what:
                continue
            m = nr[nr.entry_t == x.entry_t]
            other = f"{m.entry_t.iloc[0][11:16]}~{m.exit_t.iloc[0][11:16]} {krw(m.pnl_krw.iloc[0])}" if len(m) else "없음"
            L.append(f"| {d[6:]} | {x.entry_t[11:16]} | {x.exit_t[11:16]} | {'매수' if x.side > 0 else '매도'} | {int(x.n)} | "
                     f"{', '.join(what)} | {krw(x.pnl_krw)} | {other} |")
    out = analyze.RESULTS / f"week_summary_{'_'.join(days)}.md"
    out.write_text("\n".join(L) + "\n")
    print("\n".join(L))
    print(f"\n→ {out}")


if __name__ == "__main__":
    main()
