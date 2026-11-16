"""5분봉 읽기·정리·받기.

원본 CSV(week04 fetch_long.py 형식): ref_dt, exch_tm(KST), open/high/low/close_pric, cum_trd_qty(봉 거래량).
정리 결과: 인덱스 KST 봉 시작, 열 o h l c v r gap roll.
  r    로그 수익률 c/c₋₁. 직전 봉과 간격이 GAP_MAX 보다 길거나(gap) 롤 봉(roll)이면 NaN.
  roll 연결선물 월물 교체 점프 (기록용). NH 연결선물은 휴장 직후 첫 봉에서 월물을 바꿈 (ES 6/15·9/14 07:00
       봉에서 확인) → gap 봉 중 |로그 변화| > 20 × 직전 288봉 |r| 중앙값인 봉. 그 봉 r 은 gap 이라 이미 NaN.
       장중 단발 급변은 지표 발표 같은 진짜 변동성이라 지우지 않음 (2026-11-16 실측: 21:30·03:00 봉).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import config

ROLL_K = 20
ROLL_WIN = 288


def clean(raw: pd.DataFrame) -> pd.DataFrame:
    ts = pd.to_datetime(raw["ref_dt"].astype(str) + raw["exch_tm"].astype(str).str.zfill(6),
                        format="%Y%m%d%H%M%S")
    df = pd.DataFrame({
        "o": raw["open_pric"].astype(float).values, "h": raw["high_pric"].astype(float).values,
        "l": raw["low_pric"].astype(float).values, "c": raw["close_pric"].astype(float).values,
        "v": raw["cum_trd_qty"].astype(float).values,
    }, index=ts)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df.index.name = "ts"
    gap = df.index.to_series().diff() > config.GAP_MAX
    gap.iloc[0] = True
    lr = np.log(df["c"]).diff()
    r = lr.where(~gap)
    med = r.abs().rolling(ROLL_WIN, min_periods=ROLL_WIN // 4).median().shift(1).ffill()
    roll = (gap & (lr.abs() > ROLL_K * med)).fillna(False)
    df["r"], df["gap"], df["roll"] = r, gap.values, roll.values
    return df


def load_bars(sym: str) -> pd.DataFrame:
    return clean(pd.read_csv(config.BARS_DIR / f"{sym}.csv", dtype={"ref_dt": str, "exch_tm": str}))


def fetch_cont(client, sym: str, exch: str, start: str, max_pages: int = 400) -> pd.DataFrame:
    """연결선물(cidx_yn=1) 5분봉을 start(YYYYMMDD)까지 nxt_key 로 거슬러 받음. 원본 열 그대로."""
    from nh_client import rows

    bars, nxt = {}, "99999999"
    for _ in range(max_pages):
        body = client.chart_minute(sym, exch, start_dt=start, end_dt="99991231", dcnt=5, req_qty=500,
                                   nxt_key=nxt, cidx_yn="1")
        got = rows(body)
        for r in got:
            bars[(r["ref_dt"], r["exch_tm"])] = r
        new = str(body.get("nxt_key") or "").strip()
        if not got or not new or new == nxt or new.strip("9") == "" or min(r["ref_dt"] for r in got) < start:
            break
        nxt = new
    keep = ["ref_dt", "exch_tm", "open_pric", "high_pric", "low_pric", "close_pric", "cum_trd_qty"]
    out = pd.DataFrame([bars[k] for k in sorted(bars) if k[0] >= start])
    return out[keep] if len(out) else pd.DataFrame(columns=keep)
