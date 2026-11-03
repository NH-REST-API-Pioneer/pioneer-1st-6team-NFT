"""연계 전략 백테스트 — 학습 기간에서 조합을 고르고, 검증 기간은 한 번만 본다 (week02 방식).

사용법 (week03/leadlag/ 에서):
  python3 backtest_link.py                   # 학습 10/13~10/27, 검증 10/28~11/02
  python3 backtest_link.py --slip 0.5        # 슬리피지 민감도

비용 (2026-11-02 모의계좌 확인, 근거 문서 5절):
  수수료 편도 $2 = 0.8틱. 슬리피지 편도 --slip 틱 (기본 1 — 그날 호가 차이 2틱을 그대로 낸다고 봄).
  왕복 비용 = 2 × (0.8 + slip) = 기본 3.6틱.
결과: results/backtest_link/ (조합표·거래 목록·요약), results/charts/backtest_link_equity.png
"""
from __future__ import annotations

import argparse
import csv
import itertools
import json
import random
import statistics as st
from dataclasses import asdict, replace
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402

from strategy_link import Params, Trade, run_day  # noqa: E402

HERE = Path(__file__).parent
TICK_USD = 2.5
FEE_USD = 2.0                     # 편도
STANDARD = Params()               # 출발값
GRID = {
    "window": (1, 2, 3),
    "move_won": (0.3, 0.5, 0.7),
    "rho_max": (0.3, 0.5),
    "hold_min": (10, 20, 30),
    "tp_ticks": (None, 4, 6),
    "sl_ticks": (3, 5, 8),
}
MIN_TRADES = 20                   # 학습 기간 거래가 이보다 적은 조합은 고르지 않음

_have = {f.name for f in font_manager.fontManager.ttflist}
plt.rcParams["font.family"] = [f for f in ("AppleGothic", "Malgun Gothic", "NanumGothic") if f in _have] + ["sans-serif"]
plt.rcParams["axes.unicode_minus"] = False


def load(path: Path) -> dict[tuple[str, str], tuple[float, float]]:
    out = {}
    for r in csv.DictReader(path.open(encoding="utf-8")):
        out[(r["ref_dt"], r["exch_tm"][:4])] = (float(r["open_pric"]), float(r["close_pric"]))
    return out


def day_arrays(krx, mku, d: str, ts: list[str]):
    kc, mo, mc, mt = [], [], [], []
    lk = lm = None
    # 08:25 부터의 MKU 체결로 첫 값 채우기
    for t in [f"{m // 60:02d}{m % 60:02d}" for m in range(8 * 60 + 25, 8 * 60 + 45)]:
        lm = mku.get((d, t), (0, lm))[1]
    for t in ts:
        lk = krx.get((d, t), (0, lk))[1]
        traded = (d, t) in mku
        lm = mku[(d, t)][1] if traded else lm
        kc.append(lk)
        mo.append(mku[(d, t)][0] if traded else None)
        mc.append(lm)
        mt.append(traded)
    return kc, mo, mc, mt


def net_ticks(t: Trade, slip: float) -> float:
    return t.gross_ticks - 2 * (FEE_USD / TICK_USD + slip)


def run(p: Params, days, arrays, ts) -> list[Trade]:
    out = []
    for d in days:
        out += run_day(d, ts, *arrays[d], p)
    return out


def stats(trades: list[Trade], slip: float) -> dict:
    if not trades:
        return {"n": 0, "net_total": 0.0, "net_mean": 0.0, "win": 0.0, "gross_mean": 0.0, "usd": 0.0, "mdd": 0.0}
    net = [net_ticks(t, slip) for t in trades]
    eq, peak, mdd = 0.0, 0.0, 0.0
    for x in net:
        eq += x
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
    return {"n": len(trades), "net_total": round(sum(net), 2), "net_mean": round(st.mean(net), 3),
            "gross_mean": round(st.mean(t.gross_ticks for t in trades), 3),
            "win": round(sum(x > 0 for x in net) / len(net) * 100, 1),
            "usd": round(sum(net) * TICK_USD, 2), "mdd": round(mdd, 2)}


def random_baseline(trades: list[Trade], days, arrays, ts, slip: float, runs: int = 300) -> dict:
    """같은 날·같은 거래 수·같은 보유 시간으로 무작위 시각·무작위 방향 진입했을 때의 순손익."""
    rng = random.Random(7)
    lo, hi = ts.index("0900"), ts.index("1520")
    per_day: dict[str, list[Trade]] = {}
    for t in trades:
        per_day.setdefault(t.date, []).append(t)
    totals = []
    for _ in range(runs):
        total = 0.0
        for d, tl in per_day.items():
            _, mo, mc, _ = arrays[d]
            for t in tl:
                i = rng.randint(lo, hi)
                hold = ts.index(t.exit_time) - ts.index(t.entry_time)
                j = min(i + max(hold, 1), len(ts) - 1)
                side = rng.choice((1, -1))
                gross = side * (mc[j] - mc[i]) / 0.0001
                total += gross - 2 * (FEE_USD / TICK_USD + slip)
        totals.append(total)
    totals.sort()
    return {"mean": round(st.mean(totals), 2), "p95": round(totals[int(runs * 0.95)], 2)}


