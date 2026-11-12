"""2단계 다시 — 하루 전체 스크리닝 후보 37개, 666쌍의 6개월(2026-05-01~11-07) 5분봉 공적분.

팀원 `../cointegrate_long.py` 와 같은 방법(연결선물 로그가격, 재개장 갭 제거, Engle-Granger, ADF 시차 6 고정,
20거래일 창 5일씩 밀기)에 두 가지를 바꿨습니다.

1. 재개장 갭: 팀원은 CME·SGX 휴장 시각을 직접 적었는데, 여기는 ICE·SGX 여러 상품이 섞여서 **품목마다 데이터로**
   찾습니다. 30분 넘게 빈 뒤 첫 봉의 시각(5분 단위)이 거래일의 20% 이상에서 반복되면 「정해진 재개장」으로 보고,
   그 봉과 6시간 넘는 공백(주말) 뒤 봉만 시가→종가로 잇습니다. 거래가 드문 품목의 장중 공백은 그대로 둡니다
   (팀원이 NKD 에서 겪은 문제). `--check` 로 팀원 14개 91쌍 결과와 p값을 맞춰 봅니다.
2. 15~18시 지표 대신 **하루 전체** 지표:
   - day_cross_med  스프레드(6개월 β)가 앞 20일 평균을 하루에 몇 번 건너는지 (중앙값, 모든 시각)
   - sd_bp          20일 창마다 β 를 다시 잡은 스프레드 표준편차(bp)의 중앙값 — 한 번 벌어질 때 크기
   - cost_bp        y 1계약 + β 로 맞춘 x 계약(소수 그대로)의 왕복 비용을 y 명목 대비 bp 로 (screening_full 가정)
   - sd_to_cost     sd_bp / cost_bp — 1σ 벌어짐이 왕복 비용 몇 번치인지

검정을 666번 하므로 p < 0.05 는 우연히 30개 넘게 나옵니다. 그래서 p_6m 과 함께 20일 창 통과 비율(꾸준함)을 봅니다.

  cd week04/coint_fullday && python3 coint_full.py           # results/coint_full.csv
  python3 coint_full.py --check                              # 팀원 14개로 재개장 처리 맞춰 보기
  python3 coint_full.py --raw                                # 갭을 빼지 않은 판 → results/coint_full_raw.csv

`--raw` 가 필요한 이유: 갭 빼기는 두 다리가 같은 시각에 쉴 때만 맞습니다. SGX·ICE 처럼 한쪽만 쉬는 쌍에서는 그쪽의
밤사이 움직임만 지워져 스프레드가 매일 튑니다(SNK↔NIY: 같은 닛케이인데 08:30 봉에서 최대 136bp, p 0.33).
갭을 안 빼면 이 문제는 없고 대신 월물 교체 점프가 남습니다. 두 판을 같이 봅니다.
"""
from __future__ import annotations

import argparse
import csv
import itertools
import math
import sys
import time
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from statsmodels.tsa.stattools import adfuller, coint

HERE = Path(__file__).resolve().parent
W4 = HERE.parent
sys.path.insert(0, str(W4))

from cointegrate import half_life, ols_beta  # noqa: E402  (팀원 파일 — 불러 쓰기만)

warnings.filterwarnings("ignore")
TEAM = W4 / "results" / "data" / "bars_5m_cont"
MINE = HERE / "data"
OUT = HERE / "results"
FIRST, LAST = "20260501", "20261107"       # 팀원과 같은 기간
REOPEN_GAP = pd.Timedelta("30min")
WEEKEND_GAP = pd.Timedelta("6h")
SCHEDULED_SHARE = 0.2
ROLL_DAYS, ROLL_STEP = 20, 5
LAGS = 6


def eg(y, x) -> float:
    return coint(y, x, maxlag=LAGS, autolag=None)[1]


def source(prd: str) -> Path:
    p = TEAM / f"{prd}.csv"
    return p if p.exists() else MINE / f"{prd}.csv"


RAW = False   # --raw: 갭을 빼지 않음 (로그 종가 그대로)


