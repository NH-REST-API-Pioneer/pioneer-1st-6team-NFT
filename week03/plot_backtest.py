"""두 후보 백테스트 결과를 차트 이미지(PNG)로 그립니다 — 전략 설계 문서에 넣는 그림.

색·글꼴·검증 구간 표시는 2주차 plot_backtest.py 와 같은 모양입니다 (이익 빨강 · 손실 파랑,
두 계열 비교는 파랑·주황). 후보 B 의 두 값(선택 · 출발)은 같은 빨강에 실선·점선으로 나눕니다.

거래 목록은 backtest.run() 을 그 자리에서 다시 돌려 얻습니다 (16일 한 번에 0.1초 안). 최적화 표는
backtest.py --optimize 가 남긴 results/backtest_orb/optimize_grid.json 을 읽습니다.

  01 누적손익        A 출발값 · B 출발값 · B 선택 값 (10/12~11/02)
  02 일별손익        B 선택 값
  03 개장별손익      B 선택 값, 학습 / 검증
  04 범위x익절-히트맵  B 학습 순손익 (나머지 값은 선택 값으로 고정)
  05 개장범위폭      서울 · 런던 · 뉴욕 첫 10분 범위 폭 분포
  06 기간비교        최근 16일 vs 앞쪽 15일
  07 하루예시        11/02 서울 개장 돌파 매도

사용법 (week03/ 에서, backtest.py --optimize 를 먼저 돌린 뒤):
  python3 plot_backtest.py          # results/charts/ 에 PNG 7장
"""
from __future__ import annotations

import json
import statistics
from dataclasses import replace
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.colors import LinearSegmentedColormap, TwoSlopeNorm
from matplotlib.ticker import FuncFormatter

import backtest as bt
import strategy_bb
import strategy_orb
from common import SESSION_START, in_trading_hours, minutes_of, ticks_between

HERE = Path(__file__).parent
OUT = HERE / "results" / "charts"

# 색: 한국 시장 관례대로 이익 빨강 · 손실 파랑. 두 계열 비교는 파랑·주황 (색약 검사 통과).
INK, INK2, GRID = "#0b0b0b", "#52514e", "#e4e3df"
BLUE, ORANGE, RED = "#2a78d6", "#eb6834", "#e34948"
TEST_SHADE = "#f1f0ec"

_INSTALLED = {f.name for f in font_manager.fontManager.ttflist}
KOREAN = [f for f in ("Apple SD Gothic Neo", "Malgun Gothic", "NanumGothic") if f in _INSTALLED]
plt.rcParams.update({
    "font.family": KOREAN + ["sans-serif"],
    "axes.unicode_minus": False,
    "font.size": 10,
    "axes.edgecolor": GRID, "axes.labelcolor": INK2, "axes.titlecolor": INK,
    "axes.titlesize": 12, "axes.titleweight": "bold", "axes.titlelocation": "left",
    "axes.spines.top": False, "axes.spines.right": False,
    "xtick.color": INK2, "ytick.color": INK2,
    "axes.grid": True, "axes.grid.axis": "y", "grid.color": GRID, "grid.linewidth": 0.8,
    "figure.facecolor": "white", "axes.facecolor": "white", "savefig.dpi": 160,
})
def usd(v: float, digits: int = 0) -> str:
    """+$285.0 / −$217.5 처럼 부호를 달러 기호 앞에."""
    return f"{'+' if v > 0 else '−' if v < 0 else ''}${abs(v):,.{digits}f}"


USD = FuncFormatter(lambda v, _: usd(v))

# 문서 8절의 선택 값
B_PICK = replace(strategy_orb.Params(), range_min=10, entry_min=60, buffer_ticks=0, target_r=2.0, hold_min=60)
A_START, B_START = strategy_bb.Params(), strategy_orb.Params()
A_TRAIN1 = replace(A_START, bar_min=15, band_k=2.5, rsi_buy=35.0, confirm=False, stop_atr=1.5, hold_min=60)
EARLY = ("20260921", "20261009")
DAY_START, DAY_SPAN = minutes_of(SESSION_START), minutes_of("240000") - minutes_of(SESSION_START)


