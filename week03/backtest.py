"""두 후보 백테스트 — strategy_bb.py(후보 A)·strategy_orb.py(후보 B) 판정을 MKU 1분봉에 그대로 흘립니다.

두 후보를 같은 자로 재려고 체결 모형·요약·학습/검증 나누기를 이 파일 하나가 같이 씁니다.

## 체결 모형 (`min1-mku-v1`, 2주차 `min1-v2` 를 MKU 에 맞춤)

- 신호가 나면 **다음 1분봉 시가**에 시장가 진입, 불리한 쪽 슬리피지 1틱 (0.0001).
  MKU 는 거래가 없던 분은 봉이 없어서 「다음 1분봉」이 몇 분 뒤일 수 있습니다. 시장가 주문이
  다음 체결에 붙는 것과 같다고 봅니다. 그 봉이 23:50 이후면 진입하지 않습니다.
- 보유 중에는 1분봉마다 손절·익절을 봅니다. 같은 1분봉에서 둘 다 닿으면 손절로 셉니다 (보수적).
  손절은 손절값(갭이면 시가), 익절은 목표값(갭이면 시가) + 불리한 쪽 슬리피지 1틱.
  - 후보 A 익절 목표 = 마지막으로 닫힌 판정 봉의 중심선을 불리한 쪽 호가로 맞춘 값.
  - 후보 B 익절 목표 = 진입가 ± 범위 폭 × target_r (틱). 손절은 **범위 반대편 끝 가격** 그대로
    (후보 A 는 2주차처럼 진입가에서 stop_ticks 만큼).
- 시간 청산(후보 A: 진입 + hold_min, 후보 B: 그것과 다음 개장 시점 중 먼저)과 23:50 정리는
  그 시각 이후 첫 1분봉 시가 + 슬리피지 1틱. 23:50 뒤에 봉이 없으면 다음 날 첫 봉(자정 뒤)에서 정리합니다.
- 수수료는 0 이 기본입니다 (해외 모의계좌 수수료 확인 전). --fee 로 왕복 수수료(US$)를 줄 수 있습니다. 1계약.

학습/검증은 **한 번에 이어서 돌린 뒤 거래를 진입일로 나눕니다** (2주차 backtest_bb.py 와 같은 이유 —
후보 A 의 밴드·RSI·ATR 이 날을 넘겨 이어짐). 보유는 날을 넘기지 않아 학습 + 검증 = 전체입니다.

사용법 (week03/ 에서):
  python3 backtest.py                          # 두 후보 출발값(Params 기본값), 전체 기간
  python3 backtest.py --strategy orb --ranges  # 후보 B 개장 범위 폭 분포
  python3 backtest.py --optimize               # 두 후보 학습 최적화 → 검증
  python3 backtest.py --optimize --strategy bb --train-end 20261026
"""
from __future__ import annotations

import argparse
import csv
import itertools
import json
import statistics
from dataclasses import asdict, replace
from pathlib import Path

import strategy_bb
import strategy_orb
from common import (FLATTEN_AT, PRICE_DECIMALS, TICK, TICK_VALUE, Minute, hhmmss_of, in_trading_hours,
                    minutes_of, pnl_ticks, stop_price, take_profit_price, target_price, ticks_between)

HERE = Path(__file__).parent
DATA = HERE / "results" / "data" / "bars_1m_MKUX26.csv"
SLIP_TICKS = 1
# 11월물이 최근월물로 거래가 붙은 날부터 (매매 시간 안 1분봉이 하루 350개 이상), 다 찬 마지막 날까지
DATA_FROM, DATA_TO = "20261012", "20261102"
TRAIN_END = "20261026"   # 학습 10/12~10/26 (11일) · 검증 10/27~11/02 (5일)

