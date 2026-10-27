"""VWAP 밴드 평균회귀 백테스트 — strategy.py 판정을 지난 1분봉에 그대로 흘립니다.

체결 모형 (`min1-v1`):
- 판정 봉이 닫히면 **다음 1분봉 시가**에 시장가 진입, 불리한 쪽 슬리피지 1틱.
- 보유 중에는 1분봉마다 손절·익절을 봅니다. 익절 목표는 **직전 1분봉까지의 VWAP**.
  같은 1분봉에서 손절·익절이 함께 닿으면 손절로 셉니다 (보수적).
- 손절은 손절값(갭이면 시가)에, 익절은 목표값(갭이면 시가)에 닿은 뒤 시장가로 나간다고 보고
  불리한 쪽 슬리피지 1틱. 시간 청산·장 마감 정리는 다음 1분봉 시가 + 슬리피지 1틱.
- 수수료 0원 (모의계좌 수수료 확인 전). 1계약.

사용법 (week02/ 에서):
  python3 backtest.py                                  # 채택 값(Params 기본값), 전체 기간
  python3 backtest.py --standard                       # 정석 출발값, 전체 기간
  python3 backtest.py --from 20261021                  # 기간 지정
  python3 backtest.py --optimize --train-end 20261020  # 학습 기간 최적화 → 검증 기간 평가
"""
from __future__ import annotations

import argparse
import csv
import itertools
import json
from dataclasses import asdict, replace
from pathlib import Path

from strategy import (TICK, TICK_VALUE, Minute, Params, VWAPReversion, minutes_of, pnl_ticks, stop_price)

HERE = Path(__file__).parent
DATA = HERE / "results" / "data" / "bars_1m_A756B000.csv"
OUT = HERE / "results" / "backtest"
SLIP_TICKS = 1
MIN_BARS_PER_DAY = 250   # 이보다 봉이 적은 날(거래가 드문 원월물 시절)은 뺍니다
# 정석 출발값: 5분봉 · 2σ · 복귀 확인 · 손절 ATR×1.5 · 시간 청산 60분 (나머지는 Params 와 같음)
STANDARD = replace(Params(), bar_min=5, band_k=2.0, confirm=True, stop_atr=1.5, hold_min=60)


def load_minutes(path: Path = DATA) -> list[Minute]:
    with path.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    per_day: dict[str, int] = {}
    for r in rows:
        per_day[r["ref_dt"]] = per_day.get(r["ref_dt"], 0) + 1
    return [Minute(r["ref_dt"], r["exch_tm"], float(r["open_pric"]), float(r["high_pric"]),
                   float(r["low_pric"]), float(r["close_pric"]), float(r["cum_trd_qty"] or 0))
            for r in rows if per_day[r["ref_dt"]] >= MIN_BARS_PER_DAY]


def run(minutes: list[Minute], p: Params, keep_signals: bool = False) -> dict:
    strat = VWAPReversion(p)
    trades: list[dict] = []
    signals: list[dict] = []
    pos: dict | None = None          # 보유 중인 거래
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
            if pos is not None and last_minute is not None:     # 날 넘김 방지 (정상이면 일어나지 않음)
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
                   "stop_ticks": e["stop_ticks"], "signal_time": e["signal_time"], "held_min": 0}
            strat.on_entry()
            pending_entry = None
        if tradable and pos is not None and pending_exit is not None:
            px = m.open - slip if pos["side"] == "buy" else m.open + slip
            close(m, px, pending_exit)
            pending_exit = None

        # 2) 보유 중 감시: 장 마감 정리 → 손절 → 익절
        target = strat.current_vwap          # 이 분을 넣기 전 = 직전 분까지의 VWAP
        if tradable and pos is not None:
            side = pos["side"]
            if m.time >= p.flatten_at:
                close(m, m.open - slip if side == "buy" else m.open + slip, "장 마감 정리")
            elif side == "buy" and m.low <= pos["stop"]:
                close(m, min(m.open, pos["stop"]) - slip, "손절")
            elif side == "sell" and m.high >= pos["stop"]:
                close(m, max(m.open, pos["stop"]) + slip, "손절")
            elif target is not None and side == "buy" and m.high >= target:
                close(m, max(m.open, target) - slip, "익절(VWAP)")
            elif target is not None and side == "sell" and m.low <= target:
                close(m, min(m.open, target) + slip, "익절(VWAP)")

        # 3) 전략에 이 분을 넣고, 닫힌 판정 봉의 신호를 받음
        for s in strat.on_minute(m):
            if keep_signals and (s.side or "신호" in s.reason):
                signals.append({"date": s.bar.date, "bar": s.bar.start, "close": s.bar.close,
                                "vwap": round(s.bar.vwap, 2) if s.bar.vwap else None,
                                "upper": round(s.bar.upper, 2) if s.bar.upper else None,
                                "lower": round(s.bar.lower, 2) if s.bar.lower else None,
                                "atr": round(s.bar.atr, 3) if s.bar.atr else None,
                                "side": s.side, "reason": s.reason})
            if s.side and pos is None and pending_entry is None:
                pending_entry = {"side": s.side, "stop_ticks": s.stop_ticks, "signal_time": s.bar.end}

        # 4) 시간 청산
        if pos is not None and tradable:
            # 빈 분(거래 없음)이 있어도 시계 기준으로 셉니다
            pos["held_min"] = minutes_of(m.time) - minutes_of(pos["entry_time"]) + 1
            if pos["held_min"] >= p.hold_min and pending_exit is None:
                pending_exit = "시간 청산"
        last_minute = m

    if pos is not None and last_minute is not None:
        close(last_minute, last_minute.close, "자료 끝")
    return {"params": asdict(p), "summary": summarize(trades), "trades": trades,
            "signals": signals if keep_signals else None}