def trading_days(minutes) -> list[str]:
    return sorted({m.date for m in minutes if in_trading_hours(m.time)})


def mark_test(ax, days: list[str], right: float) -> None:
    """검증 구간을 옅게 칠하고 경계에 이름을 답니다."""
    split = sum(d <= bt.TRAIN_END for d in days)
    ax.axvspan(split, right, color=TEST_SHADE, zorder=0, lw=0)
    ax.text(split - 0.15, 1.0, "학습 ◀", transform=ax.get_xaxis_transform(), ha="right", va="bottom", color=INK2, fontsize=9)
    ax.text(split + 0.15, 1.0, "▶ 검증", transform=ax.get_xaxis_transform(), ha="left", va="bottom", color=INK2, fontsize=9)


def day_ticks(ax, days: list[str], offset: float) -> None:
    ax.set_xticks([i + offset for i in range(len(days))][::2], [f"{d[4:6]}/{d[6:]}" for d in days][::2])


def save(fig, name: str) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    print(f"  저장: {path.relative_to(HERE)}")
    return path


def equity(minutes, days) -> Path:
    """누적 실현 손익. x 는 거래일 순번 + 그날 08:25~24:00 안의 위치 (주말 빈칸 없음)."""
    idx = {d: i for i, d in enumerate(days)}
    fig, ax = plt.subplots(figsize=(9, 4.4))
    mark_test(ax, days, len(days) + 1.6)
    for kind, p, who, color, style in (("orb", B_PICK, "B 선택 값 (범위 10분 · 익절 2.0배 · 60분)", RED, "-"),
                                       ("orb", B_START, "B 출발값 (범위 15분 · 익절 1.0배 · 120분)", RED, "--"),
                                       ("bb", A_START, "A 출발값 (2주차 채택값)", BLUE, "-")):
        xs, ys, total = [0.0], [0.0], 0.0
        for t in bt.run(minutes, kind, p)["trades"]:
            total += t["pnl"]
            at = idx.get(t["exit_date"], idx[t["entry_date"]] + 1)   # 자정 뒤 정리는 그날 끝으로
            frac = min(max(minutes_of(t["exit_time"]) - DAY_START, 0) / DAY_SPAN, 1.0) if t["exit_date"] in idx else 0.0
            xs.append(at + frac)
            ys.append(total)
        xs.append(len(days))
        ys.append(total)
        ax.step(xs, ys, where="post", color=color, lw=2, ls=style, label=who)
        ax.annotate(usd(total, 1), (xs[-1], total), xytext=(4, 0), textcoords="offset points",
                    va="center", color=INK, fontsize=9)
    ax.axhline(0, color=INK2, lw=0.8)
    ax.yaxis.set_major_formatter(USD)
    day_ticks(ax, days, 0.5)
    ax.set_xlim(0, len(days) + 1.6)
    ax.set_title("누적 실현 손익 — MKUX26 · 1계약, 슬리피지 왕복 2틱, 수수료 0", pad=18)
    ax.legend(loc="upper left", frameon=False, fontsize=8.5)
    return save(fig, "01-누적손익.png")


def daily(minutes, days) -> Path:
    by_day = bt.run(minutes, "orb", B_PICK)["summary"]["by_day"]
    vals = [by_day.get(d, 0) for d in days]
    fig, ax = plt.subplots(figsize=(9, 3.8))
    mark_test(ax, days, len(days))
    ax.bar([i + 0.5 for i in range(len(days))], vals, width=0.72, color=[RED if v > 0 else BLUE for v in vals])
    for i, v in enumerate(vals):
        if v:
            ax.annotate(f"{v:+.0f}", (i + 0.5, v), xytext=(0, 3 if v > 0 else -3), textcoords="offset points",
                        ha="center", va="bottom" if v > 0 else "top", fontsize=8, color=INK2)
    ax.axhline(0, color=INK2, lw=0.8)
    ax.yaxis.set_major_formatter(USD)
    day_ticks(ax, days, 0.5)
    ax.set_xlim(0, len(days))
    plus = sum(v > 0 for v in vals)
    ax.set_title(f"일별 순손익 — 후보 B 선택 값 · 이익 {plus}일, 손실 {sum(v < 0 for v in vals)}일", pad=18)
    return save(fig, "02-일별손익.png")