def load_gapless(path: Path) -> tuple[pd.Series, list[str]]:
    """재개장 갭을 뺀 로그가격 누적과, 찾은 정해진 재개장 시각들. RAW 면 로그 종가 그대로."""
    df = pd.read_csv(path, dtype=str)
    df = df[(df["ref_dt"] >= FIRST) & (df["ref_dt"] <= LAST)]
    ts = pd.to_datetime(df["ref_dt"] + df["exch_tm"].str.zfill(6), format="%Y%m%d%H%M%S")
    keep = ~ts.duplicated(keep="last").values
    df, ts = df[keep], ts[keep].reset_index(drop=True)
    o = np.log(df["open_pric"].astype(float).values)
    c = np.log(df["close_pric"].astype(float).values)
    gap = ts.diff()
    clock = ts.dt.strftime("%H%M")
    after = clock[gap > REOPEN_GAP]
    n_days = ts.dt.normalize().nunique()
    counts = after.value_counts()
    scheduled = sorted(counts[counts >= SCHEDULED_SHARE * n_days].index)
    sched_min = {int(s[:2]) * 60 + int(s[2:]) for s in scheduled}
    cm = (ts.dt.hour * 60 + ts.dt.minute).values
    near = np.array([any(abs(m - s) <= 10 for s in sched_min) for m in cm])
    reopen = ((gap > REOPEN_GAP).values & near) | (gap >= WEEKEND_GAP).values
    if RAW:
        reopen[:] = False
    step = np.empty(len(c))
    step[0] = 0.0
    step[1:] = np.where(reopen[1:], c[1:] - o[1:], c[1:] - c[:-1])
    return pd.Series(np.cumsum(step), index=ts.values, name=path.stem), scheduled


def specs() -> dict[str, dict]:
    with (OUT / "screening_full.csv").open(encoding="utf-8") as f:
        return {r["prd"]: r for r in csv.DictReader(f) if r["candidate"] == "True"}


def pair_row(a: str, b: str, sa: pd.Series, sb: pd.Series, sp: dict, i1: dict) -> dict | None:
    j = pd.concat([sa, sb], axis=1, join="inner").dropna()
    if len(j) < 2000:
        return None
    p_ab, p_ba = eg(j[a].values, j[b].values), eg(j[b].values, j[a].values)
    y, x = (a, b) if p_ab <= p_ba else (b, a)
    yv, xv = j[y].values, j[x].values
    beta = ols_beta(yv, xv)
    resid = yv - beta * xv
    hl_h = half_life(resid) * 5 / 60

    days = sorted(set(j.index.normalize()))
    passed = total = 0
    sds = []
    for k in range(0, len(days) - ROLL_DAYS + 1, ROLL_STEP):
        m = (j.index >= days[k]) & (j.index < days[k + ROLL_DAYS - 1] + pd.Timedelta("1D"))
        w = j[m]
        if len(w) < 1000:
            continue
        total += 1
        passed += eg(w[y].values, w[x].values) < 0.05
        bw = ols_beta(w[y].values, w[x].values)
        sds.append(float(np.std(w[y].values - bw * w[x].values)) * 1e4)

    spread = pd.Series(resid, index=j.index)
    crosses = []
    for i, d in enumerate(days):
        if i < ROLL_DAYS or d.dayofweek >= 5:
            continue
        ref = spread[(spread.index >= days[i - ROLL_DAYS]) & (spread.index < d)].mean()
        today = spread[(spread.index >= d) & (spread.index < d + pd.Timedelta("1D"))] - ref
        if len(today) >= 100:
            crosses.append(int(np.sum(np.diff(np.sign(today.values)) != 0)))

    ny, nx = float(sp[y]["notional_usd"]), float(sp[x]["notional_usd"])
    hedge = abs(beta) * ny / nx                                   # x 계약 수 (y 1계약당, 소수)
    cost_usd = float(sp[y]["cost_rt_usd"]) + hedge * float(sp[x]["cost_rt_usd"])
    cost_bp = cost_usd / ny * 1e4
    sd_bp = float(np.median(sds)) if sds else math.nan
    return {
        "y": y, "x": x, "y_group": sp[y]["group"], "x_group": sp[x]["group"], "n": len(j),
        "p_6m": round(min(p_ab, p_ba), 5), "beta": round(beta, 3),
        "hl_hours": round(hl_h, 1) if math.isfinite(hl_h) else math.inf,
        "roll_pass": f"{passed}/{total}", "roll_ratio": round(passed / total, 2) if total else math.nan,
        "day_cross_med": float(np.median(crosses)) if crosses else math.nan,
        "sd_bp": round(sd_bp, 1), "cost_bp": round(cost_bp, 2), "x_per_y": round(hedge, 2),
        "sd_to_cost": round(sd_bp / cost_bp, 2) if cost_bp else math.nan,
        "both_I1": bool(i1[y] >= 0.1 and i1[x] >= 0.1), "adf_y": round(i1[y], 3), "adf_x": round(i1[x], 3),
    }