def summarize(trades: list[dict]) -> dict:
    n = len(trades)
    pnl = [t["pnl"] for t in trades]
    wins = [x for x in pnl if x > 0]
    losses = [x for x in pnl if x <= 0]
    equity = peak = mdd = 0.0
    for x in pnl:
        equity += x
        peak = max(peak, equity)
        mdd = max(mdd, peak - equity)
    by_reason: dict[str, int] = {}
    by_day: dict[str, float] = {}
    for t in trades:
        by_reason[t["reason"]] = by_reason.get(t["reason"], 0) + 1
        by_day[t["exit_date"]] = by_day.get(t["exit_date"], 0) + t["pnl"]
    gp, gl = sum(wins), -sum(losses)
    return {
        "trades": n, "wins": len(wins), "win_rate": round(len(wins) / n, 3) if n else None,
        "net_pnl": sum(pnl), "profit_factor": round(gp / gl, 2) if gl > 0 else None,
        "avg_win": round(gp / len(wins)) if wins else None,
        "avg_loss": round(-gl / len(losses)) if losses else None,
        "max_drawdown": mdd, "by_reason": by_reason, "by_day": by_day,
    }


GRID = {
    "bar_min": [3, 5, 10],
    "band_k": [1.0, 1.5, 2.0, 2.5],
    "confirm": [True, False],
    "stop_atr": [1.0, 1.5, 2.0, 3.0],
    "hold_min": [30, 60, 120],
}


def optimize(train: list[Minute], min_trades: int) -> list[dict]:
    rows = []
    keys = list(GRID)
    for values in itertools.product(*GRID.values()):
        p = replace(Params(), **dict(zip(keys, values)))
        s = run(train, p)["summary"]
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


def fmt(s: dict) -> str:
    wr = f"{s['win_rate'] * 100:.0f}%" if s["win_rate"] is not None else "-"
    return (f"거래 {s['trades']} · 승률 {wr} · 순손익 {s['net_pnl']:+,}원 · PF {s['profit_factor']} · "
            f"평균 이익/손실 {s['avg_win']}/{s['avg_loss']} · 최대낙폭 {s['max_drawdown']:,.0f}원 · {s['by_reason']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", default="00000000")
    ap.add_argument("--to", dest="end", default="99999999")
    ap.add_argument("--optimize", action="store_true")
    ap.add_argument("--standard", action="store_true", help="정석 출발값으로 실행")
    ap.add_argument("--train-end", default="20261020", help="학습 기간 마지막 날 (그 다음 날부터 검증)")
    ap.add_argument("--min-trades", type=int, default=20, help="학습 기간 최소 거래 수")
    args = ap.parse_args()

    minutes = [m for m in load_minutes() if args.start <= m.date <= args.end]
    days = sorted({m.date for m in minutes})
    OUT.mkdir(parents=True, exist_ok=True)

    if not args.optimize:
        p, name, file = (STANDARD, "정석 값", "standard_all.json") if args.standard else (Params(), "채택 값", "adopted_all.json")
        res = run(minutes, p, keep_signals=True)
        print(f"기간 {days[0]}~{days[-1]} ({len(days)}일) · {name}\n  {fmt(res['summary'])}")
        (OUT / file).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        return

    train = [m for m in minutes if m.date <= args.train_end]
    test = [m for m in minutes if m.date > args.train_end]
    tr_days = sorted({m.date for m in train})
    te_days = sorted({m.date for m in test})
    print(f"학습 {tr_days[0]}~{tr_days[-1]} ({len(tr_days)}일) · 검증 {te_days[0]}~{te_days[-1]} ({len(te_days)}일)")

    base = STANDARD
    base_tr, base_te = run(train, base), run(test, base, keep_signals=True)
    print(f"[정석] 학습: {fmt(base_tr['summary'])}\n[정석] 검증: {fmt(base_te['summary'])}")

    rows = optimize(train, args.min_trades)
    ok = [r for r in rows if r["eligible"]]
    by_net = sorted(ok, key=lambda r: r["net_pnl"], reverse=True)
    by_neigh = sorted(ok, key=lambda r: (r["neighbor_avg_pnl"], r["net_pnl"]), reverse=True)
    print(f"\n조합 {len(rows)}개 중 거래 {args.min_trades}건 이상 {len(ok)}개 · 학습 순손익 양수 {sum(r['net_pnl'] > 0 for r in ok)}개")
    print("학습 순손익 상위 5:")
    for r in by_net[:5]:
        print("  ", r)
    print("이웃 평균 상위 5 (튀는 값 거르기):")
    for r in by_neigh[:5]:
        print("  ", r)

    keys = list(GRID)
    adopted = next(r for r in rows if all(r[k] == getattr(Params(), k) for k in keys))
    picks = {"학습1위": by_net[0], "이웃평균1위": by_neigh[0], "채택": adopted}
    results = {"train_days": tr_days, "test_days": te_days, "grid": GRID,
               "baseline": {"train": base_tr["summary"], "test": base_te["summary"]}, "picked": {}}
    print()
    for name, r in picks.items():
        p = replace(base, **{k: r[k] for k in keys})
        te = run(test, p, keep_signals=True)
        print(f"[{name}] {{{', '.join(f'{k}={r[k]}' for k in keys)}}}\n  학습: 순손익 {r['net_pnl']:+,}원 거래 {r['trades']}\n  검증: {fmt(te['summary'])}")
        results["picked"][name] = {"params": asdict(p), "train": r, "test": te["summary"], "test_trades": te["trades"]}
    (OUT / "optimize_grid.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / "optimize_result.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n저장: {OUT.relative_to(HERE)}/optimize_grid.json, optimize_result.json")


if __name__ == "__main__":
    main()