def by_window(minutes) -> Path:
    tr, te, _, _ = bt.split(bt.run(minutes, "orb", B_PICK), bt.TRAIN_END)
    names = [n for n, _ in B_PICK.windows]
    fig, ax = plt.subplots(figsize=(7, 3.8))
    w = 0.36
    for k, (s, who, color) in enumerate(((tr, "학습 10/12~10/26", BLUE), (te, "검증 10/27~11/02", ORANGE))):
        vals = [s["by_window"].get(n, {"pnl": 0})["pnl"] for n in names]
        cnts = [s["by_window"].get(n, {"trades": 0})["trades"] for n in names]
        xs = [i + (k - 0.5) * (w + 0.02) for i in range(len(names))]
        ax.bar(xs, vals, width=w, color=color, label=who)
        for x, v, c in zip(xs, vals, cnts):
            ax.annotate(f"{v:+.1f}\n{c}건", (x, v), xytext=(0, 3 if v >= 0 else -3), textcoords="offset points",
                        ha="center", va="bottom" if v >= 0 else "top", fontsize=8, color=INK2)
    ax.axhline(0, color=INK2, lw=0.8)
    ax.yaxis.set_major_formatter(USD)
    ax.set_xticks(range(len(names)), [f"{n} {o[:2]}:{o[2:4]}" for n, o in B_PICK.windows])
    ax.margins(y=0.25)
    ax.set_title("개장별 순손익 — 후보 B 선택 값", pad=10)
    ax.legend(loc="upper right", frameon=False, fontsize=8.5)
    return save(fig, "03-개장별손익.png")


def heatmap() -> Path:
    """기준 범위 × 익절 배수. 나머지(대기 60분 · 여유 0틱 · 시간 청산 60분)는 선택 값으로 고정."""
    rows = json.loads((HERE / "results" / "backtest_orb" / "optimize_grid.json").read_text(encoding="utf-8"))
    fixed = {"entry_min": B_PICK.entry_min, "buffer_ticks": B_PICK.buffer_ticks, "hold_min": B_PICK.hold_min}
    ranges, targets = [10, 15, 30], [0.5, 1.0, 1.5, 2.0]
    cell = {(r["range_min"], r["target_r"]): r for r in rows if all(r[k] == v for k, v in fixed.items())}
    z = [[cell[(rm, tr)]["net_pnl"] for tr in targets] for rm in ranges]
    lim = max(abs(v) for row in z for v in row)
    cmap = LinearSegmentedColormap.from_list("pl", [BLUE, "#f4f3ef", RED])
    fig, ax = plt.subplots(figsize=(6.4, 3.6))
    im = ax.imshow(z, cmap=cmap, norm=TwoSlopeNorm(0, -lim, lim), aspect="auto")
    for i, rm in enumerate(ranges):
        for j, tr in enumerate(targets):
            r = cell[(rm, tr)]
            pick = rm == B_PICK.range_min and tr == B_PICK.target_r
            ax.text(j, i, f"{r['net_pnl']:+.1f}\n{r['trades']}건" + (" ◀ 선택" if pick else ""), ha="center",
                    va="center", fontsize=8.5, color=INK, fontweight="bold" if pick else "normal")
    ax.set_xticks(range(len(targets)), [f"{t}배" for t in targets])
    ax.set_yticks(range(len(ranges)), [f"{r}분" for r in ranges])
    ax.set_xlabel("익절 = 범위 폭 × ")
    ax.set_ylabel("기준 범위")
    ax.grid(False)
    fig.colorbar(im, ax=ax, format=USD, shrink=0.85).outline.set_visible(False)
    ax.set_title("학습 순손익 (US$) — 후보 B · 대기 60분 · 여유 0틱 · 시간 청산 60분", fontsize=10.5, pad=10)
    return save(fig, "04-범위x익절-히트맵.png")


