"""긴 기간 5분봉 받기 — 연결선물(cidx_yn=1) 로 근월물을 이어붙인 시세.

만기 지난 월물(ESU26 등)은 「종목정보를 찾을 수 없습니다」라 직접 못 받습니다. 대신 차트(분)에
cidx_yn=1 을 주면 근월물을 이어붙인 봉이 옵니다 (2026-11-08 확인, ES 6/1 까지 64쪽).
분봉 요청의 날짜 범위는 무시되고 최신부터 오므로 nxt_key 로 start 날짜에 닿을 때까지 넘깁니다.

  python3 fetch_long.py                       # 기본 14개, 2026-05-01 부터
  python3 fetch_long.py --start 20260301 --syms ESZ26:FCME

결과: results/data/bars_5m_cont/<품목>.csv  (시각 KST, 롤 보정 전 원본)
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

from nh_client import NHClient, NHError, rows

OUT = Path(__file__).parent / "results" / "data" / "bars_5m_cont"
DEFAULT = ["ESZ26:FCME", "NQZ26:FCME", "YMZ26:FCME", "RTYZ26:FCME", "GCZ26:FCME", "SIZ26:FCME",
           "PLF27:FCME", "HGZ26:FCME", "CLZ26:FCME", "HOZ26:FCME", "RBZ26:FCME", "NIYZ26:FCME",
           "NKDZ26:FCME", "SNKZ26:FSGX"]
FIELDS = ["ref_dt", "exch_tm", "open_pric", "high_pric", "low_pric", "close_pric", "cum_trd_qty"]


def fetch(client: NHClient, sym: str, exch: str, start: str) -> list[dict]:
    bars, nxt = {}, "99999999"
    for _ in range(400):
        body = client.post("/trade/v1/overseas/chart-minute", {
            "exch_cd": exch, "sym": sym, "inq_strt_dt": start, "strt_tm": "000000", "inq_ed_dt": "99991231",
            "ed_tm": "235959", "req_qty": "500", "dcnt": "5", "cidx_yn": "1", "nxt_key": nxt})
        got = rows(body)
        for r in got:
            bars[(r["ref_dt"], r["exch_tm"])] = r
        new = str(body.get("nxt_key") or "").strip()
        if not got or not new or new == nxt or new.strip("9") == "" or min(r["ref_dt"] for r in got) < start:
            break
        nxt = new
    return [bars[k] for k in sorted(bars) if k[0] >= start]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="20260501")
    ap.add_argument("--syms", nargs="*", default=DEFAULT)
    args = ap.parse_args()
    client = NHClient()
    client.token()
    OUT.mkdir(parents=True, exist_ok=True)
    for item in args.syms:
        sym, exch = item.split(":")
        try:
            bars = fetch(client, sym, exch, args.start)
        except NHError as e:
            print(f"{sym}: 실패 {e.body[:120]}", flush=True)
            continue
        with (OUT / f"{sym[:-3]}.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(bars)
        span = f"{bars[0]['ref_dt']}~{bars[-1]['ref_dt']}" if bars else "-"
        print(f"{sym}: {len(bars)}행 {span}", flush=True)


if __name__ == "__main__":
    main()
