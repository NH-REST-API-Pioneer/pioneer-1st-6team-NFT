"""SGX 미니 원달러(MKU) 데이터 받기 — 품목 정보·거래시간·만기일·현재가·지난 1분봉.

사용법 (week03/ 에서):
  python3 fetch_data.py                  # 해외 모의 서버, MKUX26, 받을 수 있는 1분봉 전부
  python3 fetch_data.py --sym MKUZ26     # 다른 월물
  python3 fetch_data.py --days 30        # 오늘부터 30일 전까지만

조회 API 만 부릅니다 (주문 없음). 키는 ~/.nh_futures/credentials_overseas.env (nh_client.py).

결과는 results/data/ 에 저장합니다. 원본 응답(JSON)도 함께 남겨 응답 모양을 확인합니다.
분봉은 거래가 있던 분만 옵니다 (빈 분은 줄이 없음). 시각(exch_tm)·날짜(ref_dt)는 한국 시간입니다.
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

from common import in_trading_hours
from nh_client import NHClient, rows

PRD = "MKU"
EXCH = "FSGX"            # 시세·차트 요청의 거래소코드 (F + 거래소)
OUT = Path(__file__).parent / "results" / "data"


def save(name: str, data) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  저장: {path.relative_to(Path(__file__).parent)}")
    return path


def fetch_minutes(client: NHClient, sym: str, start_dt: str, end_dt: str) -> list[dict]:
    """nxt_key 로 과거로 이어 받으며 start_dt 에 닿을 때까지 모읍니다 (키가 전부 9 면 끝)."""
    bars: dict[tuple[str, str], dict] = {}
    nxt = "99999999"
    page = 0
    while True:
        body = client.chart_minute(sym, EXCH, start_dt=start_dt, end_dt=end_dt, req_qty=500, nxt_key=nxt)
        page += 1
        if page == 1:
            save(f"raw_chart_minute_{sym}_page1.json", body)  # 응답 모양 확인용
        got = rows(body)
        new_nxt = str(body.get("nxt_key") or "").strip()
        if got:
            print(f"  {page}쪽: {len(got)}개 {got[-1]['ref_dt']} {got[-1]['exch_tm']} ~ {got[0]['ref_dt']} {got[0]['exch_tm']}")
        for r in got:
            bars[(r["ref_dt"], r["exch_tm"])] = r
        if not got or not new_nxt or new_nxt == nxt or new_nxt.strip("9") == "":
            break
        nxt = new_nxt
    return [bars[k] for k in sorted(bars)]


def write_csv(path: Path, bars: list[dict]) -> None:
    fields = ["ref_dt", "exch_tm", "open_pric", "high_pric", "low_pric", "close_pric",
              "cum_trd_qty", "cum_trd_amt", "glb_biz_dt", "diff_rt", "rt"]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(bars)
    print(f"  저장: {path.relative_to(Path(__file__).parent)} ({len(bars)}행)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sym", default="MKUX26", help="종목코드 (기본 MKUX26 = 2026년 11월물)")
    ap.add_argument("--days", type=int, default=90, help="오늘부터 며칠 전까지 거슬러 받을지 (기본 90)")
    args = ap.parse_args()

    client = NHClient()
    print(f"서버: {client.env} ({client.base})")
    client.token()
    print(f"토큰 scopes: {client.scope} (S=모의, R=운영)")

    print("1) 품목별 세부정보 · 장운영정보")
    for name, body in (("product_detail_MKU.json", client.product_detail(PRD)),
                       ("market_operations_MKU.json", client.market_operations(PRD))):
        save(name, body)
        for r in rows(body):
            if r.get("prd_cd") == PRD:
                print("  ", {k: v for k, v in r.items() if k in (
                    "ko_prd_nm", "cur_cd", "ctr_unt", "tick_unt", "tick_val", "trd_tm_inf", "trd_tm_inf_2",
                    "sett_pric_tm_inf")})

    print("2) 만기일 (이번 달·다음 달)")
    for sym in (args.sym, "MKUZ26"):
        body = client.product_expiry(sym)
        save(f"expiry_{sym}.json", body)
        for r in rows(body):
            print(f"   {r.get('sym')}  만기 {r.get('exp_dt')}  잔존 {int(r.get('rem_dy') or 0)}일  {r.get('sett_nm')}")

    print("3) 현재가")
    price = client.price(args.sym, EXCH)
    save(f"price_{args.sym}.json", price)
    for r in rows(price):
        print(f"   {r.get('sym_nm')}  현재가 {r.get('last_pric')}  {r.get('trd_tm')}  거래량 {r.get('cum_trd_qty')}")

    print("4) 지난 1분봉")
    today = date.today()
    start = (today - timedelta(days=args.days)).strftime("%Y%m%d")
    bars = fetch_minutes(client, args.sym, start, today.strftime("%Y%m%d"))
    if bars:
        write_csv(OUT / f"bars_1m_{args.sym}.csv", bars)
        print(f"   기간: {bars[0]['ref_dt']} {bars[0]['exch_tm']} ~ {bars[-1]['ref_dt']} {bars[-1]['exch_tm']}")
        per_day = Counter(b["ref_dt"] for b in bars if in_trading_hours(b["exch_tm"]))
        print("   날짜별 1분봉 수 (매매 시간 08:25~20:35 · 20:50~24:00 안):")
        print("   ", " ".join(f"{d[4:]}:{n}" for d, n in sorted(per_day.items())))
    save("fetch_log.json", {
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "environment": client.env,
        "sym": args.sym,
        "exch_cd": EXCH,
        "minute_bars": len(bars),
    })


if __name__ == "__main__":
    main()