def write_trades(path: Path, trades: list[Trade], slip: float) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "side", "signal_time", "entry_time", "entry_ref", "exit_time", "exit_ref",
                    "reason", "gross_ticks", "net_ticks", "dk", "rho"])
        for t in trades:
            w.writerow([t.date, "매수" if t.side > 0 else "매도", t.signal_time, t.entry_time, t.entry_ref,
                        t.exit_time, t.exit_ref, t.reason, round(t.gross_ticks, 1), round(net_ticks(t, slip), 2),
                        t.info.get("dk"), t.info.get("rho")])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", default="20261013-20261027")
    ap.add_argument("--test", default="20261028-20261102")
    ap.add_argument("--slip", type=float, default=1.0, help="편도 슬리피지 (틱)")
    args = ap.parse_args()

    krx = load(HERE / "results" / "data" / "bars_1m_A756B000.csv")
    mku = load(HERE / "results" / "data" / "bars_1m_MKUX26.csv")
    ts = [f"{m // 60:02d}{m % 60:02d}" for m in range(8 * 60 + 45, 15 * 60 + 46)]
    split = {}
    for name, rng_ in (("train", args.train), ("test", args.test)):
        a, b = rng_.split("-")
        split[name] = sorted({d for d, _ in krx if a <= d <= b} & {d for d, _ in mku})
    arrays = {d: day_arrays(krx, mku, d, ts) for d in split["train"] + split["test"]}
    cost = 2 * (FEE_USD / TICK_USD + args.slip)
    print(f"학습 {len(split['train'])}일 {split['train'][0]}~{split['train'][-1]} · "
          f"검증 {len(split['test'])}일 {split['test'][0]}~{split['test'][-1]} · 왕복 비용 {cost:.1f}틱")

    out = HERE / "results" / "backtest_link"
    out.mkdir(parents=True, exist_ok=True)

    # 1) 출발값
    std_train = stats(run(STANDARD, split["train"], arrays, ts), args.slip)
    print(f"\n1) 출발값 {asdict(STANDARD)}\n   학습: {std_train}")

    # 2) 학습 기간 조합 탐색
    keys = list(GRID)
    rows_ = []
    for combo in itertools.product(*GRID.values()):
        p = replace(STANDARD, **dict(zip(keys, combo)))
        s = stats(run(p, split["train"], arrays, ts), args.slip)
        rows_.append({**dict(zip(keys, combo)), **s})
    with (out / "grid_train.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows_[0]))
        w.writeheader()
        w.writerows(rows_)
    ok = [r for r in rows_ if r["n"] >= MIN_TRADES]
    pos_share = sum(r["net_total"] > 0 for r in ok) / len(ok) * 100
    ok.sort(key=lambda r: r["net_total"], reverse=True)
    print(f"\n2) 학습 조합 {len(rows_)}개 (거래 {MIN_TRADES}건 이상 {len(ok)}개) · 순손익 플러스 비율 {pos_share:.0f}%")
    print("   상위 5:")
    for r in ok[:5]:
        print("   ", {k: r[k] for k in keys}, f"n={r['n']} 순합 {r['net_total']:+.1f}틱 평균 {r['net_mean']:+.2f} 승률 {r['win']}%")
    best = replace(STANDARD, **{k: ok[0][k] for k in keys})

    # 3) 검증 — 고른 조합과 출발값을 한 번씩
    tr_best = run(best, split["train"], arrays, ts)
    te_best = run(best, split["test"], arrays, ts)
    te_std = run(STANDARD, split["test"], arrays, ts)
    s_tr, s_te, s_std = stats(tr_best, args.slip), stats(te_best, args.slip), stats(te_std, args.slip)
    base = random_baseline(te_best, split["test"], arrays, ts, args.slip) if te_best else {}
    print(f"\n3) 고른 조합 {asdict(best)}")
    print(f"   학습: {s_tr}")
    print(f"   검증: {s_te}")
    print(f"   검증(출발값): {s_std}")
    print(f"   검증 무작위 진입 기준선 (같은 거래 수·보유): 평균 {base.get('mean')}틱, 상위 5% {base.get('p95')}틱")
    reasons = {}
    for t in te_best:
        reasons[t.reason] = reasons.get(t.reason, 0) + 1
    print(f"   검증 청산 사유: {reasons}")

    write_trades(out / "trades_train.csv", tr_best, args.slip)
    write_trades(out / "trades_test.csv", te_best, args.slip)
    (out / "summary.json").write_text(json.dumps({
        "train_days": split["train"], "test_days": split["test"], "slip_ticks": args.slip,
        "fee_usd_per_side": FEE_USD, "roundtrip_cost_ticks": cost,
        "standard": asdict(STANDARD), "standard_train": std_train, "standard_test": s_std,
        "grid_size": len(rows_), "grid_eligible": len(ok), "grid_positive_pct": round(pos_share, 1),
        "chosen": asdict(best), "chosen_train": s_tr, "chosen_test": s_te, "random_baseline_test": base,
        "test_exit_reasons": reasons,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    # 누적 순손익 차트 (학습 → 검증)
    fig, ax = plt.subplots(figsize=(10, 4.5))
    eq, xs = [], 0.0
    for t in tr_best + te_best:
        xs += net_ticks(t, args.slip) * TICK_USD
        eq.append(xs)
    ax.plot(range(len(eq)), eq, color="#2f6db5")
    ax.axvline(len(tr_best) - 0.5, color="#c0392b", ls="--", lw=1)
    ax.text(len(tr_best), min(eq + [0]), " 검증 시작", color="#c0392b", va="bottom", fontsize=9)
    ax.axhline(0, color="#999", lw=0.8)
    ax.set_title(f"KRX→MKU 연계 · 누적 순손익 (1계약, 왕복 비용 {cost:.1f}틱)")
    ax.set_xlabel("거래 순서")
    ax.set_ylabel("누적 순손익 (USD)")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    png = HERE / "results" / "charts" / "backtest_link_equity.png"
    png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(png, dpi=130)
    print(f"\n저장: results/backtest_link/ · {png.relative_to(HERE)}")


if __name__ == "__main__":
    main()