STRATEGIES = {
    "bb": {"name": "후보 A 볼린저+RSI", "module": strategy_bb, "out": HERE / "results" / "backtest_bb",
           # 2주차 정석 출발값과 같음: 5분봉 · BB(20, 2σ) · RSI 30/70 · 복귀 확인 · ATR×1.5 · 60분
           "standard": replace(strategy_bb.Params(), bar_min=5, band_k=2.0, rsi_buy=30.0, confirm=True,
                               stop_atr=1.5, hold_min=60),
           "grid": {"bar_min": [1, 5, 15], "band_k": [1.5, 2.0, 2.5], "rsi_buy": [30.0, 35.0, 40.0, 100.0],
                    "confirm": [True, False], "stop_atr": [1.5, 2.0, 3.0], "hold_min": [60, 120]},
           "min_trades": 15},
    "orb": {"name": "후보 B 세션 개장 돌파", "module": strategy_orb, "out": HERE / "results" / "backtest_orb",
            "standard": strategy_orb.Params(),
            "grid": {"range_min": [10, 15, 30], "entry_min": [30, 60, 120], "buffer_ticks": [0, 2],
                     "target_r": [0.5, 1.0, 1.5, 2.0], "hold_min": [60, 120, 240]},
            "min_trades": 10},
}

# 진입 시각으로 나누는 시간대 (한국 시간)
BLOCKS = (("개장 전", "082500", "090000"), ("서울 장", "090000", "153000"), ("아시아 오후", "153000", "170000"),
          ("런던", "170000", "223000"), ("뉴욕", "223000", "240000"))


def block_of(hhmmss: str) -> str:
    return next((name for name, a, b in BLOCKS if a <= hhmmss < b), "기타")


def load_minutes(path: Path = DATA, start: str = DATA_FROM, end: str = DATA_TO) -> list[Minute]:
    with path.open(encoding="utf-8") as f:
        return [Minute(r["ref_dt"], r["exch_tm"], float(r["open_pric"]), float(r["high_pric"]),
                       float(r["low_pric"]), float(r["close_pric"]), float(r["cum_trd_qty"] or 0))
                for r in csv.DictReader(f) if start <= r["ref_dt"] <= end]


