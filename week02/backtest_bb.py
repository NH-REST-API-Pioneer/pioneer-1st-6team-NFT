"""볼린저 밴드 + RSI 평균회귀 백테스트 — strategy_bb.py 판정을 지난 1분봉에 그대로 흘립니다.

체결 모형은 VWAP 후보(backtest.py)와 **같은 `min1-v1`** 입니다. 두 후보를 같은 자로 재기 위해
데이터 읽기(load_minutes)·요약(summarize)·출력(fmt)은 backtest.py 의 것을 그대로 씁니다.
다른 점은 익절 목표 하나 — VWAP 대신 **마지막으로 닫힌 판정 봉의 볼린저 중심선** 입니다.

- 판정 봉이 닫히면 **다음 1분봉 시가**에 시장가 진입, 불리한 쪽 슬리피지 1틱.
- 보유 중에는 1분봉마다 손절·익절을 봅니다. 같은 1분봉에서 둘 다 닿으면 손절 (보수적).
- 손절은 손절값(갭이면 시가), 익절은 목표값(갭이면 시가) + 불리한 쪽 슬리피지 1틱.
  시간 청산·장 마감 정리는 다음 1분봉 시가 + 슬리피지 1틱. 수수료 0원. 1계약.

학습/검증은 **한 번에 이어서 돌린 뒤 거래를 날짜로 나눕니다.** 밴드·RSI·ATR 이 날을 넘겨 이어지는 지표라,
검증 구간만 따로 돌리면 준비 봉(기간×2) 동안 판단하지 않고 그 뒤로도 Wilder 평활의 시작값이 달라, 검증 첫날 거래가
달라지기 때문입니다 (채택 값에서 10/21 거래 3건 → 1건).
보유는 날을 넘기지 않으므로(15:30 정리) 거래를 진입일로 나누면 학습 + 검증 = 전체입니다.

사용법 (week02/ 에서):
  python3 backtest_bb.py                                  # 채택 값(Params 기본값), 전체 기간
  python3 backtest_bb.py --standard                       # 정석 출발값, 전체 기간
  python3 backtest_bb.py --from 20261021                  # 기간 지정
  python3 backtest_bb.py --optimize --train-end 20261020 --min-trades 15   # 학습 최적화 → 검증
"""
from __future__ import annotations

import argparse
import itertools
import json
from dataclasses import asdict, replace
from pathlib import Path

from backtest import SLIP_TICKS, fmt, load_minutes, summarize
from strategy import TICK, TICK_VALUE, Minute, minutes_of, pnl_ticks, stop_price
from strategy_bb import BBRSIReversion, Params

HERE = Path(__file__).parent
OUT = HERE / "results" / "backtest_bb"
# 정석 출발값: 5분봉 · BB(20, 2σ) · RSI(14) 30/70 · 복귀 확인 · 손절 ATR×1.5 · 시간 청산 60분
STANDARD = replace(Params(), bar_min=5, band_k=2.0, rsi_buy=30.0, confirm=True, stop_atr=1.5, hold_min=60)