def range_widths(minutes, days) -> Path:
    """개장마다 첫 10분 범위 폭 (틱). 점 하나 = 하루, 굵은 가로선 = 중앙값."""
    fig, ax = plt.subplots(figsize=(7, 3.8))
    for i, (name, open_) in enumerate(B_PICK.windows):
        a = minutes_of(open_)
        widths = []
        for d in days:
            got = [m for m in minutes if m.date == d and a <= minutes_of(m.time) < a + B_PICK.range_min]
            if got:
                widths.append(ticks_between(max(m.high for m in got), min(m.low for m in got)))
        jitter = [((k * 7) % 11 - 5) / 30 for k in range(len(widths))]
        ax.scatter([i + j for j in jitter], widths, s=36, color=BLUE, alpha=0.75, edgecolor="white", lw=1.5, zorder=3)
        med = statistics.median(widths)
        ax.hlines(med, i - 0.3, i + 0.3, color=INK, lw=2.2, zorder=4)
        ax.annotate(f"중앙 {med:g}틱", (i + 0.32, med), va="center", fontsize=8.5, color=INK)
    ax.axhline(B_PICK.min_range_ticks, color=INK2, lw=0.9, ls=":")
    ax.annotate(f"잡음 필터 {B_PICK.min_range_ticks}틱 (이보다 좁으면 진입 안 함)", (-0.55, B_PICK.min_range_ticks),
                xytext=(0, 3), textcoords="offset points", va="bottom", fontsize=8, color=INK2)
    ax.set_xticks(range(3), [f"{n} {o[:2]}:{o[2:4]}" for n, o in B_PICK.windows])
    ax.set_xlim(-0.6, 2.6)
    ax.set_ylim(bottom=0)
    ax.set_ylabel("범위 폭 (틱, 1틱 ≈ 0.18원)")
    ax.set_title(f"개장 첫 10분 범위 폭 — 10/12~11/02, 하루 = 점 하나", pad=10)
    return save(fig, "05-개장범위폭.png")


def periods(recent, early) -> Path:
    cands = (("A 출발값", "bb", A_START), ("A 학습 1위", "bb", A_TRAIN1),
             ("B 출발값", "orb", B_START), ("B 선택 값", "orb", B_PICK))
    fig, ax = plt.subplots(figsize=(8, 4))
    w = 0.36
    for k, (mins, who, color) in enumerate(((recent, "10/12~11/02 (16일, 백테스트 기간)", BLUE),
                                            (early, "9/21~10/09 (15일, 거래 적던 때)", ORANGE))):
        xs = [i + (k - 0.5) * (w + 0.02) for i in range(len(cands))]
        vals = [bt.run(mins, kind, p)["summary"]["net_pnl"] for _, kind, p in cands]
        ax.bar(xs, vals, width=w, color=color, label=who)
        for x, v in zip(xs, vals):
            ax.annotate(f"{v:+.1f}", (x, v), xytext=(0, 3 if v >= 0 else -3), textcoords="offset points",
                        ha="center", va="bottom" if v >= 0 else "top", fontsize=8.5, color=INK2)
    ax.axhline(0, color=INK2, lw=0.8)
    ax.yaxis.set_major_formatter(USD)
    ax.set_xticks(range(len(cands)), [c[0] for c in cands])
    ax.margins(y=0.15)
    ax.set_title("기간에 따라 뒤집히는 우열 — 순손익 (수수료 0)", pad=10)
    ax.legend(loc="lower left", frameon=False, fontsize=8.5)
    return save(fig, "06-기간비교.png")