def run(minutes: list[Minute], kind: str, p, keep_signals: bool = False, fee: float = 0.0) -> dict:
    strat = strategy_bb.BBRSIReversion(p) if kind == "bb" else strategy_orb.SessionBreakout(p)
    trades: list[dict] = []
    signals: list[dict] = []
    pos: dict | None = None
    pending: dict | None = None
    slip = SLIP_TICKS * TICK

    def fill(side_of_order: str, price: float) -> float:
        """시장가 체결가 — 사는 주문은 1틱 비싸게, 파는 주문은 1틱 싸게."""
        return round(price + slip if side_of_order == "buy" else price - slip, PRICE_DECIMALS)

    def close(m: Minute, price: float, reason: str) -> None:
        nonlocal pos
        exit_ = fill("sell" if pos["side"] == "buy" else "buy", price)
        ticks = pnl_ticks(pos["side"], pos["entry"], exit_)
        pnl = ticks * TICK_VALUE - fee
        trades.append({**pos, "exit_date": m.date, "exit_time": m.time, "exit": exit_, "reason": reason,
                       "ticks": ticks, "fee": fee, "pnl": pnl})
        strat.on_exit(pnl, stopped=reason == "손절")
        pos = None

    prev_date = None
    for m in minutes:
        if m.date != prev_date:
            if kind == "bb":
                strat.end_of_day()      # 덜 닫힌 판정 봉 정리 (진입 시간 밖이라 신호는 버림)
            pending = None
            prev_date = m.date

        # 1) 보유 중: 23:50 정리 · 시간 청산 — 그 시각 이후 첫 봉 시가 (매매 시간 밖 봉이어도 체결로 봄)
        if pos is not None:
            if m.date != pos["entry_date"] or m.time >= p.flatten_at:
                close(m, m.open, "마감 정리")
            elif m.time >= pos["deadline"]:
                close(m, m.open, pos["deadline_reason"])

        tradable = in_trading_hours(m.time)
        # 2) 대기 중인 진입을 이 분 시가에 체결
        if pending is not None and pos is None and tradable:
            e, pending = pending, None
            if m.time < p.flatten_at:
                entry = fill(e["side"], m.open)
                hold_end = hhmmss_of(minutes_of(m.time) + p.hold_min)
                deadline, why = hold_end, "시간 청산"
                if e.get("exit_by") and e["exit_by"] < hold_end:
                    deadline, why = e["exit_by"], "다음 개장 전 청산"
                stop = e["stop_level"] if e.get("stop_level") is not None else stop_price(e["side"], entry, e["stop_ticks"])
                pos = {"side": e["side"], "window": e.get("window"), "signal_time": e["signal_time"],
                       "entry_date": m.date, "entry_time": m.time, "entry": entry,
                       "stop": stop, "stop_ticks": abs(ticks_between(entry, stop)),
                       "target": (target_price(e["side"], entry, e["target_ticks"])
                                  if e.get("target_ticks") else None),
                       "deadline": deadline, "deadline_reason": why, "block": block_of(m.time)}
                if kind == "bb":
                    strat.on_entry()
                else:
                    strat.on_entry(e["window"])

        # 3) 보유 중 감시: 손절 → 익절 (진입한 봉도 봄)
        if pos is not None and tradable:
            side = pos["side"]
            if kind == "bb":
                tp = take_profit_price(side, strat.target) if strat.target is not None else None
            else:
                tp = pos["target"]
            if side == "buy" and m.low <= pos["stop"]:
                close(m, min(m.open, pos["stop"]), "손절")
            elif side == "sell" and m.high >= pos["stop"]:
                close(m, max(m.open, pos["stop"]), "손절")
            elif tp is not None and side == "buy" and m.high >= tp:
                close(m, max(m.open, tp), "익절")
            elif tp is not None and side == "sell" and m.low <= tp:
                close(m, min(m.open, tp), "익절")

        # 4) 전략에 이 분을 넣고 신호를 받음
        for s in strat.on_minute(m):
            if kind == "bb":
                t, extra = s.bar.end, {"bar": s.bar.start, "close": s.bar.close,
                                       "rsi": round(s.bar.rsi, 1) if s.bar.rsi is not None else None}
                stop_level = None
            else:
                t, extra = s.minute.time, {"window": s.window, "close": s.minute.close,
                                           "range_high": s.range_high, "range_low": s.range_low}
                stop_level = s.range_low if s.side == "buy" else s.range_high
            if keep_signals and (s.side or "신호" in s.reason):
                signals.append({"date": m.date, "time": t, **extra, "side": s.side, "reason": s.reason})
            if s.side and pos is None and pending is None:
                pending = {"side": s.side, "stop_ticks": s.stop_ticks, "signal_time": t,
                           "target_ticks": getattr(s, "target_ticks", None), "exit_by": getattr(s, "exit_by", None),
                           "window": getattr(s, "window", None),
                           "stop_level": stop_level}

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
    by = lambda key: {k: {"trades": sum(1 for t in trades if t[key] == k),  # noqa: E731
                          "pnl": sum(t["pnl"] for t in trades if t[key] == k)}
                      for k in dict.fromkeys(t[key] for t in trades)}
    by_reason: dict[str, int] = {}
    by_day: dict[str, float] = {}
    for t in trades:
        by_reason[t["reason"]] = by_reason.get(t["reason"], 0) + 1
        by_day[t["entry_date"]] = by_day.get(t["entry_date"], 0) + t["pnl"]
    gp, gl = sum(wins), -sum(losses)
    out = {
        "trades": n, "wins": len(wins), "win_rate": round(len(wins) / n, 3) if n else None,
        "net_pnl": sum(pnl), "profit_factor": round(gp / gl, 2) if gl > 0 else None,
        "avg_win": round(gp / len(wins), 1) if wins else None,
        "avg_loss": round(-gl / len(losses), 1) if losses else None,
        "max_drawdown": mdd, "fee_total": sum(t["fee"] for t in trades),
        "by_reason": by_reason, "by_block": by("block"), "by_day": by_day,
    }
    if trades and trades[0].get("window"):
        out["by_window"] = by("window")
    return out


def split(res: dict, train_end: str) -> tuple[dict, dict, list[dict], list[dict]]:
    """전체 결과를 진입일로 학습/검증으로 나눕니다 → (학습 요약, 검증 요약, 검증 거래, 검증 신호)."""
    tr = [t for t in res["trades"] if t["entry_date"] <= train_end]
    te = [t for t in res["trades"] if t["entry_date"] > train_end]
    sig = [x for x in (res["signals"] or []) if x["date"] > train_end]
    return summarize(tr), summarize(te), te, sig