def run(minutes: list[Minute], p: Params, keep_signals: bool = False) -> dict:
    strat = BBRSIReversion(p)
    trades: list[dict] = []
    signals: list[dict] = []
    pos: dict | None = None
    pending_entry: dict | None = None
    pending_exit: str | None = None
    slip = SLIP_TICKS * TICK

    def close(m: Minute, price: float, reason: str) -> None:
        nonlocal pos, pending_exit
        pending_exit = None
        ticks = pnl_ticks(pos["side"], pos["entry"], price)
        pnl = ticks * TICK_VALUE
        trades.append({**pos, "exit_date": m.date, "exit_time": m.time, "exit": round(price, 1),
                       "reason": reason, "ticks": ticks, "pnl": pnl})
        strat.on_exit(pnl, stopped=reason == "손절")
        pos = None

    prev_date = None
    last_minute: Minute | None = None
    for m in minutes:
        if m.date != prev_date:
            if pos is not None and last_minute is not None:
                close(last_minute, last_minute.close, "날 끝 강제 정리")
            strat.end_of_day()
            pending_entry = pending_exit = None
            prev_date = m.date
        tradable = p.session_start <= m.time < p.session_end

        # 1) 대기 중인 진입·청산을 이 분 시가에 체결
        if tradable and pending_entry is not None:
            e = pending_entry
            entry = m.open + slip if e["side"] == "buy" else m.open - slip
            pos = {"side": e["side"], "entry_date": m.date, "entry_time": m.time, "entry": round(entry, 1),
                   "stop": stop_price(e["side"], round(entry, 1), e["stop_ticks"]),
                   "stop_ticks": e["stop_ticks"], "signal_time": e["signal_time"],
                   "target": round(e["target"], 2), "held_min": 0}
            strat.on_entry()
            pending_entry = None
        if tradable and pos is not None and pending_exit is not None:
            px = m.open - slip if pos["side"] == "buy" else m.open + slip
            close(m, px, pending_exit)
            pending_exit = None

        # 2) 보유 중 감시: 장 마감 정리 → 손절 → 익절(중심선)
        target = strat.target                 # 마지막으로 닫힌 판정 봉의 중심선
        if tradable and pos is not None:
            side = pos["side"]
            if m.time >= p.flatten_at:
                close(m, m.open - slip if side == "buy" else m.open + slip, "장 마감 정리")
            elif side == "buy" and m.low <= pos["stop"]:
                close(m, min(m.open, pos["stop"]) - slip, "손절")
            elif side == "sell" and m.high >= pos["stop"]:
                close(m, max(m.open, pos["stop"]) + slip, "손절")
            elif target is not None and side == "buy" and m.high >= target:
                close(m, max(m.open, target) - slip, "익절(중심선)")
            elif target is not None and side == "sell" and m.low <= target:
                close(m, min(m.open, target) + slip, "익절(중심선)")

        # 3) 전략에 이 분을 넣고, 닫힌 판정 봉의 신호를 받음
        for s in strat.on_minute(m):
            if keep_signals and (s.side or "신호" in s.reason):
                b = s.bar
                signals.append({"date": b.date, "bar": b.start, "close": b.close,
                                "middle": round(b.middle, 2) if b.middle else None,
                                "upper": round(b.upper, 2) if b.upper else None,
                                "lower": round(b.lower, 2) if b.lower else None,
                                "rsi": round(b.rsi, 1) if b.rsi is not None else None,
                                "atr": round(b.atr, 3) if b.atr else None,
                                "side": s.side, "reason": s.reason})
            if s.side and pos is None and pending_entry is None:
                pending_entry = {"side": s.side, "stop_ticks": s.stop_ticks, "signal_time": s.bar.end,
                                 "target": s.bar.middle}

        # 4) 시간 청산
        if pos is not None and tradable:
            pos["held_min"] = minutes_of(m.time) - minutes_of(pos["entry_time"]) + 1
            if pos["held_min"] >= p.hold_min and pending_exit is None:
                pending_exit = "시간 청산"
        last_minute = m

    if pos is not None and last_minute is not None:
        close(last_minute, last_minute.close, "자료 끝")
    return {"params": asdict(p), "summary": summarize(trades), "trades": trades,
            "signals": signals if keep_signals else None}


# 후보는 모두 앱의 기준 봉 목록(1·5·15·30·60분봉)에 있는 값 — 앱에서 그대로 재현할 수 있게.
GRID = {
    "bar_min": [1, 5, 15],
    "band_k": [1.5, 2.0, 2.5],
    "rsi_buy": [30.0, 35.0, 40.0, 100.0],   # 100 = RSI 필터 끔 (볼린저만)
    "confirm": [True, False],
    "stop_atr": [1.5, 2.0, 3.0],
    "hold_min": [60, 120],
}


def split(res: dict, train_end: str) -> tuple[dict, dict, list[dict], list[dict]]:
    """전체 기간 결과를 진입일로 학습/검증으로 나눕니다 → (학습 요약, 검증 요약, 검증 거래, 검증 신호)."""
    tr = [t for t in res["trades"] if t["entry_date"] <= train_end]
    te = [t for t in res["trades"] if t["entry_date"] > train_end]
    sig = [x for x in (res["signals"] or []) if x["date"] > train_end]
    return summarize(tr), summarize(te), te, sig


def optimize(minutes: list[Minute], train_end: str, min_trades: int) -> list[dict]:
    rows = []
    keys = list(GRID)
    for values in itertools.product(*GRID.values()):
        p = replace(Params(), **dict(zip(keys, values)))
        s = split(run(minutes, p), train_end)[0]
        rows.append({**dict(zip(keys, values)), **{k: s[k] for k in
                     ("trades", "win_rate", "net_pnl", "profit_factor", "max_drawdown")}})
    # 이웃 평균: 값 하나만 한 칸 바꾼 조합들의 순손익 평균 (한 점만 튀는 값을 거르기 위해)
    index = {tuple(r[k] for k in keys): r for r in rows}
    for r in rows:
        neigh = []
        for i, k in enumerate(keys):
            vals = GRID[k]
            at = vals.index(r[k])
            for j in (at - 1, at + 1):
                if 0 <= j < len(vals):
                    key = list(tuple(r[k2] for k2 in keys))
                    key[i] = vals[j]
                    neigh.append(index[tuple(key)]["net_pnl"])
        r["neighbor_avg_pnl"] = round(sum(neigh) / len(neigh)) if neigh else None
        r["eligible"] = r["trades"] >= min_trades
    return rows


