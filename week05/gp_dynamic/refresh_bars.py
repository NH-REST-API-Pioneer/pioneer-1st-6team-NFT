"""data/bars/<품목>.csv 를 최신까지 이어 받기 (모의 서버 차트 조회만, 주문 없음).

처음엔 팀원 파일(config.TEAM_BARS)을 읽어서 시작하고, 마지막 날짜부터 연결선물 5분봉을 받아 붙입니다.
차트 API 는 한 번에 500봉까지라 nxt_key 로 나눠 받고, (ref_dt, exch_tm) 중복은 새 것으로 지웁니다.

  .venv/bin/python refresh_bars.py            # 8종목
  .venv/bin/python refresh_bars.py GC ES      # 고른 것만
"""
from __future__ import annotations

import sys

import pandas as pd

import bars
import config
from nh_client import NHClient, NHError

# vol_regime/refresh_bars.py 와 같은 대표 월물 (연결선물 조회에 종목 코드로만 씀)
SIG = {"ES": "ESZ26", "NQ": "NQZ26", "CL": "CLF27", "GC": "GCZ26", "HG": "HGZ26",
       "6E": "6EZ26", "6J": "6JZ26", "ZN": "ZNZ26"}


def main(syms: list[str]) -> None:
    config.BARS_DIR.mkdir(parents=True, exist_ok=True)
    client = NHClient()
    client.token()
    for prd in syms:
        old = bars.read_raw(prd)
        start = old["ref_dt"].max()
        try:
            new = bars.fetch_cont(client, SIG[prd], "FCME", start)
        except NHError as e:
            print(f"{prd}: 실패 {e.body[:120]} — 기존 파일 유지", flush=True)
            continue
        both = bars.merge(old, new)
        both.to_csv(config.BARS_DIR / f"{prd}.csv", index=False)
        print(f"{prd}: {len(old)} → {len(both)}행, {both['ref_dt'].iloc[0]}~{both['ref_dt'].iloc[-1]} "
              f"{both['exch_tm'].iloc[-1]}", flush=True)


if __name__ == "__main__":
    main(sys.argv[1:] or list(SIG))