def example_day(minutes, date: str = "20261102") -> Path:
    """서울 개장 돌파 한 건: 기준 범위 · 신호 · 진입 · 손절 · 익절 · 청산."""
    res = bt.run(minutes, "orb", B_PICK)
    t = next(x for x in res["trades"] if x["entry_date"] == date and x["window"] == "서울")
    lo_t, hi_t = minutes_of("085000"), minutes_of("103000")
    day = [m for m in minutes if m.date == date and lo_t <= minutes_of(m.time) <= hi_t]
    x = [minutes_of(m.time) for m in day]
    a = minutes_of("090000")
    rng = [m for m in day if a <= minutes_of(m.time) < a + B_PICK.range_min]
    r_hi, r_lo = max(m.high for m in rng), min(m.low for m in rng)
    fig, ax = plt.subplots(figsize=(9, 4.4))
    ax.vlines(x, [m.low for m in day], [m.high for m in day], color=INK2, lw=1)
    ax.plot(x, [m.close for m in day], color=INK, lw=1.4, marker="o", ms=2.5, label="1분봉 종가 (세로선 = 고가~저가)")
    ax.fill_between([a, a + B_PICK.range_min - 1], r_lo, r_hi, color=TEST_SHADE, zorder=0)
    ax.hlines([r_hi, r_lo], a, hi_t, color=INK2, lw=0.9, ls="--")
    ax.annotate(f"기준 범위 09:00~09:09  {r_lo:.4f}~{r_hi:.4f} ({ticks_between(r_hi, r_lo)}틱)", (a, r_hi),
                xytext=(0, 6), textcoords="offset points", fontsize=8.5, color=INK2)
    e, xit = minutes_of(t["entry_time"]), minutes_of(t["exit_time"])
    ax.hlines(t["stop"], e, xit, color=BLUE, lw=1.6)
    ax.annotate(f"손절 {t['stop']:.4f} (범위 위 끝)", (xit, t["stop"]), xytext=(4, 4), textcoords="offset points",
                va="bottom", ha="left", fontsize=8.5, color=INK)
    ax.hlines(t["target"], e, xit, color=RED, lw=1.6)
    ax.annotate(f"익절 목표 {t['target']:.4f} (범위 폭 × 2)", (e, t["target"]), xytext=(0, 4), textcoords="offset points",
                va="bottom", fontsize=8.5, color=INK)
    ax.scatter([e], [t["entry"]], marker="v", s=90, color=BLUE, zorder=5, edgecolor="white", lw=1.5)
    ax.annotate(f"매도 진입 {t['entry_time'][:2]}:{t['entry_time'][2:4]}  {t['entry']:.4f}", (e, t["entry"]),
                xytext=(-8, -12), textcoords="offset points", ha="right", va="top", fontsize=8.5, color=INK)
    ax.scatter([xit], [t["exit"]], marker="o", s=70, color=RED, zorder=5, edgecolor="white", lw=1.5)
    ax.annotate(f"익절 청산 {t['exit_time'][:2]}:{t['exit_time'][2:4]}  {t['exit']:.4f}  {t['ticks']:+d}틱 = {usd(t['pnl'], 1)}",
                (xit, t["exit"]), xytext=(xit + 14, t["exit"] - 0.00035), textcoords="data", va="center", fontsize=8.5,
                color=INK, arrowprops={"arrowstyle": "-", "color": INK2, "lw": 0.8})
    ticks = list(range(lo_t, hi_t + 1, 15))
    ax.set_xticks(ticks, [f"{v // 60:02d}:{v % 60:02d}" for v in ticks])
    ax.set_ylabel("MKU 가격 (US$ / 1,000원)")
    ax.text(0.01, 0.02, "↓ 아래로 갈수록 원화 약세 (환율 상승)", transform=ax.transAxes, fontsize=8.5, color=INK2)
    ax.legend(loc="upper right", frameon=False, fontsize=8.5)
    ax.set_title(f"하루 예시 — {date[4:6]}/{date[6:]} 서울 개장 범위 아래로 돌파 → 매도 (원화 약세 쪽)", pad=10)
    return save(fig, "07-하루예시.png")


def main() -> None:
    recent = bt.load_minutes()
    early = bt.load_minutes(start=EARLY[0], end=EARLY[1])
    days = trading_days(recent)
    equity(recent, days)
    daily(recent, days)
    by_window(recent)
    heatmap()
    range_widths(recent, days)
    periods(recent, early)
    example_day(recent)


if __name__ == "__main__":
    main()
