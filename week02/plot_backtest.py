"""백테스트 결과 JSON 을 차트 이미지(PNG)로 그립니다 — README·전략 근거 문서에 넣는 그림.

backtest.py 가 results/backtest/ 에 남긴 파일만 읽습니다. 손익을 다시 셈하지 않고 거래별 pnl 을 더하기만 합니다.

  adopted_all.json · standard_all.json  → 누적 손익, 일별 손익
  optimize_grid.json                    → 밴드 폭 × 손절 배수 히트맵 (학습 순손익)
  optimize_result.json                  → 정석 · 학습 1위 · 채택의 학습/검증 순손익

사용법 (week02/ 에서, backtest.py 를 먼저 돌린 뒤):
  python3 plot_backtest.py          # results/charts/ 에 PNG 4장
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.colors import LinearSegmentedColormap, TwoSlopeNorm
from matplotlib.ticker import FuncFormatter

from strategy import Params, minutes_of

HERE = Path(__file__).parent
SRC = HERE / "results" / "backtest"
OUT = HERE / "results" / "charts"
TRAIN_END = "20261020"   # backtest.py --train-end 와 같은 값
MIN_TRADES = 15          # 최적화 때 쓴 --min-trades (이보다 적은 조합은 후보에서 뺐음)

# 색: 한국 시장 관례대로 이익 빨강 · 손실 파랑. 두 계열 비교는 파랑·주황.
INK, INK2, GRID = "#0b0b0b", "#52514e", "#e4e3df"
BLUE, ORANGE, RED = "#2a78d6", "#eb6834", "#e34948"
TEST_SHADE = "#f1f0ec"

_INSTALLED = {f.name for f in font_manager.fontManager.ttflist}
# 맥·윈도우 어느 쪽에서 돌려도 한글이 나오게, 깔린 폰트 중 첫 번째를 씁니다
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
WON = FuncFormatter(lambda v, _: f"{v / 10000:+,.0f}만" if v else "0")


def load(name: str) -> dict:
    return json.loads((SRC / name).read_text(encoding="utf-8"))


def all_days() -> list[str]:
    r = load("optimize_result.json")
    return r["train_days"] + r["test_days"]


def mark_test(ax, days: list[str], left: float, right: float) -> None:
    """검증 구간을 옅게 칠하고 경계에 이름을 답니다."""
    split = sum(d <= TRAIN_END for d in days) + left
    ax.axvspan(split, right, color=TEST_SHADE, zorder=0, lw=0)
    ax.text(split - 0.15, 1.0, "학습 ◀", transform=ax.get_xaxis_transform(), ha="right", va="bottom", color=INK2, fontsize=9)
    ax.text(split + 0.15, 1.0, "▶ 검증", transform=ax.get_xaxis_transform(), ha="left", va="bottom", color=INK2, fontsize=9)


def day_ticks(ax, days: list[str], offset: float) -> None:
    ax.set_xticks([i + offset for i in range(len(days))][::2], [f"{d[4:6]}/{d[6:]}" for d in days][::2])


def equity(days: list[str]) -> Path:
    """누적 실현 손익. x 는 거래일 순번 + 그날 장중 위치 (주말·휴일 빈칸 없음)."""
    p = Params()
    start, span = minutes_of(p.session_start), minutes_of(p.session_end) - minutes_of(p.session_start)
    idx = {d: i for i, d in enumerate(days)}
    fig, ax = plt.subplots(figsize=(9, 4.2))
    mark_test(ax, days, 0, len(days))
    for name, label, color in (("adopted_all.json", "채택 (3분봉·1.5σ·ATR×3.0·120분)", RED),
                               ("standard_all.json", "정석 (5분봉·2σ·ATR×1.5·60분)", BLUE)):
        trades = load(name)["trades"]
        xs, ys, total = [0.0], [0.0], 0
        for t in trades:
            total += t["pnl"]
            xs.append(idx[t["exit_date"]] + (minutes_of(t["exit_time"]) - start) / span)
            ys.append(total)
        xs.append(len(days))
        ys.append(total)
        ax.step(xs, ys, where="post", color=color, lw=2, label=label)
        ax.annotate(f"{total:+,}원", (xs[-1], total), xytext=(4, 0), textcoords="offset points",
                    va="center", color=INK, fontsize=9)
    ax.axhline(0, color=INK2, lw=0.8)
    ax.yaxis.set_major_formatter(WON)
    day_ticks(ax, days, 0.5)
    ax.set_xlim(0, len(days) + 1.6)
    ax.set_title("누적 실현 손익 — 1계약, 슬리피지 왕복 2틱, 수수료 0원", pad=18)
    ax.legend(loc="upper left", frameon=False)
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
    win = sum(v > 0 for v in vals)
    lose = sum(v < 0 for v in vals)
    ax.set_title(f"채택 값 일별 순손익 — 이익 {win}일(빨강) · 손실 {lose}일(파랑)", pad=18)
    return save(fig, "02-일별손익.png")


def heatmap() -> Path:
    """채택값의 나머지 세 값(3분봉·복귀 확인·120분)을 고정하고 밴드 폭 × 손절 배수만 바꾼 학습 순손익."""
    p = Params()
    rows = [r for r in load("optimize_grid.json")
            if r["bar_min"] == p.bar_min and r["confirm"] == p.confirm and r["hold_min"] == p.hold_min]
    bands = sorted({r["band_k"] for r in rows})
    stops = sorted({r["stop_atr"] for r in rows})
    cell = {(r["band_k"], r["stop_atr"]): r for r in rows}
    z = [[cell[(b, s)]["net_pnl"] for s in stops] for b in bands]
    lim = max(abs(v) for row in z for v in row)
    cmap = LinearSegmentedColormap.from_list("pnl", [BLUE, "#f1f0ec", RED])
    fig, ax = plt.subplots(figsize=(6.4, 4.2))
    ax.imshow(z, cmap=cmap, norm=TwoSlopeNorm(0, -lim, lim), aspect="auto")
    for i, b in enumerate(bands):
        for j, s in enumerate(stops):
            r = cell[(b, s)]
            chosen = (b, s) == (p.band_k, p.stop_atr)
            few = r["trades"] < MIN_TRADES
            ax.text(j, i, f"{r['net_pnl'] / 10000:+.1f}만\n{r['trades']}건" + (" · 후보 아님" if few else ""),
                    ha="center", va="center", fontsize=9, color=INK2 if few else INK,
                    fontweight="bold" if chosen else "normal")
            if chosen:
                ax.add_patch(plt.Rectangle((j - 0.48, i - 0.48), 0.96, 0.96, fill=False, ec=INK, lw=2))
    ax.set_xticks(range(len(stops)), [f"ATR×{s}" for s in stops])
    ax.set_yticks(range(len(bands)), [f"{b}σ" for b in bands])
    ax.set_xlabel("손절 배수")
    ax.set_ylabel("밴드 폭")
    ax.grid(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_visible(False)
    ax.set_title(f"학습 기간 순손익 — 3분봉·복귀 확인·120분 고정\n(굵은 테두리 = 채택 · 거래 {MIN_TRADES}건 미만은 후보 아님)", fontsize=11)
    return save(fig, "03-밴드x손절-히트맵.png")


def compare() -> Path:
    r = load("optimize_result.json")
    names = ["정석", "학습 순손익 1위", "채택\n(이웃 평균 1위)"]
    train = [r["baseline"]["train"]["net_pnl"], r["picked"]["학습1위"]["train"]["net_pnl"], r["picked"]["채택"]["train"]["net_pnl"]]
    test = [r["baseline"]["test"]["net_pnl"], r["picked"]["학습1위"]["test"]["net_pnl"], r["picked"]["채택"]["test"]["net_pnl"]]
    fig, ax = plt.subplots(figsize=(7.5, 3.8))
    w = 0.36
    xs = range(len(names))
    for off, vals, label, color in ((-w / 2, train, f"학습 ({len(r['train_days'])}일)", BLUE),
                                    (w / 2, test, f"검증 ({len(r['test_days'])}일)", ORANGE)):
        bars = ax.bar([x + off for x in xs], vals, width=w - 0.03, color=color, label=label, zorder=2)
        for b, v in zip(bars, vals):
            ax.annotate(f"{v / 10000:+.1f}만", (b.get_x() + b.get_width() / 2, v), xytext=(0, 3 if v >= 0 else -3),
                        textcoords="offset points", ha="center", va="bottom" if v >= 0 else "top", fontsize=9, color=INK)
    ax.axhline(0, color=INK2, lw=0.8)
    ax.yaxis.set_major_formatter(WON)
    ax.set_xticks(list(xs), names)
    ax.margins(y=0.15)
    ax.set_title("학습에서 고른 값이 검증에서도 버텼나 — 순손익", pad=10)
    ax.legend(loc="upper left", frameon=False)
    return save(fig, "04-학습vs검증.png")


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


if __name__ == "__main__":
    main()
