"""런던 개장 돌파 (3주차 후보 B 규칙) — 대회 종목 고르기 백테스트. 1분봉, 2026-10-01~11-06.

3주차 strategy_orb.py 규칙을 종목 무관하게 다시 쓴 것 (그 파일은 MKU 틱·시간대에 맞춰져 있어 직접 못 씀):
- 개장: 런던 현지 07:00 또는 08:00. 한국 시간으로는 10/25 까지(영국 서머타임) 15:00·16:00, 10/26 부터 16:00·17:00.
  11/13 대회 날은 16:00·17:00.
- 기준 범위: 개장부터 range_min 분 동안의 1분봉 최고가·최저가. 범위 폭이 4틱 미만이면 진입 안 함 (3주차와 같음).
- 진입: 범위가 정해진 뒤 entry_min 분 안에 1분봉 종가가 범위 밖으로 1틱 이상 → 그 방향. 다음 분 시가 + 불리한 쪽 1틱.
- 손절: 범위 반대편 끝 (닿으면 그 값 − 1틱, 시가가 이미 넘었으면 시가 − 1틱).
- 익절: 진입가 ± 범위 폭 × target_r (지정가, 슬리피지 없음). 같은 봉에서 손절·익절 둘 다 닿으면 손절.
- 시간 청산: 진입 뒤 hold_min 분, 늦어도 17:58 봉 종가 − 1틱 (대회 끝 18:00). 개장마다 하루 한 번.
- 비용: 계약당 편도 수수료. 「낙관」 $2 (모의 SGX 미니 원달러 실측과 같은 값) / 「공식」 일반 $8 (NH 수수료 부과 기준).
- 비교 단위 R: 1 R = 진입 때 손절까지 잃을 돈 (손절 틱 + 1틱 슬리피지) × 틱가치 + 왕복 수수료. 종목마다 틱 크기가
  달라 달러로는 비교가 안 돼서, 같은 위험(1 R)을 졌을 때 몇 R 을 벌었나로 본다.

사용법 (week04/contest_orb/ 에서):  python3 orb_backtest.py   → results/backtest/
"""
from __future__ import annotations

import csv
import itertools
import json
import statistics as st
from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import date
from pathlib import Path

HERE = Path(__file__).parent
DATA = HERE / "results" / "data"
OUT = HERE / "results" / "backtest"
FIRST_DAY, TEST_FROM = "20261001", "20261026"
BST_END = "20261026"          # 이날부터 영국 표준시 (런던 07:00 = 16:00 KST)
LAST_BAR = "175800"           # 이 봉 종가에 정리 (대회 끝 18:00 전)

# 품목정보(11/08 조회): 틱(차트 가격 단위)·틱가치. 증거금은 주문가능수량 API 로 본 범위의 위쪽.
PRODUCTS = {
    "RBZ26": {"name": "가솔린", "tick": 0.0001, "tick_val": 4.2, "margin": 9_999},
    "HOZ26": {"name": "난방유", "tick": 0.0001, "tick_val": 4.2, "margin": 16_665},
    "CLZ26": {"name": "WTI 원유", "tick": 0.01, "tick_val": 10.0, "margin": 9_999},
    "NGZ26": {"name": "천연가스", "tick": 0.001, "tick_val": 10.0, "margin": 3_125},
    "SIZ26": {"name": "은", "tick": 0.005, "tick_val": 25.0, "margin": 24_998},
    "GCZ26": {"name": "금", "tick": 0.1, "tick_val": 10.0, "margin": 24_998},
    "PLF27": {"name": "백금", "tick": 0.1, "tick_val": 5.0, "margin": 7_142},
    "HGZ26": {"name": "구리", "tick": 0.0005, "tick_val": 12.5, "margin": 11_110},
    "NQZ26": {"name": "나스닥 E-mini", "tick": 0.25, "tick_val": 5.0, "margin": 49_995},
    "6EZ26": {"name": "유로", "tick": 0.00005, "tick_val": 6.25, "margin": 2_325},
}
FEES = {"낙관 $2": 2.0, "공식 $8": 8.0}


