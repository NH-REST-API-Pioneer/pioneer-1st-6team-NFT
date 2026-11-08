"""스크리닝 후보 해외선물의 1분봉 받기 — 조회만 (주문 없음).

모의 서버에서 차트가 되는 거래소는 CME·SGX·ICE(DX) 뿐이었습니다 (2026-11-08 확인:
EUREX·HKEX·CBOE 는 chart-minute 가 「종목을 찾을수 없습니다」). 그래서 후보는 그 셋에서,
대회 시간(KST 15~18시)에 열려 있는 근월물만 골랐습니다.

  python3 fetch_bars.py                 # 기본 후보 전부, 오늘부터 21일 전까지
  python3 fetch_bars.py --syms 6EZ26:FCME 6BZ26:FCME --days 30
  python3 fetch_bars.py --force         # 이미 받은 파일도 다시

결과: results/data/bars/<sym>.csv (거래가 있던 분만, 시각은 KST)
"""
from __future__ import annotations

import argparse
import csv
from datetime import date, timedelta
from pathlib import Path

from nh_client import NHClient, NHError, rows

OUT = Path(__file__).parent / "results" / "data" / "bars"

# 근월물 (10/19~11/07 기간 기준 가장 활발한 월물)
CANDIDATES = {
    "FCME": [
        # 통화
        "6AZ26", "6BZ26", "6CZ26", "6EZ26", "6JZ26", "6SZ26", "6NZ26", "6MZ26",
        "M6EZ26", "M6BZ26", "M6AZ26", "E7Z26", "J7Z26", "MJYZ26", "RPZ26", "RYZ26",
        # 지수
        "ESZ26", "NQZ26", "YMZ26", "RTYZ26", "MESZ26", "MNQZ26", "MYMZ26", "M2KZ26", "NKDZ26", "NIYZ26",
        # 금속
        "GCZ26", "MGCZ26", "SIZ26", "MSIZ26", "HGZ26", "PLF27", "PAZ26",
        # 에너지
        "CLZ26", "MCLZ26", "QMZ26", "NGZ26", "HOZ26", "RBZ26",
        # 금리
        "ZNZ26", "ZFZ26", "ZBZ26", "TNZ26", "UBZ26", "SR3Z26",
    ],
    "FSGX": ["MKUX26", "SFCX26", "SINX26", "SNKZ26", "TWNX26", "UCX26", "IUX26", "SSGX26"],
    "FICE": ["DXZ26"],
}
FIELDS = ["ref_dt", "exch_tm", "open_pric", "high_pric", "low_pric", "close_pric", "cum_trd_qty"]


def fetch_minutes(client: NHClient, sym: str, exch: str, start_dt: str, end_dt: str) -> list[dict]:
    """nxt_key 로 과거로 이어 받으며 start_dt 에 닿을 때까지 (3주차 fetch_data.py 와 같은 방식)."""
    bars: dict[tuple[str, str], dict] = {}
    nxt = "99999999"
    while True:
        body = client.chart_minute(sym, exch, start_dt=start_dt, end_dt=end_dt, req_qty=500, nxt_key=nxt)
        got = rows(body)
        for r in got:
            bars[(r["ref_dt"], r["exch_tm"])] = r
        new = str(body.get("nxt_key") or "").strip()
        if not got or not new or new == nxt or new.strip("9") == "" or min(r["ref_dt"] for r in got) < start_dt:
            break
        nxt = new
    return [bars[k] for k in sorted(bars) if k[0] >= start_dt]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--syms", nargs="*", help="sym:거래소 (예 6EZ26:FCME). 없으면 기본 후보 전부")
    ap.add_argument("--days", type=int, default=21)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    pairs = [tuple(s.split(":")) for s in args.syms] if args.syms else \
        [(s, e) for e, syms in CANDIDATES.items() for s in syms]
    today = date.today()
    start, end = (today - timedelta(days=args.days)).strftime("%Y%m%d"), today.strftime("%Y%m%d")

    client = NHClient()
    client.token()
    OUT.mkdir(parents=True, exist_ok=True)
    for sym, exch in pairs:
        path = OUT / f"{sym}.csv"
        if path.exists() and not args.force:
            print(f"{sym}: 이미 있음, 건너뜀")
            continue
        try:
            bars = fetch_minutes(client, sym, exch, start, end)
        except NHError as e:
            print(f"{sym}: 실패 {e.body[:120]}")
            continue
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(bars)
        span = f"{bars[0]['ref_dt']}~{bars[-1]['ref_dt']}" if bars else "-"
        print(f"{sym}: {len(bars)}행 {span}", flush=True)


if __name__ == "__main__":
    main()
