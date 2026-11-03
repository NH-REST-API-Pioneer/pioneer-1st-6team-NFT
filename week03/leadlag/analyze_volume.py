"""MKU 상품 특성 — 한국시간 시간대별 평균 거래량·변동폭.

사용법 (week03/leadlag/ 에서):
  python3 analyze_volume.py                                  # MKUX26, 10/15~10/30 세션
  python3 analyze_volume.py --start 20261013 --end 20261030

차트(분)의 거래소시간(exch_tm)은 한국시간입니다 — 주간 08:25 개장 전(06:15~08:25)에 봉이 하나도 없습니다.
분봉은 체결이 있었던 분만 옵니다. 그래서 「평균 거래량」은 시간대 합계 ÷ 세션 수로 셉니다.
06:15 에 끝나는 야간 세션의 자정 이후 봉은 전날 세션으로 묶습니다.
"""
from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

HERE = Path(__file__).parent
TICK = 0.0001                 # MKU 틱 (품목별세부정보)
DAY = (8 * 60 + 45, 15 * 60 + 45)   # 서울 주간장 = KRX 달러선물 08:45~15:45
MOCK_CLOSE = 24 * 60          # 모의 해외 운영 07:00~24:00
from matplotlib import font_manager  # noqa: E402

_have = {f.name for f in font_manager.fontManager.ttflist}
plt.rcParams["font.family"] = [f for f in ("AppleGothic", "Malgun Gothic", "NanumGothic") if f in _have] + ["sans-serif"]
plt.rcParams["axes.unicode_minus"] = False


def session_day(ref_dt: str, hhmm: int) -> date:
    d = date(int(ref_dt[:4]), int(ref_dt[4:6]), int(ref_dt[6:]))
    return d - timedelta(days=1) if hhmm < 7 * 60 else d


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sym", default="MKUX26")
    ap.add_argument("--start", default="20261015", help="세션 시작일 (근월물이 된 뒤부터)")
    ap.add_argument("--end", default="20261030")
    args = ap.parse_args()
    start, end = (date(int(s[:4]), int(s[4:6]), int(s[6:])) for s in (args.start, args.end))

    vol = defaultdict(float)          # 시(0~23) → 거래량 합
    rng = defaultdict(list)           # 시 → 1분봉 (고가-저가) 틱
    sessions = set()
    zones = defaultdict(float)        # 구간별 거래량
    for r in csv.DictReader((HERE / "results" / "data" / f"bars_1m_{args.sym}.csv").open(encoding="utf-8")):
        t = r["exch_tm"]
        m = int(t[:2]) * 60 + int(t[2:4])
        s = session_day(r["ref_dt"], m)
        if not (start <= s <= end) or s.weekday() >= 5:
            continue
        sessions.add(s)
        q = float(r["cum_trd_qty"])
        vol[m // 60] += q
        rng[m // 60].append((float(r["high_pric"]) - float(r["low_pric"])) / TICK)
        if DAY[0] <= m < DAY[1]:
            zones["서울 주간장 08:45~15:45"] += q
        elif m >= 7 * 60:
            zones["그 밖의 모의 시간 (~24:00)"] += q
        else:
            zones["모의 마감 뒤 00:00~06:15"] += q

    n = len(sessions)
    total = sum(zones.values())
    hours = list(range(24))
    avg = [vol[h] / n for h in hours]
    avg_rng = [sum(rng[h]) / len(rng[h]) if rng[h] else 0 for h in hours]

    out = HERE / "results" / "analysis"
    out.mkdir(parents=True, exist_ok=True)
    with (out / f"volume_by_hour_{args.sym}.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["hour_kst", "avg_volume_per_session", "share_pct", "avg_1m_range_ticks", "bars"])
        for h in hours:
            w.writerow([h, round(avg[h], 1), round(vol[h] / total * 100, 2), round(avg_rng[h], 2), len(rng[h])])

    print(f"{args.sym} · 세션 {n}개 ({min(sessions)} ~ {max(sessions)}) · 총 거래량 {total:,.0f} (세션당 {total / n:,.0f})")
    for k, v in zones.items():
        print(f"  {k:<26} {v / total * 100:5.1f}%")
    for h in hours:
        if vol[h]:
            print(f"  {h:02d}시  평균 {avg[h]:7.0f}계약  1분봉 변동폭 {avg_rng[h]:.2f}틱")

    order = [*range(7, 24), *range(0, 7)]          # 한국 아침부터 다음날 새벽 순
    labels = [f"{h:02d}" for h in order]
    colors = ["#2f6db5" if DAY[0] // 60 <= h < 16 else ("#9db7d9" if h >= 7 else "#cccccc") for h in order]
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 7), sharex=True, gridspec_kw={"height_ratios": [3, 2]})
    ax1.bar(labels, [avg[h] for h in order], color=colors)
    ax1.set_ylabel("세션당 평균 거래량 (계약)")
    ax1.set_title(f"SGX MKU({args.sym}) 한국시간 시간대별 거래량 · {min(sessions):%m/%d}~{max(sessions):%m/%d} {n}세션")
    ax1.axvline(labels.index("00") - 0.5, color="#c0392b", ls="--", lw=1)
    ax1.text(labels.index("00") - 0.4, max(avg) * 0.95, "모의 운영 마감 24:00", color="#c0392b", fontsize=9)
    # 봉이 몇 개 없는 시간(06시 마감 직전 등)은 평균이 튀므로 그리지 않습니다
    ax2.plot(labels, [avg_rng[h] if len(rng[h]) >= 30 else float("nan") for h in order], marker="o", color="#444")
    ax2.set_ylabel("1분봉 평균 변동폭 (틱)")
    ax2.set_xlabel("한국시간 (시)  ·  진한 파랑 = 서울 주간장 08~15시, 연한 파랑 = 그 밖의 모의 시간, 회색 = 모의 마감 뒤")
    for ax in (ax1, ax2):
        ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    png = HERE / "results" / "charts" / f"volume_by_hour_{args.sym}.png"
    png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(png, dpi=130)
    print(f"  저장: {png.relative_to(HERE)}")


if __name__ == "__main__":
    main()
