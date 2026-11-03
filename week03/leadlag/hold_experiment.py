"""보유 시간을 늘리면 연계 신호의 손익이 어떻게 바뀌나 — 비용(호가·수수료)을 넣은 순차 시뮬레이션.

사용법 (week03/leadlag/ 에서):
  python3 hold_experiment.py
  python3 hold_experiment.py --start 20261013 --end 20261030

신호 두 가지 (둘 다 09:00~15:20 진입, 15:40 강제 청산, 1계약, 보유 중 새 진입 없음):
  short : KRX 1분 Δ ≥ 0.5원 이고 같은 분 MKU 반영률 < 0.5  → KRX 방향 (leadlag.py 의 가장 좋은 조건)
  trend : KRX 30분 Δ ≥ 1.5원                               → KRX 방향 (KRX 를 방향 필터로)
  trendM: MKU 자기 30분 움직임 ≥ 같은 크기                  → 그 방향 (KRX 가 보태는 게 있는지 비교용)
진입가 = 신호 분의 MKU 마지막 체결가, 청산가 = h 분 뒤 마지막 체결가.
비용(왕복, MKU 틱): 호가 차이 1틱 + 수수료 2 × (편도 $ ÷ $2.50). 수수료는 $2·$8 두 경우.
"""
from __future__ import annotations

import argparse
import json
import statistics as st
from pathlib import Path

import leadlag as L

HERE = Path(__file__).parent
TICK_USD = 2.5
FEES = {"$2": 2.0, "$8": 8.0}
SPREAD_TICKS = 1.0
HOLDS = (3, 5, 10, 15, 30, 60, 90, 120)


def day_series(krx, mku, d, ts):
    kc, mc, lk, lm = [], [], None, None
    for t in ts:
        lk = krx.get((d, t), (0, lk))[1]
        lm = mku.get((d, t), (0, lm))[1]
        kc.append(lk)
        mc.append(lm)
    return kc, mc


def signal(kind: str, kc, mc, i: int) -> int:
    """+1 = MKU 매수(원화 강세 쪽), -1 = MKU 매도, 0 = 없음."""
    if kind == "short":
        if None in (kc[i], kc[i - 1], mc[i], mc[i - 1]):
            return 0
        dk = kc[i] - kc[i - 1]
        if abs(dk) < 0.5:
            return 0
        s = 1 if dk > 0 else -1
        exp_ticks = abs(dk) / (1000 * L.MKU_TICK / mc[i] ** 2)
        got = -s * (mc[i] - mc[i - 1]) / L.MKU_TICK
        return -s if got / exp_ticks < 0.5 else 0
    if i < 30 or None in (kc[i], kc[i - 30], mc[i], mc[i - 30]):
        return 0
    if kind == "trend":
        dk = kc[i] - kc[i - 30]
        return 0 if abs(dk) < 1.5 else (-1 if dk > 0 else 1)
    # trendM: MKU 를 원/달러로 바꾼 30분 움직임이 1.5원 이상
    dm = 1000 / mc[i] - 1000 / mc[i - 30]
    return 0 if abs(dm) < 1.5 else (-1 if dm > 0 else 1)


def simulate(kind, hold, krx, mku, days, ts):
    entry_last = ts.index("1520")
    flat = ts.index("1530")          # 데이터 끝(15:30) = 15:40 청산의 근사
    trades = []
    for d in days:
        kc, mc = day_series(krx, mku, d, ts)
        i = 1
        while i <= entry_last:
            s = signal(kind, kc, mc, i)
            if s == 0:
                i += 1
                continue
            j = min(i + hold, flat)
            trades.append(s * (mc[j] - mc[i]) / L.MKU_TICK)
            i = j + 1                # 청산 다음 분부터 다시 판정
    return trades


def summarize(trades):
    if not trades:
        return None
    out = {"n": len(trades), "gross_mean": st.mean(trades),
           "t": L.tstat(trades), "win_gross": sum(x > 0 for x in trades) / len(trades) * 100}
    for name, fee in FEES.items():
        cost = SPREAD_TICKS + 2 * fee / TICK_USD
        net = [x - cost for x in trades]
        out[f"net{name}_mean"] = st.mean(net)
        out[f"net{name}_total_usd"] = sum(net) * TICK_USD
        out[f"win{name}"] = sum(x > 0 for x in net) / len(net) * 100
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="20261013")
    ap.add_argument("--end", default="20261030")
    args = ap.parse_args()
    krx = L.load(L.KRX_FILES)
    mku = L.load([HERE / "results" / "data" / "bars_1m_MKUX26.csv"])
    days = sorted({d for d, _ in krx if args.start <= d <= args.end} & {d for d, _ in mku})
    ts = L.minutes()
    print(f"세션 {len(days)}개 ({days[0]}~{days[-1]}) · 비용(왕복) $2: {SPREAD_TICKS + 4 / TICK_USD:.1f}틱, "
          f"$8: {SPREAD_TICKS + 16 / TICK_USD:.1f}틱")
    result = {}
    for kind in ("short", "trend", "trendM"):
        print(f"\n[{kind}]  보유 | 거래수 | 총평균(비용 전) t  승률 | 순평균 $2  $8 | 14일 순손익 $2  $8")
        for h in HOLDS:
            s = summarize(simulate(kind, h, krx, mku, days, ts))
            result[f"{kind}_{h}"] = s
            if not s:
                continue
            print(f"  {h:>4}분 | {s['n']:>4} | {s['gross_mean']:+6.2f}틱 {s['t']:+5.1f} {s['win_gross']:4.0f}% | "
                  f"{s['net$2_mean']:+6.2f} {s['net$8_mean']:+6.2f} | "
                  f"${s['net$2_total_usd']:+8.0f} ${s['net$8_total_usd']:+8.0f}")
    out = HERE / "results" / "analysis" / "hold_experiment.json"
    out.write_text(json.dumps({"days": days, "result": result}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n저장: {out.relative_to(HERE)}")


if __name__ == "__main__":
    main()