@dataclass(frozen=True)
class Rule:
    london_open: str = "0700"   # 런던 현지 시각 HHMM
    range_min: int = 10         # 3주차 채택값
    entry_min: int = 60         # 3주차: 09:10~10:10
    target_r: float = 2.0       # 3주차 채택값
    hold_min: int = 60          # 3주차 채택값
    min_range_ticks: int = 4    # 3주차와 같음


def load(sym: str) -> dict[str, dict[str, tuple[float, float, float, float]]]:
    out: dict[str, dict] = defaultdict(dict)
    for r in csv.DictReader(open(DATA / f"{sym}.csv")):
        try:
            o, h, l, c = (float(r[k]) for k in ("open_pric", "high_pric", "low_pric", "close_pric"))
        except ValueError:
            continue
        if c > 0:
            out[r["ref_dt"]][r["exch_tm"]] = (o, h, l, c)
    return out


def kst_open(day: str, london_hhmm: str) -> str:
    h = int(london_hhmm[:2]) + (8 if day < BST_END else 9)
    return f"{h:02d}{london_hhmm[2:]}00"


def add_min(hhmmss: str, n: int) -> str:
    m = int(hhmmss[:2]) * 60 + int(hhmmss[2:4]) + n
    return f"{m // 60:02d}{m % 60:02d}00"


def weekday(d: str) -> int:
    return date(int(d[:4]), int(d[4:6]), int(d[6:])).weekday()


def run_day(day: str, bars: dict, spec: dict, rule: Rule) -> dict | None:
    tick = spec["tick"]
    t0 = kst_open(day, rule.london_open)
    t_range_end = add_min(t0, rule.range_min)
    rng = [b for t, b in bars.items() if t0 <= t < t_range_end]
    if not rng:
        return None
    hi, lo = max(b[1] for b in rng), min(b[2] for b in rng)
    width = round((hi - lo) / tick)
    if width < rule.min_range_ticks:
        return {"day": day, "skip": f"범위 {width}틱"}
    times = sorted(t for t in bars if t >= t_range_end and t <= LAST_BAR)
    entry_last = min(add_min(t_range_end, rule.entry_min), add_min(LAST_BAR, -2))
    for i, t in enumerate(times):
        if t > entry_last:
            break
        c = bars[t][3]
        side = 1 if c >= hi + tick - 1e-12 else (-1 if c <= lo - tick + 1e-12 else 0)
        if not side or i + 1 >= len(times):
            continue
        et = times[i + 1]
        entry = bars[et][0] + side * tick
        stop = lo if side > 0 else hi
        target = entry + side * width * rule.target_r * tick
        stop_ticks = abs(entry - stop) / tick + 1
        exit_by = min(add_min(et, rule.hold_min), LAST_BAR)
        why, px, xt = None, None, None
        for t2 in times[i + 1:]:
            o, h, l, cl = bars[t2]
            hit_stop = (l <= stop) if side > 0 else (h >= stop)
            hit_tgt = (h >= target) if side > 0 else (l <= target)
            if hit_stop:
                base = min(o, stop) if side > 0 else max(o, stop)
                why, px, xt = "손절", base - side * tick, t2
                break
            if hit_tgt:
                why, px, xt = "익절", (max(o, target) if side > 0 else min(o, target)), t2
                break
            if t2 >= exit_by:
                why, px, xt = "시간", cl - side * tick, t2
                break
        if why is None:                       # 데이터가 끊김 → 마지막 종가
            t2 = times[-1]
            why, px, xt = "끝", bars[t2][3] - side * tick, t2
        ticks = side * (px - entry) / tick
        out = {"day": day, "open_kst": t0, "side": side, "entry_t": et, "exit_t": xt, "why": why,
               "range_ticks": width, "stop_ticks": round(stop_ticks, 1), "ticks": round(ticks, 1),
               "test": day >= TEST_FROM, "fri": weekday(day) == 4}
        for fname, fee in FEES.items():
            risk = stop_ticks * spec["tick_val"] + 2 * fee
            pnl = ticks * spec["tick_val"] - 2 * fee
            out[f"usd {fname}"] = round(pnl, 2)
            out[f"R {fname}"] = round(pnl / risk, 3)
        return out
    return {"day": day, "skip": "돌파 없음"}


