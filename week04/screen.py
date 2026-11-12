"""스크리닝 2단계 — KST 15:00~18:00의 거래량·변동성·비용을 품목별로 비교.

입력: results/data/bars/<sym>.csv (fetch_bars.py), results/data/products_all.csv (list_products.py)
출력: results/screening.csv, results/hourly_volume.csv, 화면에 상위 표

지표 (평일만, 15:00~17:59 시작 분봉)
  vol_per_min     구간 분당 평균 체결 계약 수 (거래 없는 분은 0 으로 셈)
  share_vs_avg    구간 시간당 거래량 / 하루 평균 시간당 거래량 (1 보다 크면 그 시간에 몰림)
  move30_usd      30분 수익(종가 차) 표준편차를 계약당 달러로
  range3h_usd     15~18시 고가-저가 평균, 계약당 달러
  cost_rt_usd     왕복 비용 가정 = 수수료 편도×2 + 호가 1틱
  move_to_cost    move30_usd / cost_rt_usd (클수록 비용이 덜 아픔)

수수료 가정: NH 표(2026-09-07) USD 상품 계약당 $8, E-micro·E-nano $2. MKU 는 실측 $2.
USD 아닌 상품은 대략 환율(아래 FX)로 달러 환산 — 순위 보기용이라 정밀하지 않습니다.
"""
from __future__ import annotations

import csv
import math
import statistics as st
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).parent
BARS = ROOT / "results" / "data" / "bars"
PRODUCTS = ROOT / "results" / "data" / "products_all.csv"
FX = {"USD": 1.0, "JPY": 1 / 150, "GBP": 1.31, "EUR": 1.12, "SGD": 0.77, "CNH": 0.14, "CNY": 0.14, "HKD": 0.128}
MICRO = {"MKU", "MES", "MNQ", "MYM", "M2K", "MGC", "MSI", "MCL", "M6E", "M6B", "M6A", "MJY", "MCD", "MSF"}
WIN = ("150000", "175959")


def load_specs() -> dict[str, dict]:
    specs = {}
    with PRODUCTS.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            specs[r["prd_cd"]] = {
                "name": r["ko_prd_nm"].strip(), "group": r["prd_grp_nm"].strip(), "cur": r["cur_cd"],
                "tick": float(r["tick_unt"]), "tick_val": float(r["tick_val"]) * FX.get(r["cur_cd"], 1.0),
            }
    return specs


def load_bars(path: Path) -> list[tuple[str, str, float, float, float, float]]:
    out = []
    with path.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if date(int(r["ref_dt"][:4]), int(r["ref_dt"][4:6]), int(r["ref_dt"][6:])).weekday() >= 5:
                continue
            out.append((r["ref_dt"], r["exch_tm"], float(r["high_pric"]), float(r["low_pric"]),
                        float(r["close_pric"]), float(r["cum_trd_qty"] or 0)))
    return out


def main() -> None:
    specs = load_specs()
    results, hourly_rows = [], []
    for path in sorted(BARS.glob("*.csv")):
        sym = path.stem
        prd = sym[:-3]
        sp = specs.get(prd)
        bars = load_bars(path)
        if not sp or len(bars) < 100:
            continue
        # 시세가 명세 틱과 다른 단위로 올 때가 있음 (6J·J7 은 100 배로 표시: 0.636 = 0.00636).
        # 실제 최소 가격 변화가 명세 틱의 10 배 넘게 크면 그만큼 틱을 키웁니다.
        diffs = [abs(b[4] - a[4]) for a, b in zip(bars, bars[1:]) if abs(b[4] - a[4]) > 1e-12]
        tick = sp["tick"]
        if diffs and min(diffs) / tick >= 10:
            tick *= 10 ** round(math.log10(min(diffs) / tick))
        usd_per_pt = sp["tick_val"] / tick
        fee = 2.0 if prd in MICRO else 8.0
        cost = 2 * fee + sp["tick_val"]

        by_day: dict[str, list] = defaultdict(list)
        hour_vol: dict[int, float] = defaultdict(float)
        days = set()
        for d, t, h, l, c, v in bars:
            days.add(d)
            hour_vol[int(t[:2])] += v
            if WIN[0] <= t <= WIN[1]:
                by_day[d].append((t, h, l, c, v))
        win_days = [d for d in by_day if len(by_day[d]) >= 30]
        if not win_days:
            results.append({"sym": sym, "name": sp["name"], "note": "15~18시 거래 거의 없음"})
            continue
        n_days = len(days)
        total_vol = sum(hour_vol.values())
        active_hours = sum(1 for h in hour_vol if hour_vol[h] > 0)
        win_vol = sum(v for d in win_days for *_, v in by_day[d])
        vol_per_min = win_vol / (len(win_days) * 180)
        share = (win_vol / len(win_days) / 3) / (total_vol / n_days / max(active_hours, 1))

        moves30, ranges = [], []
        for d in win_days:
            rows_ = by_day[d]
            ranges.append((max(r[1] for r in rows_) - min(r[2] for r in rows_)) * usd_per_pt)
            buckets: dict[int, float] = {}
            for t, _, _, c, _ in rows_:
                k = (int(t[:2]) * 60 + int(t[2:4])) // 30
                buckets[k] = c  # 30분 구간 마지막 종가
            closes = [buckets[k] for k in sorted(buckets)]
            moves30 += [(b - a) * usd_per_pt for a, b in zip(closes, closes[1:])]
        move30 = st.pstdev(moves30) if len(moves30) > 2 else math.nan
        results.append({
            "sym": sym, "name": sp["name"], "group": sp["group"], "days": len(win_days),
            "vol_per_min": round(vol_per_min, 1), "share_vs_avg": round(share, 2),
            "move30_usd": round(move30, 1), "range3h_usd": round(st.mean(ranges), 1),
            "cost_rt_usd": round(cost, 2), "move_to_cost": round(move30 / cost, 2), "note": "",
        })
        per_day = {h: hour_vol[h] / n_days for h in range(24)}
        hourly_rows.append({"sym": sym, **{f"h{h:02d}": round(per_day[h]) for h in range(24)}})

    fields = ["sym", "name", "group", "days", "vol_per_min", "share_vs_avg", "move30_usd", "range3h_usd",
              "cost_rt_usd", "move_to_cost", "note"]
    with (ROOT / "results" / "screening.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(results)
    with (ROOT / "results" / "hourly_volume.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["sym"] + [f"h{h:02d}" for h in range(24)])
        w.writeheader()
        w.writerows(hourly_rows)

    ok = sorted((r for r in results if not r["note"]), key=lambda r: -r["move_to_cost"])
    print(f"{'sym':8} {'이름':14} {'일수':>3} {'분당계약':>8} {'몰림':>5} {'30분$':>7} {'3시간폭$':>8} {'왕복$':>6} {'움직임/비용':>8}")
    for r in ok:
        print(f"{r['sym']:8} {r['name'][:12]:14} {r['days']:>3} {r['vol_per_min']:>8} {r['share_vs_avg']:>5} "
              f"{r['move30_usd']:>7} {r['range3h_usd']:>8} {r['cost_rt_usd']:>6} {r['move_to_cost']:>8}")
    for r in results:
        if r["note"]:
            print(f"{r['sym']:8} {r['name']}: {r['note']}")


if __name__ == "__main__":
    main()