def check() -> None:
    """팀원 14개를 이 재개장 처리로 다시 해서 팀원 coint_long.csv 의 p_6m 과 비교."""
    team = pd.read_csv(W4 / "results" / "coint_long.csv")
    series = {p.stem: load_gapless(p)[0] for p in sorted(TEAM.glob("*.csv"))}
    diffs = []
    for _, r in team.iterrows():
        j = pd.concat([series[r["y"]], series[r["x"]]], axis=1, join="inner").dropna()
        p = min(eg(j[r["y"]].values, j[r["x"]].values), eg(j[r["x"]].values, j[r["y"]].values))
        diffs.append((r["y"], r["x"], r["p_6m"], round(p, 4)))
    d = pd.DataFrame(diffs, columns=["y", "x", "team_p", "mine_p"])
    d["same_side_005"] = (d.team_p < 0.05) == (d.mine_p < 0.05)
    print(d.sort_values("team_p").head(15).to_string(index=False))
    print(f"p<0.05 판정이 같은 쌍 {d.same_side_005.sum()} / {len(d)}, |p 차이| 중앙값 {np.median(abs(d.team_p - d.mine_p)):.4f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--raw", action="store_true", help="갭을 빼지 않은 판")
    args = ap.parse_args()
    global RAW
    RAW = args.raw
    if args.check:
        check()
        return
    sp = specs()
    missing = [k for k in sp if not source(k).exists()]
    if missing:
        raise SystemExit(f"6개월 5분봉이 없습니다: {missing} — fetch_more.py 먼저")
    t0 = time.time()
    series, sched = {}, {}
    for k in sp:
        series[k], sched[k] = load_gapless(source(k))
    print("재개장 시각:", " ".join(f"{k}:{','.join(v)}" for k, v in sched.items()))
    i1 = {k: adfuller(v.values, maxlag=LAGS, autolag=None)[1] for k, v in series.items()}
    print("단일 ADF p<0.1 (혼자 정상 → 공적분 판정 못 믿음):", [k for k, v in i1.items() if v < 0.1])
    out = []
    pairs = list(itertools.combinations(sorted(series), 2))
    for n, (a, b) in enumerate(pairs, 1):
        row = pair_row(a, b, series[a], series[b], sp, i1)
        if row:
            out.append(row)
        if n % 100 == 0:
            print(f"  {n}/{len(pairs)} 쌍 {time.time() - t0:.0f}초", flush=True)
    df = pd.DataFrame(out).sort_values(["both_I1", "p_6m"], ascending=[False, True])
    OUT.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT / ("coint_full_raw.csv" if RAW else "coint_full.csv"), index=False)
    pd.set_option("display.width", 250)
    cols = ["y", "x", "p_6m", "beta", "hl_hours", "roll_pass", "day_cross_med", "sd_bp", "cost_bp", "sd_to_cost"]
    good = df[df.both_I1]
    print(f"\n{len(df)}쌍 · 두 다리 다 비정상 {len(good)}쌍 · p<0.05 {int((good.p_6m < 0.05).sum())}쌍 · "
          f"p<0.01 {int((good.p_6m < 0.01).sum())}쌍 · 20일 창 절반 이상 통과 {int((good.roll_ratio >= 0.5).sum())}쌍")
    print("\n[p 순 상위 25]")
    print(good.head(25)[cols].to_string(index=False))
    print("\n[20일 창 통과 비율 순 상위 15]")
    print(good.sort_values(["roll_ratio", "p_6m"], ascending=[False, True]).head(15)[cols].to_string(index=False))
    print(f"\n{time.time() - t0:.0f}초")


if __name__ == "__main__":
    main()
