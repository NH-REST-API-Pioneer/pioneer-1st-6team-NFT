"""장중(분~시간) 신호 후보 검정 — 학습·검증 구간만 본다. 시험 구간(10/01~)은 건드리지 않음.

데이터: week05/vol_regime/data/bars/*.csv (CME 연결선물 5분봉, KST 봉 시작), 정리 규칙은 vol_regime/data.py 그대로.
세션 기준 신호는 뉴욕 시각으로 정의 (서머타임 자동 반영).

신호마다 종목-날짜 한 건(사건)씩 x(과거 구간 수익률), y(미래 구간 수익률)를 만들고
  1) 회귀 기울기 — y/σ 를 x/σ 에 (σ = 종목별 직전 20일 일간 변동성), 날짜 군집 t
  2) 부호 매매 — sign(x) 방향으로 y 구간 보유, 거래당 bp (비용 전), 날짜 단위 t
  3) 비용 — 왕복 1틱 + 마이크로 왕복 $4 를 bp 로
를 학습·검증에 따로 낸다.

실행: python3 scan.py   → results/scan_summary.md, results/scan_by_symbol.csv
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
VR = HERE.parent / "vol_regime"
sys.path.insert(0, str(VR))
import config as vcfg  # noqa: E402
from data import load_bars  # noqa: E402

SYMS = ["ES", "NQ", "CL", "GC", "HG", "6E", "6J", "ZN"]
TRAIN = ("2026-05-01", "2026-08-31")
VAL = ("2026-09-01", "2026-09-30")
TEST_START = pd.Timestamp("2026-10-01")
NY = "America/New_York"
OUT = HERE / "results"


def load(sym: str) -> pd.DataFrame:
    df = load_bars(sym)
    df = df[df.index < TEST_START]                      # 시험 구간 잘라냄
    ny = df.index.tz_localize("Asia/Seoul").tz_convert(NY)
    df["ny"] = ny
    df["nyd"] = ny.tz_localize(None).normalize()        # 뉴욕 날짜
    df["nyt"] = ny.hour * 60 + ny.minute                # 뉴욕 분
    df["lc"] = np.log(df["c"])
    df["lo"] = np.log(df["o"])
    return df


def price_at(df: pd.DataFrame, minute: int, field: str = "lo") -> pd.Series:
    """뉴욕 날짜별, 봉 시작이 minute 인 봉의 로그 시가(lo) 또는 종가(lc). 없으면 빠짐."""
    s = df[df["nyt"] == minute]
    return s.groupby("nyd")[field].first()


def ret(df, a: int, b: int, prev_day_a: bool = False) -> pd.Series:
    """뉴욕 a분 봉 시가 → b분 봉 시가 로그 수익률 (b 봉 시작 = 보유 끝). prev_day_a: a 는 전날."""
    pa, pb = price_at(df, a), price_at(df, b)
    if prev_day_a:
        pa = pa.copy()
        pa.index = pa.index + pd.Timedelta(days=1)
    return (pb - pa).dropna()


def daily_sigma(df) -> pd.Series:
    """뉴욕 날짜별 직전 20일 일간 수익률 표준편차 (당일 제외)."""
    d = df.groupby("nyd")["r"].apply(lambda r: np.sqrt(np.nansum(r ** 2)))
    return d.rolling(20, min_periods=10).mean().shift(1)


def cost_bp(sym: str, df) -> float:
    tick, pv, ratio = vcfg.SPEC[sym]
    px = df["c"].median()
    return (tick + 2 * vcfg.FEE_SIDE / (pv / ratio)) / px * 1e4


# ---------- 신호 정의: (이름, 설명, 함수(df) -> DataFrame[x, y]) ----------
def s_overnight(df):
    # 전날 18:00 → 09:30 (아시아·유럽 동안) 이 09:30 → 10:30 을 맞히나
    return pd.DataFrame({"x": ret(df, 18 * 60, 9 * 60 + 30, prev_day_a=True), "y": ret(df, 570, 630)})


def s_first30(df):
    # Gao 외(2018): 첫 30분 → 나머지 (10:00 → 15:30)
    return pd.DataFrame({"x": ret(df, 570, 600), "y": ret(df, 600, 930)})


def s_first30_short(df):
    # 첫 30분 → 다음 1시간 (10:00 → 11:00)
    return pd.DataFrame({"x": ret(df, 570, 600), "y": ret(df, 600, 660)})


def s_asia_europe(df):
    # 아시아 (19:00 → 03:00 NY ≈ KST 08~16시) 이 유럽 오전 (03:00 → 08:00) 을 맞히나
    return pd.DataFrame({"x": ret(df, 19 * 60, 180, prev_day_a=True), "y": ret(df, 180, 480)})


def s_london_open(df):
    # 런던 개장 30분 (03:00 → 03:30) → 03:30 → 08:00
    return pd.DataFrame({"x": ret(df, 180, 210), "y": ret(df, 210, 480)})


def s_release(df):
    # 08:30 봉 (지표 발표) → 08:35 → 09:30. 발표 여부는 모름 → 크기 조건 버전은 따로
    return pd.DataFrame({"x": ret(df, 510, 515), "y": ret(df, 515, 570)})


def s_prev_day(df):
    # 전날 09:30 → 16:00 (정규장) 방향이 오늘 09:30 → 16:00 을 맞히나
    x = ret(df, 570, 960)
    x.index = x.index + pd.Timedelta(days=1)
    return pd.DataFrame({"x": x, "y": ret(df, 570, 960)})


def s_late_day(df):
    # Baltussen 외(2021) 형태: 09:30 → 15:30 이 마지막 30분 (15:30 → 16:00) 을 맞히나 (모의 시간엔 못 씀, 참고)
    return pd.DataFrame({"x": ret(df, 570, 930), "y": ret(df, 930, 960)})


SIGNALS = [
    ("overnight", "전날 18:00→09:30 ⇒ 09:30→10:30", s_overnight),
    ("first30_rest", "09:30→10:00 ⇒ 10:00→15:30", s_first30),
    ("first30_1h", "09:30→10:00 ⇒ 10:00→11:00", s_first30_short),
    ("asia_europe", "전날 19:00→03:00 ⇒ 03:00→08:00", s_asia_europe),
    ("london_open", "03:00→03:30 ⇒ 03:30→08:00", s_london_open),
    ("release_0830", "08:30→08:35 ⇒ 08:35→09:30", s_release),
    ("prev_day", "전날 09:30→16:00 ⇒ 09:30→16:00", s_prev_day),
    ("late_30", "09:30→15:30 ⇒ 15:30→16:00 (참고)", s_late_day),
]


def same_time(df, lookback: int = 5) -> pd.DataFrame:
    """Heston 외(2010): 30분 칸마다 직전 lookback 일 같은 칸 평균 수익률 ⇒ 오늘 같은 칸. 칸마다 한 건."""
    d = df.copy()
    d["slot"] = d["nyt"] // 30
    g = d.groupby(["nyd", "slot"])
    rr = (g["lc"].last() - g["lo"].first()).rename("y").reset_index()
    n = g.size().reset_index(name="n")
    rr = rr[n["n"] >= 5]
    # 칸마다 그 칸이 있었던 날만 놓고 직전 lookback 번 평균 (당일 제외)
    rr = rr.sort_values(["slot", "nyd"])
    rr["x"] = rr.groupby("slot")["y"].transform(lambda v: v.rolling(lookback, min_periods=lookback).mean().shift(1))
    out = rr.dropna(subset=["x", "y"]).set_index("nyd")[["x", "y"]]
    return out


def clustered_t(x: np.ndarray, y: np.ndarray, g: np.ndarray) -> tuple[float, float]:
    """y = a + b x, 날짜 군집 표준오차. (b, t)"""
    X = np.column_stack([np.ones_like(x), x])
    XtX_inv = np.linalg.inv(X.T @ X)
    b = XtX_inv @ X.T @ y
    e = y - X @ b
    meat = np.zeros((2, 2))
    for k in np.unique(g):
        m = g == k
        s = X[m].T @ e[m]
        meat += np.outer(s, s)
    G = len(np.unique(g))
    V = XtX_inv @ meat @ XtX_inv * G / max(G - 1, 1)
    return b[1], b[1] / np.sqrt(V[1, 1])


def evaluate(ev: pd.DataFrame) -> dict:
    """ev: 열 date sym x y z_x z_y cost."""
    if len(ev) < 20:
        return {"n": len(ev)}
    b, t = clustered_t(ev["zx"].values, ev["zy"].values, ev["date"].values.astype("datetime64[D]").astype(int))
    pnl = np.sign(ev["x"]) * ev["y"] * 1e4                # bp, 비용 전
    daily = pnl.groupby(ev["date"]).mean()
    tt = daily.mean() / (daily.std(ddof=1) / np.sqrt(len(daily))) if len(daily) > 2 else np.nan
    return {"n": len(ev), "days": ev["date"].nunique(), "beta": b, "t_beta": t,
            "gross_bp": pnl.mean(), "t_gross": tt, "cost_bp": ev["cost"].mean(),
            "net_bp": pnl.mean() - ev["cost"].mean(), "hit": (pnl > 0).mean()}


def main() -> None:
    OUT.mkdir(exist_ok=True)
    data = {s: load(s) for s in SYMS}
    sig = {s: daily_sigma(d) for s, d in data.items()}
    costs = {s: cost_bp(s, d) for s, d in data.items()}

    builders = SIGNALS + [("same_time_5d", "같은 30분 칸 직전 5일 평균 ⇒ 오늘", lambda d: same_time(d, 5)),
                          ("same_time_20d", "같은 30분 칸 직전 20일 평균 ⇒ 오늘", lambda d: same_time(d, 20))]
    rows, sym_rows = [], []
    for name, desc, fn in builders:
        evs = []
        for s, d in data.items():
            e = fn(d).dropna()
            e = e[(e["x"] != 0)]
            e["sym"], e["date"] = s, e.index
            e["sigma"] = sig[s].reindex(e.index).values
            e = e.dropna(subset=["sigma"])
            e["zx"], e["zy"] = (e["x"] / e["sigma"]).clip(-4, 4), (e["y"] / e["sigma"]).clip(-4, 4)  # 단발 급변(6J 7/30 등) 영향 제한
            e["cost"] = costs[s]
            evs.append(e.reset_index(drop=True))
        ev = pd.concat(evs, ignore_index=True)
        for per, (a, b) in (("train", TRAIN), ("val", VAL)):
            m = (ev["date"] >= a) & (ev["date"] <= b)
            r = evaluate(ev[m])
            rows.append({"signal": name, "desc": desc, "period": per, **r})
            for s in SYMS:
                rs = evaluate(ev[m & (ev["sym"] == s)])
                sym_rows.append({"signal": name, "period": per, "sym": s, **rs})

    res = pd.DataFrame(rows)
    by = pd.DataFrame(sym_rows)
    by.to_csv(OUT / "scan_by_symbol.csv", index=False)
    res.to_csv(OUT / "scan_pooled.csv", index=False)

    w = res.pivot(index=["signal", "desc"], columns="period",
                  values=["n", "beta", "t_beta", "gross_bp", "t_gross", "net_bp"])
    lines = ["# 장중 신호 후보 검정 — 학습(05/01~08/31)·검증(09월)", "",
             "8종목 묶음. β·t = 표준화 y 를 표준화 x 에 회귀(날짜 군집). 부호 매매 = sign(x) 방향 보유, 거래당 bp.",
             "표준화 값은 ±4 에서 자름. 비용 = 왕복 1틱 + 마이크로 $4 (종목 평균). 후보 10개 × 2구간이라 |t| 2 는 우연일 수 있음 (본페로니 기준 |t|≳2.8).", "",
             "| 신호 | 정의 (뉴욕 시각) | 학습 n | 학습 β (t) | 검증 β (t) | 학습 부호매매 bp (t) | 검증 부호매매 bp (t) | 비용 bp |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for (name, desc), r in w.iterrows():
        c = res[(res.signal == name) & (res.period == "train")]["cost_bp"].iloc[0]
        lines.append(f"| {name} | {desc} | {int(r[('n','train')])} | {r[('beta','train')]:+.3f} ({r[('t_beta','train')]:+.2f}) | "
                     f"{r[('beta','val')]:+.3f} ({r[('t_beta','val')]:+.2f}) | {r[('gross_bp','train')]:+.2f} ({r[('t_gross','train')]:+.2f}) | "
                     f"{r[('gross_bp','val')]:+.2f} ({r[('t_gross','val')]:+.2f}) | {c:.2f} |")
    lines += ["", "## 종목별 학습 β 의 t", ""]
    tb = by[by.period == "train"].pivot(index="signal", columns="sym", values="t_beta")[SYMS]
    lines.append("| 신호 | " + " | ".join(SYMS) + " |")
    lines.append("| --- |" + " --- |" * len(SYMS))
    for name, r in tb.iterrows():
        lines.append(f"| {name} | " + " | ".join(f"{v:+.1f}" for v in r.values) + " |")
    lines += ["", "## 종목별 비용 bp", "", " · ".join(f"{s} {costs[s]:.2f}" for s in SYMS)]
    (OUT / "scan_summary.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
