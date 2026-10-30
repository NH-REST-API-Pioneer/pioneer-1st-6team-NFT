"""실행 기록(results/runs/*/journal.jsonl · summary.json)을 README 표로 바꿉니다.

  python3 summarize_runs.py                                  # results/runs/ 의 모든 실행
  python3 summarize_runs.py results/runs/20261030_bb_orders  # 한 실행만

기록 파일은 읽기만 합니다. 수수료는 기록에 있는 값(모의계좌: 예수금 fee 차이, 종이·리플레이: 체결마다 690원)을
쓰고, 기록에 없으면(10/29 실행 — 수수료 기록 추가 전) FEE_PER_FILL 로 셉니다. 시각은 모두 KST.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).parent
FEE_PER_FILL = 690          # 10/29·10/30 모의계좌 예수금 fee 로 확인한 1계약 1번 체결 수수료
TICK = 0.1


def hm(c: str) -> str:
    return f"{c[:2]}:{c[2:4]}"


def load(run: Path) -> tuple[list[dict], dict]:
    recs = [json.loads(line) for line in (run / "journal.jsonl").read_text(encoding="utf-8").splitlines() if line]
    summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    return recs, summary


def reason_kind(reason: str) -> str:
    """신호 기록의 reason 을 갈래로 묶음 (README 의 「신호 까닭별 건수」)."""
    if "걸러" in reason:
        return "밴드 밖이지만 RSI 가 걸러 진입 안 함"
    if "보유 중" in reason:
        return "신호 · 이미 보유 중"
    if "청산 뒤 대기" in reason:
        return "신호 · 청산 뒤 1봉 대기"
    if "시간 밖" in reason:
        return "신호 · 진입 시간(09:30~15:00) 밖"
    if "한도" in reason or "손절" in reason:
        return "신호 · 손절 2회 한도로 진입 안 함"
    return reason


def report(run: Path) -> None:
    recs, summary = load(run)
    trades = summary["trades"]
    print(f"\n## {run.name}\n")

    signals = [r for r in recs if r["kind"] == "signal"]
    entered = Counter()
    kinds = Counter()
    for s in signals:
        if s.get("side") and not any(k in s["reason"] for k in ("보유 중", "시간 밖", "한도")):
            entered["진입 신호"] += 1
        else:
            kinds[reason_kind(s["reason"])] += 1
    errors = [r for r in recs if r["kind"] == "event" and any(k in r.get("text", "") for k in ("오류", "실패", "확인 못"))]
    fills = [r for r in recs if r["kind"] == "fill"]

    print("| # | 방향 | 신호 봉 | 진입 | 청산 | 청산 까닭 | 틱 | 손익 | 수수료 | 수수료 뒤 |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    gross = fee_sum = 0.0
    for i, t in enumerate(trades, 1):
        fee = t.get("fee")
        if fee is None:
            fee = 2 * FEE_PER_FILL
        gross += t["pnl"]
        fee_sum += fee
        side = "매수" if t["side"] == "buy" else "매도"
        sig = hm(t["signal_time"]) if t.get("signal_time") else "-"
        print(f"| {i} | {side} | {sig} | {hm(t['entry_time'])} · {t['entry']:.1f} | {hm(t['exit_time'])} · {t['exit']:.1f} "
              f"| {t['reason']} | {t['ticks']:+d} | {t['pnl']:+,} | {fee:,.0f} | {t['pnl'] - fee:+,.0f} |")
    print(f"| **합계** | | | | | | | **{gross:+,.0f}** | {fee_sum:,.0f} | **{gross - fee_sum:+,.0f}** |")

    # 신호 봉 종가 vs 진입 체결가 — 불리한 쪽이면 +
    sig_close = {s["clock"]: s["close"] for s in signals if s.get("side")}
    slips = []
    for t in trades:
        c = sig_close.get(t.get("signal_time", ""))
        if c is not None:
            slips.append(round(((t["entry"] - c) if t["side"] == "buy" else (c - t["entry"])) / TICK))
    print(f"\n- 진입 신호 {entered['진입 신호']}번 · 체결 기록 {len(fills)}건 · 거래 {len(trades)}건")
    for k, n in kinds.most_common():
        print(f"- {k}: {n}번")
    if slips:
        print(f"- 신호 봉 종가 대비 진입 체결 (불리한 쪽 +): {', '.join(f'{s:+d}틱' for s in slips)}")
    for side in ("account_start", "account_end"):
        if summary.get(side):
            a = summary[side]
            print(f"- 계좌 {'시작' if side == 'account_start' else '끝'}: 수수료 {a['fee']:,.0f} · 확정손익 {a['fut_cls_pl_amt']:+,.0f} "
                  f"· 주문가능 {a['ord_psb_tot_amt']:,.0f}")
    print(f"- 오류 이벤트: {len(errors)}건" + "".join(f"\n  - {e['at']} {e['text']}" for e in errors))


def main() -> None:
    runs = [Path(a) for a in sys.argv[1:]] or sorted(p for p in (HERE / "results" / "runs").iterdir()
                                                     if (p / "summary.json").exists())
    for run in runs:
        report(run)


if __name__ == "__main__":
    main()
