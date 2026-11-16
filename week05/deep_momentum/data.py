"""연결선물 일봉 받기·정리.

받기 (모의 서버 차트 조회만): /trade/v1/overseas/chart-daily, cidx_yn=1(연결), req_qty 최대.
  한 번에 오는 줄 수가 제한돼서 nxt_key 로 과거로 거슬러 이어 받고, ref_dt 중복은 나중 것으로 지웁니다.
  2000~2002년부터 2026-11 까지 종목당 6,200~6,900일.

정리:
  1) 2004년 전은 버림 (ZN·6J 는 2001~2003년에 단위가 다른 값이 섞여 있음).
  2) 튀는 값: 하루만 뛰었다가 다음날 시가에 원래 수준으로 돌아온 시가·종가를 전일 종가로 (fix_spikes).
     CL 2016-10-19 종가 1.4조, HG 2013-05-24 종가 0.033, ZN 2004~2009 공휴일 무렵 91↔112 같은 값
  · 1/1·12/25 휴장일 줄은 버림 (ZN 2009-01-01 처럼 엉뚱한 값).
  3) 롤 점프: NH 연결선물은 월물을 바꿀 때 종가가 뜁니다 (diff_rt 도 종가 차이와 같아 롤 정보를 주지 않음).
     종목별 롤 일정 창(ROLL_WINDOWS) 안에서 전일 종가 → 당일 시가 갭이 가장 큰 날 하루를 롤로 보고
     그 갭만 뺍니다. 다른 날 갭은 실제 수익이라 둡니다 (GC 는 26년 수익의 대부분이 갭, 즉 정산 뒤 시간 외).
  r    = log(종가/전일 종가) − (롤 날이면 그 갭)
  px   = 원래 종가 (계약 수 환산용)
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

import config

START = "2004-01-01"
# (롤 월, 첫날, 끝날) — 만기 일정에 맞춘 창. NH 는 만기 주(지수·통화) 또는 인도월 전 달 말(금리·금속)에 바꿈
_Q = (3, 6, 9, 12)
ROLL_WINDOWS = {
    "ES": (_Q, 8, 21), "NQ": (_Q, 8, 21), "6E": (_Q, 8, 21), "6J": (_Q, 8, 21),
    "ZN": ((2, 5, 8, 11), 20, 31), "GC": ((1, 3, 5, 7, 9, 11), 20, 31),
    "HG": ((2, 4, 6, 8, 11), 20, 31), "CL": (tuple(range(1, 13)), 10, 25),
}


def fetch(client, sym: str, exch: str = "FCME", start: str = "20000101", max_pages: int = 40) -> pd.DataFrame:
    from nh_client import rows

    got, nxt = {}, "99999999"
    for _ in range(max_pages):
        body = client.post("/trade/v1/overseas/chart-daily", {
            "exch_cd": exch, "sym": sym, "inq_strt_dt": start, "inq_ed_dt": "99991231",
            "req_qty": "9999", "cidx_yn": "1", "nxt_key": nxt})
        r = rows(body)
        for x in r:
            got[x["ref_dt"]] = x
        new = str(body.get("nxt_key") or "").strip()
        if not r or not new or new == nxt or new.strip("9") == "":
            break
        nxt = new
    keep = ["ref_dt", "open_pric", "high_pric", "low_pric", "close_pric", "cum_trd_qty"]
    return pd.DataFrame([got[k] for k in sorted(got)])[keep]


def fix_spikes(o: np.ndarray, c: np.ndarray, tol: float = 0.1) -> tuple[np.ndarray, np.ndarray, int]:
    """뛰었다가 바로 돌아온 값만 고침: 전일 종가(ref)와 다음날 시가(nxt)는 비슷한데(차이 < tol/2)
    당일 시가나 종가가 둘 다와 tol 넘게 다르면 → ref 로 바꿈. 진짜 급변은 다음날에도 그 수준이라 안 걸림."""
    o, c = o.copy(), c.copy()
    n = 0
    for t in range(1, len(c) - 1):
        ref, nxt = c[t - 1], o[t + 1]
        if abs(np.log(nxt / ref)) >= tol / 2:
            continue
        for arr in (o, c):
            if abs(np.log(arr[t] / ref)) > tol and abs(np.log(arr[t] / nxt)) > tol:
                arr[t] = ref; n += 1
    return o, c, n


def roll_days(gap: pd.Series, sym: str) -> pd.DatetimeIndex:
    months, d0, d1 = ROLL_WINDOWS[sym]
    g = gap.dropna()
    m = g.index.month.isin(months) & (g.index.day >= d0) & (g.index.day <= d1)
    w = g[m]
    if not len(w):
        return pd.DatetimeIndex([])
    key = w.index.to_period("M")
    return pd.DatetimeIndex([grp.abs().idxmax() for _, grp in w.groupby(key)])


def clean(raw: pd.DataFrame, sym: str) -> pd.DataFrame:
    d = raw.copy()
    d["ref_dt"] = d["ref_dt"].astype(str)
    d = d.drop_duplicates("ref_dt", keep="last").sort_values("ref_dt")
    idx = pd.to_datetime(d["ref_dt"], format="%Y%m%d")
    out = pd.DataFrame({"o": d["open_pric"].astype(float).values, "c": d["close_pric"].astype(float).values,
                        "v": d["cum_trd_qty"].astype(float).values}, index=idx)
    out = out[(out.o > 0) & (out.c > 0)]
    md = out.index.strftime("%m-%d")
    out = out[~md.isin(["01-01", "12-25"])]      # 휴장일 줄 (ZN 2009-01-01 처럼 엉뚱한 값이 들어 있음)
    o, c, n = fix_spikes(out.o.values, out.c.values)
    out["o"], out["c"] = o, c
    out.attrs["spikes_fixed"] = n
    full = np.log(out.c).diff()
    gap = np.log(out.o / out.c.shift(1))
    rd = roll_days(gap, sym)
    out["roll"] = out.index.isin(rd)
    out["r"] = full - gap.where(out["roll"], 0.0)
    out["gap"] = gap
    out["px"] = out.c
    out.index.name = "date"
    return out.loc[START:].iloc[1:]


def winsorize(r: pd.Series) -> pd.Series:
    m = r.ewm(halflife=config.WINSOR_HL).mean()
    s = r.ewm(halflife=config.WINSOR_HL).std()
    lo, hi = m - config.WINSOR_K * s, m + config.WINSOR_K * s
    return r.clip(lower=lo.shift(1), upper=hi.shift(1)).fillna(r)


def load(sym: str) -> pd.DataFrame:
    d = clean(pd.read_csv(config.DATA / f"{sym}.csv", dtype={"ref_dt": str}), sym)
    d["r"] = winsorize(d["r"])
    return d


def main(syms: list[str]) -> None:
    from nh_client import NHClient

    config.DATA.mkdir(parents=True, exist_ok=True)
    client = NHClient()
    client.token()
    for s in syms:
        new = fetch(client, config.SIG[s])
        path = config.DATA / f"{s}.csv"
        if path.exists():
            old = pd.read_csv(path, dtype={"ref_dt": str})
            new = pd.concat([old, new.astype(str)], ignore_index=True)
        new = new.drop_duplicates("ref_dt", keep="last").sort_values("ref_dt")
        new.to_csv(path, index=False)
        print(f"{s}: {len(new)}일 {new.ref_dt.iloc[0]}~{new.ref_dt.iloc[-1]}", flush=True)


if __name__ == "__main__":
    main(sys.argv[1:] or config.SYMS)