def optimize(minutes: list[Minute], kind: str, train_end: str, min_trades: int, fee: float) -> list[dict]:
    spec = STRATEGIES[kind]
    grid, keys, rows = spec["grid"], list(spec["grid"]), []
    for values in itertools.product(*grid.values()):
        p = replace(spec["standard"], **dict(zip(keys, values)))
        s = split(run(minutes, kind, p, fee=fee), train_end)[0]
        rows.append({**dict(zip(keys, values)), **{k: s[k] for k in
                     ("trades", "win_rate", "net_pnl", "profit_factor", "max_drawdown")}})
    # 이웃 평균: 값 하나만 한 칸 바꾼 조합들의 순손익 평균 (한 점만 튀는 값을 거르기 위해, 2주차와 같음)
    index = {tuple(r[k] for k in keys): r for r in rows}
    for r in rows:
        neigh = []
        for i, k in enumerate(keys):
            vals = grid[k]
            at = vals.index(r[k])
            for j in (at - 1, at + 1):
                if 0 <= j < len(vals):
                    key = [r[k2] for k2 in keys]
                    key[i] = vals[j]
                    neigh.append(index[tuple(key)]["net_pnl"])
        r["neighbor_avg_pnl"] = round(sum(neigh) / len(neigh), 1) if neigh else None
        r["eligible"] = r["trades"] >= min_trades
    return rows


def label(kind: str, r: dict) -> str:
    if kind == "bb":
        rsi = "RSI 끔" if r["rsi_buy"] >= 100 else f"RSI {r['rsi_buy']:.0f}/{100 - r['rsi_buy']:.0f}"
        return (f"{r['bar_min']}분봉 · {r['band_k']}σ · {rsi} · {'복귀 확인' if r['confirm'] else '즉시'} · "
                f"ATR×{r['stop_atr']} · {r['hold_min']}분")
    return (f"범위 {r['range_min']}분 · 대기 {r['entry_min']}분 · 여유 {r['buffer_ticks']}틱 · "
            f"익절 {r['target_r']}배 · {r['hold_min']}분")


def fmt(s: dict) -> str:
    wr = f"{s['win_rate'] * 100:.0f}%" if s["win_rate"] is not None else "-"
    blocks = " · ".join(f"{k} {v['trades']}건 {v['pnl']:+.1f}" for k, v in s["by_block"].items())
    return (f"거래 {s['trades']} · 승률 {wr} · 순손익 US${s['net_pnl']:+,.1f} · PF {s['profit_factor']} · "
            f"평균 이익/손실 {s['avg_win']}/{s['avg_loss']} · 최대낙폭 US${s['max_drawdown']:,.1f} · "
            f"{s['by_reason']}\n    시간대: {blocks}")