def run(sym: str, rule: Rule) -> list[dict]:
    data = load(sym)
    days = sorted(d for d in data if d >= FIRST_DAY and weekday(d) < 5)
    out = []
    for d in days:
        r = run_day(d, data[d], PRODUCTS[sym], rule)
        if r:
            out.append(r)
    return out


def summary(rows: list[dict], key: str) -> dict:
    tr = [r for r in rows if "skip" not in r]
    days = len(rows)
    if not tr:
        return {"days": days, "trades": 0}
    v = [t[key] for t in tr]
    return {"days": days, "trades": len(tr), "sum_R": round(sum(v), 2), "avg_R": round(st.mean(v), 3),
            "win": sum(x > 0 for x in v), "worst_R": round(min(v), 2),
            "why": {w: sum(t["why"] == w for t in tr) for w in ("익절", "손절", "시간", "끝")}}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    base = Rule()
    grid_axes = {"range_min": (5, 10, 15), "target_r": (1.0, 2.0), "hold_min": (30, 60)}
    result = {}
    all_rows = []
    for sym in PRODUCTS:
        if not (DATA / f"{sym}.csv").exists():
            continue
        result[sym] = {"name": PRODUCTS[sym]["name"]}
        for lo in ("0700", "0800"):
            rule = replace(base, london_open=lo)
            rows = run(sym, rule)
            all_rows += [{"sym": sym, "london": lo, **r} for r in rows]
            part = {"학습": [r for r in rows if r["day"] < TEST_FROM], "검증": [r for r in rows if r["day"] >= TEST_FROM],
                    "금요일": [r for r in rows if weekday(r["day"]) == 4]}
            res = {f"{p} {k}": summary(rs, f"R {k}") for p, rs in part.items() for k in FEES}
            # 이웃 값 12개: 학습·검증 모두 플러스 비율
            both, tr_pos, te_pos = 0, 0, 0
            combos = list(itertools.product(*grid_axes.values()))
            for combo in combos:
                rr = run(sym, replace(rule, **dict(zip(grid_axes, combo))))
                a = sum(r["R 공식 $8"] for r in rr if "skip" not in r and r["day"] < TEST_FROM)
                b = sum(r["R 공식 $8"] for r in rr if "skip" not in r and r["day"] >= TEST_FROM)
                tr_pos += a > 0
                te_pos += b > 0
                both += a > 0 and b > 0
            res["이웃 12개 (공식 $8)"] = {"학습 플러스": tr_pos, "검증 플러스": te_pos, "둘 다": both}
            result[sym][f"런던 {lo[:2]}:{lo[2:]}"] = res
    (OUT / "summary.json").write_text(json.dumps({"rule": asdict(base), "fees": FEES, "products": PRODUCTS,
                                                  "result": result}, ensure_ascii=False, indent=1))
    keys = sorted({k for r in all_rows for k in r}, key=lambda k: (k not in ("sym", "london", "day"), k))
    with open(OUT / "trades.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(all_rows)
    for sym, r in result.items():
        for lo in ("런던 07:00", "런던 08:00"):
            x = r[lo]
            def f(s):
                if not s.get("trades"):
                    return "  거래 0"
                return f"{s['trades']:2d}건 승{s['win']:2d} 합 {s['sum_R']:+6.2f}R 평균 {s['avg_R']:+.2f}R"
            print(f"{sym:6s} {r['name']:8s} {lo} | 학습 {f(x['학습 공식 $8'])} | 검증 {f(x['검증 공식 $8'])} | "
                  f"금 {f(x['금요일 공식 $8'])} | 이웃 {x['이웃 12개 (공식 $8)']}")


if __name__ == "__main__":
    main()
