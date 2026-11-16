"""5분봉 읽기·정리·받기.

정리 규칙은 5주차 vol_regime/data.py 와 같고(팀원 코드), 롤 점프를 이어 붙이는 것만 더했습니다.
원본 CSV: ref_dt, exch_tm(KST), open/high/low/close_pric, cum_trd_qty.
정리 결과: 인덱스 KST 봉 시작, 열 o h l c v r gap.
  r     로그 수익률 c/c₋₁. 직전 봉과 간격이 GAP_MAX 보다 길면 NaN (휴장·주말을 건너는 수익률은 신호에서 버림).
  gap   직전 봉과 간격이 긴 봉. o h l c 는 간격 점프(직전 마감 → 이 봉 시가)를 뺀 값이라 롤 점프가 손익·잔차 z 에
        들어가지 않음. 대가: 매일 휴장(1시간)을 들고 넘길 때의 실제 변동도 0으로 봄 (근거 문서 한계).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import config


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
    r = np.log(df["c"]).diff().where(~gap)
    # 간격 점프(직전 마감 → 간격 뒤 첫 시가)를 빼서 이어 붙임. NH 연결선물은 휴장 직후 첫 봉에서 월물을
    # 바꾸는데(팀원 실측), 롤 봉만 골라내는 규칙은 GC·SI 에서 주말 봉을 롤로 잘못 잡아서 간격 전체를 뺌.
    jump = (np.log(df["o"]) - np.log(df["c"].shift(1))).where(gap, 0.0).fillna(0.0).cumsum()
    adj = np.exp(-jump.values)
    df["c_raw"] = df["c"].values          # 계약 수 환산(명목금액)에는 원래 가격을 씀
    for col in ("o", "h", "l", "c"):
        df[col] = df[col].values * adj
    df["r"], df["gap"] = r.values, gap.values
    return df


def path(sym: str):
    p = config.BARS_DIR / f"{sym}.csv"
    return p if p.exists() else config.TEAM_BARS / f"{sym}.csv"


def read_raw(sym: str) -> pd.DataFrame:
    return pd.read_csv(path(sym), dtype={"ref_dt": str, "exch_tm": str})


def load(sym: str) -> pd.DataFrame:
    return clean(read_raw(sym))


def fetch_cont(client, sym: str, exch: str, start: str, max_pages: int = 400) -> pd.DataFrame:
    """연결선물(cidx_yn=1) 5분봉을 start(YYYYMMDD)까지 nxt_key 로 거슬러 받음 (vol_regime/data.py 와 같음)."""
    from nh_client import rows

    got_all, nxt = {}, "99999999"
    for _ in range(max_pages):
        body = client.chart_minute(sym, exch, start_dt=start, end_dt="99991231", dcnt=5, req_qty=500,
                                   nxt_key=nxt, cidx_yn="1")
        got = rows(body)
        for r in got:
            got_all[(r["ref_dt"], r["exch_tm"])] = r
        new = str(body.get("nxt_key") or "").strip()
        if not got or not new or new == nxt or new.strip("9") == "" or min(r["ref_dt"] for r in got) < start:
            break
        nxt = new
    keep = ["ref_dt", "exch_tm", "open_pric", "high_pric", "low_pric", "close_pric", "cum_trd_qty"]
    out = pd.DataFrame([got_all[k] for k in sorted(got_all) if k[0] >= start])
    return out[keep] if len(out) else pd.DataFrame(columns=keep)


def merge(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """이어 붙이고 (ref_dt, exch_tm) 중복은 새 것으로. 정렬."""
    if not len(new):
        return old
    both = pd.concat([old, new.astype(old.dtypes.to_dict())], ignore_index=True)
    return both.drop_duplicates(["ref_dt", "exch_tm"], keep="last").sort_values(["ref_dt", "exch_tm"]) \
               .reset_index(drop=True)
