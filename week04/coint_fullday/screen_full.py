"""1단계 다시 — 하루 전체(24시간) 기준 스크리닝. `../screen.py` 는 15~18시만 봤습니다.

입력: ../results/data/bars/<sym>.csv (3주 1분봉, 10/19~11/07, 24시간이 다 들어 있음), ../results/data/products_all.csv
출력: results/screening_full.csv, 화면에 표

품목마다 가장 활발한 월물 하나만 남기고, 같은 기초자산의 작은 계약(마이크로 · E7 · J7 · QM)은 뺍니다
(가격이 큰 계약과 같아서 공적분 검정에 새 정보가 없음. 주문할 때만 바꿔 씀).

지표 (평일만, 모든 시각)
  active_min      하루 평균 「체결이 있던 1분봉」 수. CME 는 하루 1,380분 열림
  vol_per_day     하루 평균 체결 계약 수
  move30_usd      30분 종가 변화 표준편차, 계약당 달러 (하루 전체)
  cost_rt_usd     왕복 비용 가정 = 수수료 편도×2 + 1틱 (screen.py 와 같은 가정: 일반 $8, 마이크로 $2)
  move_to_cost    move30_usd / cost_rt_usd

공적분 후보(2단계로 넘기는 것)는 **거래 활발도만**으로 정합니다: active_min ≥ ACTIVE_MIN.
한 다리의 움직임/비용은 페어 수익을 정하지 않아서(금리처럼 한 다리는 덜 움직여도 스프레드 비용 비율은 다를 수 있음)
여기서 거르지 않고, 2단계에서 스프레드 기준 비용 비율로 봅니다.

  cd week04/coint_fullday && python3 screen_full.py
"""
from __future__ import annotations

import csv
import math
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
W4 = HERE.parent
sys.path.insert(0, str(W4))

from screen import MICRO, load_bars, load_specs  # noqa: E402  (팀원 파일 — 불러 쓰기만)

BARS = W4 / "results" / "data" / "bars"
OUT = HERE / "results"
SAME_AS_BIG = {"E7", "J7", "QM"}   # 미니 유로·미니 엔·미니 원유 = 6E·6J·CL 과 같은 가격
ACTIVE_MIN = 300                    # 하루 300분(= CME 열린 시간의 약 22%) 이상 체결이 있어야 후보


def tick_of(bars, spec_tick: float) -> float:
    """screen.py 와 같은 보정: 시세가 명세 틱의 10배 넘는 단위로 오면(6J·J7 은 100배 표시) 틱을 키움."""
    diffs = [abs(b[4] - a[4]) for a, b in zip(bars, bars[1:]) if abs(b[4] - a[4]) > 1e-12]
    tick = spec_tick
    if diffs and min(diffs) / tick >= 10:
        tick *= 10 ** round(math.log10(min(diffs) / tick))
    return tick


def main() -> None:
    specs = load_specs()
    by_prd: dict[str, list[tuple[str, Path, int]]] = defaultdict(list)
    for path in sorted(BARS.glob("*.csv")):
        sym = path.stem
        prd = sym[:-3]
        # MKU 는 screen.py 가 수수료 때문에 MICRO 에 넣었지만 큰 원달러 계약이 목록에 없어 남깁니다
        if (prd in MICRO and prd != "MKU") or prd in SAME_AS_BIG or prd not in specs:
            continue
        n = sum(1 for _ in path.open(encoding="utf-8")) - 1
        by_prd[prd].append((sym, path, n))

    rows = []
    for prd, items in sorted(by_prd.items()):
        sym, path, _ = max(items, key=lambda t: t[2])        # 가장 활발한 월물
        sp = specs[prd]
        bars = load_bars(path)                              # 평일만
        if len(bars) < 100:
            continue
        tick = tick_of(bars, sp["tick"])
        usd_per_pt = sp["tick_val"] / tick
        cost = 2 * (2.0 if prd in MICRO else 8.0) + sp["tick_val"]
        days = sorted({b[0] for b in bars})
        vol = sum(b[5] for b in bars)
        by_day: dict[str, dict[int, float]] = defaultdict(dict)
        for d, t, _h, _l, c, _v in bars:
            by_day[d][(int(t[:2]) * 60 + int(t[2:4])) // 30] = c   # 30분 구간 마지막 종가
        moves = []
        for d in days:
            b = by_day[d]
            ks = sorted(b)
            moves += [(b[k2] - b[k1]) * usd_per_pt for k1, k2 in zip(ks, ks[1:]) if k2 == k1 + 1]
        move30 = st.pstdev(moves) if len(moves) > 2 else math.nan
        last = bars[-1][4]
        rows.append({
            "prd": prd, "sym": sym, "name": sp["name"], "group": sp["group"], "cur": sp["cur"],
            "days": len(days), "active_min": round(len(bars) / len(days)), "vol_per_day": round(vol / len(days)),
            "move30_usd": round(move30, 1), "cost_rt_usd": round(cost, 2),
            "move_to_cost": round(move30 / cost, 2), "tick": tick, "tick_val_usd": round(sp["tick_val"], 4),
            "last": last, "notional_usd": round(last * usd_per_pt),
            "candidate": len(bars) / len(days) >= ACTIVE_MIN,
        })

    OUT.mkdir(parents=True, exist_ok=True)
    keys = list(rows[0])
    with (OUT / "screening_full.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: (-r["candidate"], -r["move_to_cost"])))
    print(f"{'품목':5} {'이름':14} {'묶음':4} {'체결분/일':>8} {'계약/일':>9} {'30분$':>8} {'왕복$':>6} {'움직임/비용':>8} 후보")
    for r in sorted(rows, key=lambda r: (r["group"], -r["move_to_cost"])):
        print(f"{r['prd']:5} {r['name'][:12]:14} {r['group'][:4]:4} {r['active_min']:>8} {r['vol_per_day']:>9,} "
              f"{r['move30_usd']:>8} {r['cost_rt_usd']:>6} {r['move_to_cost']:>8}  {'O' if r['candidate'] else '-'}")
    print(f"후보 {sum(r['candidate'] for r in rows)} / {len(rows)} (체결분 ≥ {ACTIVE_MIN}/일)")


if __name__ == "__main__":
    main()
