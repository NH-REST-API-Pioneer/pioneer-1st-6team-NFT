"""2단계 준비 — 하루 전체 스크리닝 후보 중 6개월 5분봉이 아직 없는 품목을 받습니다. 조회만 (주문 없음).

팀원이 받은 14개(../results/data/bars_5m_cont/)는 그대로 쓰고, 나머지만 `../fetch_long.fetch` 로
연결선물(cidx_yn=1) 5분봉을 2026-05-01 부터 받아 data/<품목>.csv 에 둡니다. 이미 받은 파일은 건너뜁니다.

  cd week04/coint_fullday && python3 fetch_more.py
"""
from __future__ import annotations

import csv
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
W4 = HERE.parent
sys.path.insert(0, str(W4))

from fetch_long import FIELDS, fetch  # noqa: E402  (팀원 파일 — 불러 쓰기만)
from nh_client import NHClient, NHError  # noqa: E402

sys.path.insert(0, str(HERE))
import products  # noqa: E402

TEAM = W4 / "results" / "data" / "bars_5m_cont"
OUT = HERE / "data"
EXCH = {"CME": "FCME", "SGX": "FSGX", "ICE": "FICE"}
START = "20260501"


def candidates() -> list[dict]:
    with (HERE / "results" / "screening_full.csv").open(encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if r["candidate"] == "True"]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    client = NHClient()
    client.token()
    for r in candidates():
        prd, sym = r["prd"], r["sym"]
        if (TEAM / f"{prd}.csv").exists() or (OUT / f"{prd}.csv").exists():
            continue
        exch = EXCH[products.exch_of(prd)]
        t0 = time.time()
        try:
            bars = fetch(client, sym, exch, START)
        except NHError as e:
            print(f"{sym}: 실패 {e.body[:160]}", flush=True)
            continue
        with (OUT / f"{prd}.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(bars)
        span = f"{bars[0]['ref_dt']}~{bars[-1]['ref_dt']}" if bars else "-"
        print(f"{sym} ({exch}): {len(bars):,}행 {span}  {time.time() - t0:.0f}초", flush=True)


if __name__ == "__main__":
    main()