def label(r: dict) -> str:
    rsi = "RSI 끔" if r["rsi_buy"] >= 100 else f"RSI {r['rsi_buy']:.0f}/{100 - r['rsi_buy']:.0f}"
    return (f"{r['bar_min']}분봉 · {r['band_k']}σ · {rsi} · {'복귀 확인' if r['confirm'] else '즉시'} · "
            f"ATR×{r['stop_atr']} · {r['hold_min']}분")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", default="00000000")
    ap.add_argument("--to", dest="end", default="99999999")
    ap.add_argument("--optimize", action="store_true")
    ap.add_argument("--standard", action="store_true", help="정석 출발값으로 실행")
    ap.add_argument("--train-end", default="20261020", help="학습 기간 마지막 날 (그 다음 날부터 검증)")
    ap.add_argument("--min-trades", type=int, default=15, help="학습 기간 최소 거래 수")
    args = ap.parse_args()

    minutes = [m for m in load_minutes() if args.start <= m.date <= args.end]
    days = sorted({m.date for m in minutes})
    OUT.mkdir(parents=True, exist_ok=True)

    if not args.optimize:
        p, name, file = (STANDARD, "정석 값", "standard_all.json") if args.standard else (Params(), "채택 값", "adopted_all.json")
        res = run(minutes, p, keep_signals=True)
        print(f"기간 {days[0]}~{days[-1]} ({len(days)}일) · {name} [{label(asdict(p))}]\n  {fmt(res['summary'])}")
        (OUT / file).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        return

    tr_days = [d for d in days if d <= args.train_end]
    te_days = [d for d in days if d > args.train_end]
    print(f"학습 {tr_days[0]}~{tr_days[-1]} ({len(tr_days)}일) · 검증 {te_days[0]}~{te_days[-1]} ({len(te_days)}일)")

    base = STANDARD
    base_tr, base_te, _, _ = split(run(minutes, base, keep_signals=True), args.train_end)
    print(f"[정석] {label(asdict(base))}\n  학습: {fmt(base_tr)}\n  검증: {fmt(base_te)}")

    rows = optimize(minutes, args.train_end, args.min_trades)
    ok = [r for r in rows if r["eligible"]]
    by_net = sorted(ok, key=lambda r: r["net_pnl"], reverse=True)
    by_neigh = sorted(ok, key=lambda r: (r["neighbor_avg_pnl"], r["net_pnl"]), reverse=True)
    print(f"\n조합 {len(rows)}개 중 거래 {args.min_trades}건 이상 {len(ok)}개 · 학습 순손익 양수 {sum(r['net_pnl'] > 0 for r in ok)}개")
    print("학습 순손익 상위 5:")
    for r in by_net[:5]:
        print(f"   {label(r)} → 학습 {r['net_pnl']:+,} ({r['trades']}건) · 이웃 {r['neighbor_avg_pnl']:+,} · MDD {r['max_drawdown']:,.0f}")
    print("이웃 평균 상위 5 (튀는 값 거르기):")
    for r in by_neigh[:5]:
        print(f"   {label(r)} → 학습 {r['net_pnl']:+,} ({r['trades']}건) · 이웃 {r['neighbor_avg_pnl']:+,} · MDD {r['max_drawdown']:,.0f}")

    keys = list(GRID)
    adopted = next(r for r in rows if all(r[k] == getattr(Params(), k) for k in keys))
    rsi_off = next(r for r in rows if all(r[k] == (100.0 if k == "rsi_buy" else getattr(Params(), k)) for k in keys))
    picks = {"학습1위": by_net[0], "이웃평균1위": by_neigh[0], "채택": adopted, "채택_RSI끔": rsi_off}
    results = {"train_days": tr_days, "test_days": te_days, "grid": GRID, "min_trades": args.min_trades,
               "split": "전체 기간을 이어서 돌린 뒤 진입일로 학습/검증을 나눔",
               "baseline": {"params": asdict(base), "train": base_tr, "test": base_te}, "picked": {}}
    print()
    for name, r in picks.items():
        p = replace(base, **{k: r[k] for k in keys})
        _, te, te_trades, te_signals = split(run(minutes, p, keep_signals=True), args.train_end)
        print(f"[{name}] {label(r)}\n  학습: 순손익 {r['net_pnl']:+,}원 거래 {r['trades']} · 이웃 {r['neighbor_avg_pnl']:+,}\n  검증: {fmt(te)}")
        results["picked"][name] = {"params": asdict(p), "train": r, "test": te, "test_trades": te_trades, "test_signals": te_signals}
    (OUT / "optimize_grid.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / "optimize_result.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n저장: {OUT.relative_to(HERE)}/optimize_grid.json, optimize_result.json")


if __name__ == "__main__":
    main()
