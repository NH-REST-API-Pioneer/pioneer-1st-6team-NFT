"""볼린저+RSI 백테스트 결과 JSON 을 차트 이미지(PNG)로 그립니다 — README·전략 근거 문서에 넣는 그림.

backtest_bb.py 가 results/backtest_bb/ 에 남긴 파일만 읽습니다. 손익을 다시 셈하지 않고 거래별 pnl 을
더하기만 합니다. 색·글꼴·검증 구간 표시는 VWAP 후보의 plot_backtest.py 것을 그대로 가져다 써서 두 후보의
그림이 같은 모양입니다.

  adopted_all.json · standard_all.json  → 01 누적 손익, 02 일별 손익, 05 하루 예시 (밴드·RSI·체결)
  optimize_grid.json                    → 03 밴드 폭 × RSI 문턱 히트맵 (학습 순손익)
  optimize_result.json                  → 04 정석 · 학습 1위 · 채택 · 채택(RSI 끔) 의 학습/검증 순손익

사용법 (week02/ 에서, backtest_bb.py 를 먼저 돌린 뒤):
  python3 plot_backtest_bb.py          # results/charts_bb/ 에 PNG 5장
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap, TwoSlopeNorm

from backtest import load_minutes
from backtest_bb import label
from plot_backtest import (BLUE, GRID, INK, INK2, ORANGE, RED, TRAIN_END, WON, day_ticks,  # 같은 색·글꼴·구간 표시
                           mark_test)
from strategy import minutes_of
from strategy_bb import BBRSIReversion, Params

HERE = Path(__file__).parent
SRC = HERE / "results" / "backtest_bb"
OUT = HERE / "results" / "charts_bb"


def load(name: str) -> dict:
    return json.loads((SRC / name).read_text(encoding="utf-8"))


def all_days() -> list[str]:
    r = load("optimize_result.json")
    return r["train_days"] + r["test_days"]


def short(p: dict) -> str:
    rsi = "RSI 끔" if p["rsi_buy"] >= 100 else f"RSI {p['rsi_buy']:.0f}/{100 - p['rsi_buy']:.0f}"
    return f"{p['bar_min']}분봉·{p['band_k']}σ·{rsi}·{'복귀' if p['confirm'] else '즉시'}·ATR×{p['stop_atr']}·{p['hold_min']}분"


def equity(days: list[str]) -> Path:
    """누적 실현 손익. x 는 거래일 순번 + 그날 장중 위치 (주말·휴일 빈칸 없음)."""
    p = Params()
    start, span = minutes_of(p.session_start), minutes_of(p.session_end) - minutes_of(p.session_start)
    idx = {d: i for i, d in enumerate(days)}
    fig, ax = plt.subplots(figsize=(9, 4.2))
    mark_test(ax, days, 0, len(days))
    for name, who, color in (("adopted_all.json", "채택", RED), ("standard_all.json", "정석", BLUE)):
        res = load(name)
        xs, ys, total = [0.0], [0.0], 0
        for t in res["trades"]:
            total += t["pnl"]
            xs.append(idx[t["exit_date"]] + (minutes_of(t["exit_time"]) - start) / span)
            ys.append(total)
        xs.append(len(days))
        ys.append(total)
        ax.step(xs, ys, where="post", color=color, lw=2, label=f"{who} ({short(res['params'])})")
        ax.annotate(f"{total:+,}원", (xs[-1], total), xytext=(4, 0), textcoords="offset points",
                    va="center", color=INK, fontsize=9)
    ax.axhline(0, color=INK2, lw=0.8)
    ax.yaxis.set_major_formatter(WON)
    day_ticks(ax, days, 0.5)
    ax.set_xlim(0, len(days) + 1.6)
    ax.set_title("누적 실현 손익 — 볼린저+RSI · 1계약, 슬리피지 왕복 2틱, 수수료 0원", pad=18)
    ax.legend(loc="upper left", frameon=False, fontsize=8.5)
    return save(fig, "01-누적손익.png")


def daily(days: list[str]) -> Path:
    by_day = load("adopted_all.json")["summary"]["by_day"]
    vals = [by_day.get(d, 0) for d in days]
    fig, ax = plt.subplots(figsize=(9, 3.6))
    mark_test(ax, days, -0.5, len(days) - 0.5)
    ax.bar(range(len(days)), vals, width=0.7, color=[RED if v > 0 else BLUE for v in vals], zorder=2)
    for i, v in enumerate(vals):
        if v == 0:
            ax.text(i, 0, "거래\n없음", ha="center", va="bottom", color=INK2, fontsize=8)
    ax.axhline(0, color=INK2, lw=0.8)
    ax.yaxis.set_major_formatter(WON)
    day_ticks(ax, days, 0)
    ax.set_xlim(-0.6, len(days) - 0.4)
    win, lose = sum(v > 0 for v in vals), sum(v < 0 for v in vals)
    ax.set_title(f"채택 값 일별 순손익 — 이익 {win}일(빨강) · 손실 {lose}일(파랑)", pad=18)
    return save(fig, "02-일별손익.png")


def heatmap() -> Path:
    """채택값의 나머지 네 값(판정 봉·진입·손절·시간)을 고정하고 밴드 폭 × RSI 문턱만 바꾼 학습 순손익."""
    p = Params()
    r = load("optimize_result.json")
    min_trades = r["min_trades"]
    rows = [x for x in load("optimize_grid.json")
            if x["bar_min"] == p.bar_min and x["confirm"] == p.confirm and x["stop_atr"] == p.stop_atr and x["hold_min"] == p.hold_min]
    bands = sorted({x["band_k"] for x in rows})
    levels = sorted({x["rsi_buy"] for x in rows})
    cell = {(x["band_k"], x["rsi_buy"]): x for x in rows}
    z = [[cell[(b, l)]["net_pnl"] for l in levels] for b in bands]
    lim = max(abs(v) for row in z for v in row)
    cmap = LinearSegmentedColormap.from_list("pnl", [BLUE, "#f1f0ec", RED])
    fig, ax = plt.subplots(figsize=(6.8, 4.2))
    ax.imshow(z, cmap=cmap, norm=TwoSlopeNorm(0, -lim, lim), aspect="auto")
    for i, b in enumerate(bands):
        for j, l in enumerate(levels):
            x = cell[(b, l)]
            chosen = (b, l) == (p.band_k, p.rsi_buy)
            few = x["trades"] < min_trades
            ax.text(j, i, f"{x['net_pnl'] / 10000:+.1f}만\n{x['trades']}건" + (" · 후보 아님" if few else ""),
                    ha="center", va="center", fontsize=9, color=INK2 if few else INK,
                    fontweight="bold" if chosen else "normal")
            if chosen:
                ax.add_patch(plt.Rectangle((j - 0.48, i - 0.48), 0.96, 0.96, fill=False, ec=INK, lw=2))
    ax.set_xticks(range(len(levels)), ["RSI 끔" if l >= 100 else f"RSI {l:.0f}/{100 - l:.0f}" for l in levels])
    ax.set_yticks(range(len(bands)), [f"{b}σ" for b in bands])
    ax.set_xlabel("RSI 문턱 (매수 ≤ / 매도 ≥)")
    ax.set_ylabel("밴드 폭")
    ax.grid(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_visible(False)
    fixed = f"{p.bar_min}분봉·{'복귀 확인' if p.confirm else '즉시 진입'}·ATR×{p.stop_atr}·{p.hold_min}분 고정"
    ax.set_title(f"학습 기간 순손익 — {fixed}\n(굵은 테두리 = 채택 · 거래 {min_trades}건 미만은 후보 아님)", fontsize=11)
    return save(fig, "03-밴드xRSI-히트맵.png")


def compare() -> Path:
    r = load("optimize_result.json")
    groups = [("정석", r["baseline"]), ("학습 순손익 1위", r["picked"]["학습1위"]),
              ("채택\n(이웃 평균 1위)", r["picked"]["채택"]), ("채택에서\nRSI 만 끔", r["picked"]["채택_RSI끔"])]
    names = [g[0] for g in groups]
    train = [g[1]["train"]["net_pnl"] for g in groups]
    test = [g[1]["test"]["net_pnl"] for g in groups]
    fig, ax = plt.subplots(figsize=(8.5, 3.9))
    w = 0.36
    xs = range(len(names))
    for off, vals, who, color in ((-w / 2, train, f"학습 ({len(r['train_days'])}일)", BLUE),
                                  (w / 2, test, f"검증 ({len(r['test_days'])}일)", ORANGE)):
        bars = ax.bar([x + off for x in xs], vals, width=w - 0.03, color=color, label=who, zorder=2)
        for b, v in zip(bars, vals):
            ax.annotate(f"{v / 10000:+.1f}만", (b.get_x() + b.get_width() / 2, v), xytext=(0, 3 if v >= 0 else -3),
                        textcoords="offset points", ha="center", va="bottom" if v >= 0 else "top", fontsize=9, color=INK)
    ax.axhline(0, color=INK2, lw=0.8)
    ax.yaxis.set_major_formatter(WON)
    ax.set_xticks(list(xs), names)
    ax.margins(y=0.18)
    ax.set_title("학습에서 고른 값이 검증에서도 버텼나 — 순손익", pad=10)
    ax.legend(loc="upper right", frameon=False)
    return save(fig, "04-학습vs검증.png")


def day_example(days: list[str]) -> Path:
    """검증 기간에서 거래가 가장 많았던 하루 — 1분봉 종가 위에 판정 봉의 밴드, 아래에 RSI, 체결 표지."""
    res = load("adopted_all.json")
    p = Params(**res["params"])
    trades = res["trades"]
    count = Counter(t["entry_date"] for t in trades)
    test_days = [d for d in days if d > TRAIN_END]
    day = max(test_days, key=lambda d: (count.get(d, 0), d))

    strat = BBRSIReversion(p)
    bars = []
    minutes = load_minutes()
    for m in minutes:                       # 지표가 날을 넘겨 이어지므로 처음부터 흘립니다
        if m.date > day:
            break
        for s in strat.on_minute(m):
            if s.bar.date == day:
                bars.append(s.bar)
    mins = [m for m in minutes if m.date == day and p.session_start <= m.time < p.session_end]
    t0 = minutes_of(p.session_start)
    xm = [minutes_of(m.time) - t0 for m in mins]
    xb = [minutes_of(b.end) + 1 - t0 for b in bars]     # 판정 봉이 닫힌 시각

    fig, (ax, ax2) = plt.subplots(2, 1, figsize=(9.5, 6), sharex=True, gridspec_kw={"height_ratios": [3, 1.1]})
    ax.plot(xm, [m.close for m in mins], color=INK2, lw=0.9, label="1분봉 종가")
    for key, style, who in (("upper", "-", f"밴드 ±{p.band_k}σ"), ("lower", "-", None), ("middle", "--", "중심선 (익절 목표)")):
        ys = [getattr(b, key) for b in bars]
        ax.step(xb, ys, where="post", color=BLUE, lw=1.1, ls=style, alpha=0.9, label=who)
    for t in trades:
        if t["entry_date"] != day:
            continue
        xe, xx = minutes_of(t["entry_time"]) - t0, minutes_of(t["exit_time"]) - t0
        up = t["side"] == "buy"
        color = RED if t["pnl"] > 0 else BLUE
        ax.plot([xe, xx], [t["entry"], t["exit"]], color=color, lw=1.4, alpha=0.8)
        ax.scatter([xe], [t["entry"]], marker="^" if up else "v", s=70, color=RED if up else BLUE, zorder=5, edgecolor="white")
        ax.scatter([xx], [t["exit"]], marker="x", s=55, color=color, zorder=5)
        ax.annotate(f"{t['reason']} {t['pnl'] / 1000:+.0f}천", (xx, t["exit"]), xytext=(4, 7 if up else -11),
                    textcoords="offset points", fontsize=7.5, color=INK,
                    bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="none", alpha=0.85), zorder=6)
    ax.set_ylabel("가격 (원)")
    ax.legend(loc="upper left", frameon=False, fontsize=8.5, ncol=3)
    n = count.get(day, 0)
    pnl = sum(t["pnl"] for t in trades if t["entry_date"] == day)
    ax.set_title(f"{day[:4]}/{day[4:6]}/{day[6:]} 하루 — 채택 값 · 거래 {n}건 · 순손익 {pnl:+,}원 (▲매수 ▼매도 ✕청산)", pad=10)

    ax2.step(xb, [b.rsi for b in bars], where="post", color=ORANGE, lw=1.3)
    ax2.axhline(p.rsi_buy, color=RED, lw=0.8, ls="--")
    ax2.axhline(p.rsi_sell, color=BLUE, lw=0.8, ls="--")
    ax2.axhspan(0, p.rsi_buy, color=RED, alpha=0.06, lw=0)
    ax2.axhspan(p.rsi_sell, 100, color=BLUE, alpha=0.06, lw=0)
    ax2.set_ylim(0, 100)
    ax2.set_ylabel(f"RSI({p.rsi_period})")
    ax2.text(1, p.rsi_buy + 2, f"매수 문턱 {p.rsi_buy:.0f}", fontsize=8, color=INK2)
    ax2.text(1, p.rsi_sell + 2, f"매도 문턱 {p.rsi_sell:.0f}", fontsize=8, color=INK2)
    ticks = list(range(0, minutes_of(p.session_end) - t0 + 1, 30))
    ax2.set_xticks(ticks, [f"{(t0 + t) // 60:02d}:{(t0 + t) % 60:02d}" for t in ticks])
    ax2.set_xlim(0, minutes_of(p.session_end) - t0)
    return save(fig, "05-하루예시.png")


def save(fig, name: str) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    print(f"  저장: {path.relative_to(HERE)}")
    return path


def main() -> None:
    days = all_days()
    equity(days)
    daily(days)
    heatmap()
    compare()
    day_example(days)


if __name__ == "__main__":
    main()
