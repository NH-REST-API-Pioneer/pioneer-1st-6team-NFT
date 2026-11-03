"""보유 시간별 연계 전략 백테스트 — 보유를 고정하고 나머지만 학습 기간에서 골라 검증 기간에 한 번 적용.

사용법 (week03/leadlag/ 에서):
  python3 hold_backtest.py               # 슬리피지 편도 0·1틱 둘 다

체결·비용·기간은 backtest_link.py 와 같습니다 (다음 분 시가 체결, 수수료 편도 $2, 15:40 강제 청산).
보유가 길면 늦은 신호는 15:40 에 잘리므로 「실제 평균 보유」도 함께 봅니다.
"""
from __future__ import annotations

import itertools
import json
import statistics as st
from dataclasses import asdict, replace

import backtest_link as B

HOLDS = (10, 20, 30, 60, 90, 120, 180)
SLIPS = (0.0, 1.0)


def main() -> None:
    krx = B.load(B.HERE / "results" / "data" / "bars_1m_A756B000.csv")
    mku = B.load(B.HERE / "results" / "data" / "bars_1m_MKUX26.csv")
    ts = [f"{m // 60:02d}{m % 60:02d}" for m in range(8 * 60 + 45, 15 * 60 + 46)]
    train = sorted({d for d, _ in krx if "20261013" <= d <= "20261027"} & {d for d, _ in mku})
    test = sorted({d for d, _ in krx if "20261028" <= d <= "20261102"} & {d for d, _ in mku})
    arrays = {d: B.day_arrays(krx, mku, d, ts) for d in train + test}
    grid = {k: v for k, v in B.GRID.items() if k != "hold_min"}
    keys = list(grid)
    out = {}
    for slip in SLIPS:
        print(f"\n슬리피지 편도 {slip}틱 · 왕복 비용 {2 * (B.FEE_USD / B.TICK_USD + slip):.1f}틱")
        print("  보유 | 출발값 학습 / 검증 ($)      | 고른 조합 학습 / 검증 ($)  검증거래 평균보유 | 학습 플러스 조합")
        for h in HOLDS:
            std = replace(B.STANDARD, hold_min=h)
            s_tr = B.stats(B.run(std, train, arrays, ts), slip)
            s_te = B.stats(B.run(std, test, arrays, ts), slip)
            rows = []
            for combo in itertools.product(*grid.values()):
                p = replace(std, **dict(zip(keys, combo)))
                rows.append((B.stats(B.run(p, train, arrays, ts), slip), p))
            ok = [r for r in rows if r[0]["n"] >= B.MIN_TRADES]
            pos = sum(r[0]["net_total"] > 0 for r in ok) / len(ok) * 100 if ok else 0
            best_s, best_p = max(ok, key=lambda r: r[0]["net_total"])
            te_trades = B.run(best_p, test, arrays, ts)
            b_te = B.stats(te_trades, slip)
            held = st.mean(ts.index(t.exit_time) - ts.index(t.entry_time) for t in te_trades) if te_trades else 0
            out[f"slip{slip}_hold{h}"] = {"standard_train": s_tr, "standard_test": s_te, "chosen": asdict(best_p),
                                          "chosen_train": best_s, "chosen_test": b_te, "positive_pct": round(pos, 1)}
            print(f"  {h:>4}분 | {s_tr['usd']:+7.0f} / {s_te['usd']:+6.0f} (n {s_te['n']:>2})  | "
                  f"{best_s['usd']:+7.0f} / {b_te['usd']:+6.0f} (n {b_te['n']:>2}) {held:5.0f}분 | {pos:4.0f}%")
    path = B.HERE / "results" / "backtest_link" / "hold_backtest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n저장: {path.relative_to(B.HERE)}")


if __name__ == "__main__":
    main()