def show_ranges(minutes: list[Minute], p: strategy_orb.Params) -> None:
    """개장 시점마다 첫 range_min 분 범위 폭(틱)과 그 안 1분봉 수 — 범위 필터 값이 맞는지 보려고."""
    print(f"개장 범위 폭 (첫 {p.range_min}분, 틱) · 괄호는 범위 안 1분봉 수")
    for name, open_ in p.windows:
        a, b = minutes_of(open_), minutes_of(open_) + p.range_min
        widths, counts = [], []
        for d in sorted({m.date for m in minutes}):
            got = [m for m in minutes if m.date == d and a <= minutes_of(m.time) < b]
            counts.append(len(got))
            if got:
                widths.append(ticks_between(max(m.high for m in got), min(m.low for m in got)))
        if widths:
            q = statistics.quantiles(widths, n=4) if len(widths) > 1 else [widths[0]] * 3
            print(f"  {name} {open_[:2]}:{open_[2:4]}  {len(widths)}일 · 폭 최소 {min(widths)} · 25% {q[0]:.0f} · "
                  f"중앙 {q[1]:.0f} · 75% {q[2]:.0f} · 최대 {max(widths)} · 봉 수 중앙 {statistics.median(counts):.0f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", choices=["bb", "orb", "both"], default="both")
    ap.add_argument("--from", dest="start", default=DATA_FROM)
    ap.add_argument("--to", dest="end", default=DATA_TO)
    ap.add_argument("--optimize", action="store_true")
    ap.add_argument("--standard", action="store_true", help="후보 A 를 2주차 정석 출발값으로")
    ap.add_argument("--ranges", action="store_true", help="후보 B 개장 범위 폭 분포만 출력")
    ap.add_argument("--train-end", default=TRAIN_END, help="학습 기간 마지막 날 (그 다음 날부터 검증)")
    ap.add_argument("--fee", type=float, default=0.0, help="왕복 수수료 US$ (기본 0)")
    args = ap.parse_args()

    minutes = load_minutes(start=args.start, end=args.end)
    days = sorted({m.date for m in minutes if in_trading_hours(m.time)})
    kinds = ["bb", "orb"] if args.strategy == "both" else [args.strategy]
    if args.ranges:
        show_ranges(minutes, strategy_orb.Params())
        return

    for kind in kinds:
        spec = STRATEGIES[kind]
        out = spec["out"]
        out.mkdir(parents=True, exist_ok=True)
        if not args.optimize:
            p = spec["standard"] if (args.standard or kind == "orb") else spec["module"].Params()
            res = run(minutes, kind, p, keep_signals=True, fee=args.fee)
            print(f"[{spec['name']}] 기간 {days[0]}~{days[-1]} ({len(days)}일) · {label(kind, asdict(p))}\n  {fmt(res['summary'])}")
            (out / "start_all.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
            continue

        tr_days = [d for d in days if d <= args.train_end]
        te_days = [d for d in days if d > args.train_end]
        print(f"\n===== {spec['name']} · 학습 {tr_days[0]}~{tr_days[-1]} ({len(tr_days)}일) · "
              f"검증 {te_days[0]}~{te_days[-1]} ({len(te_days)}일)")
        start_p = spec["module"].Params() if kind == "bb" else spec["standard"]
        s_tr, s_te, _, _ = split(run(minutes, kind, start_p, fee=args.fee), args.train_end)
        print(f"[출발값] {label(kind, asdict(start_p))}\n  학습: {fmt(s_tr)}\n  검증: {fmt(s_te)}")

        rows = optimize(minutes, kind, args.train_end, spec["min_trades"], args.fee)
        ok = [r for r in rows if r["eligible"]]
        by_net = sorted(ok, key=lambda r: r["net_pnl"], reverse=True)
        by_neigh = sorted(ok, key=lambda r: (r["neighbor_avg_pnl"], r["net_pnl"]), reverse=True)
        print(f"\n조합 {len(rows)}개 중 학습 거래 {spec['min_trades']}건 이상 {len(ok)}개 · "
              f"학습 순손익 양수 {sum(r['net_pnl'] > 0 for r in ok)}개")
        for title, lst in (("학습 순손익 상위 5", by_net), ("이웃 평균 상위 5", by_neigh)):
            print(title + ":")
            for r in lst[:5]:
                print(f"   {label(kind, r)} → 학습 {r['net_pnl']:+,.1f} ({r['trades']}건, 승률 {r['win_rate']}) · "
                      f"이웃 {r['neighbor_avg_pnl']:+,.1f} · MDD {r['max_drawdown']:,.1f}")

        keys = list(spec["grid"])
        picks = {"출발값": next(r for r in rows if all(r[k] == getattr(start_p, k) for k in keys))}
        if by_net:
            picks["학습1위"] = by_net[0]
        if by_neigh:
            picks["이웃평균1위"] = by_neigh[0]
        results = {"train_days": tr_days, "test_days": te_days, "grid": spec["grid"],
                   "min_trades": spec["min_trades"], "fee": args.fee,
                   "split": "전체 기간을 이어서 돌린 뒤 진입일로 학습/검증을 나눔", "picked": {}}
        print()
        for name, r in picks.items():
            p = replace(start_p, **{k: r[k] for k in keys})
            _, te, te_trades, te_signals = split(run(minutes, kind, p, keep_signals=True, fee=args.fee), args.train_end)
            print(f"[{name}] {label(kind, r)}\n  학습: 순손익 US${r['net_pnl']:+,.1f} 거래 {r['trades']} · "
                  f"이웃 {r['neighbor_avg_pnl']:+,.1f}\n  검증: {fmt(te)}")
            results["picked"][name] = {"params": asdict(p), "train": r, "test": te,
                                       "test_trades": te_trades, "test_signals": te_signals}
        (out / "optimize_grid.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        (out / "optimize_result.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"저장: {out.relative_to(HERE)}/optimize_grid.json, optimize_result.json")


if __name__ == "__main__":
    main()
