"""data/bars/<품목>.csv 를 최신까지 이어 받기 (모의 서버 차트 조회만).

  .venv/bin/python refresh_bars.py            # 각 파일 마지막 날짜부터 받아 붙임
"""
from __future__ import annotations

import pandas as pd

import config
import data
from nh_client import NHClient, NHError

SIG = {"ES": "ESZ26", "NQ": "NQZ26", "CL": "CLF27", "GC": "GCZ26", "HG": "HGZ26",
       "6E": "6EZ26", "6J": "6JZ26", "ZN": "ZNZ26"}


def main() -> None:
    client = NHClient()
    client.token()
    for prd, sym in SIG.items():
        path = config.BARS_DIR / f"{prd}.csv"
        old = pd.read_csv(path, dtype={"ref_dt": str, "exch_tm": str})
        start = old["ref_dt"].max()
        try:
            new = data.fetch_cont(client, sym, "FCME", start)
        except NHError as e:
            print(f"{prd}: 실패 {e.body[:120]} — 기존 파일 유지", flush=True)
            continue
        if not len(new):
            print(f"{prd}: 새 봉 없음 — 기존 파일 유지", flush=True)
            continue
        both = pd.concat([old, new.astype(old.dtypes.to_dict())], ignore_index=True)
        both = both.drop_duplicates(["ref_dt", "exch_tm"], keep="last").sort_values(["ref_dt", "exch_tm"])
        both.to_csv(path, index=False)
        print(f"{prd}: {len(old)} → {len(both)}행, {both['ref_dt'].iloc[0]}~{both['ref_dt'].iloc[-1]}", flush=True)


if __name__ == "__main__":
    main()
