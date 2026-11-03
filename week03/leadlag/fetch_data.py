"""SGX MKU(미니 원달러) 데이터 받기 — 상품 사양·장운영정보·현재가·지난 1분봉.

사용법 (week03/leadlag/ 에서):
  python3 fetch_data.py                      # 모의 서버, MKUX26, 1분봉 최대 20000개
  python3 fetch_data.py --sym MKUZ26 --bars 5000
  python3 fetch_data.py --exch FCME --prd 6E --sym 6EZ26
  python3 fetch_data.py --krx A756B000       # KRX 미국달러 선물 1분봉 (국내 키, 연계 신호용)

조회 API 만 부릅니다 (주문 없음). 해외 키(~/.nh_futures/credentials_overseas.env)를 씁니다.
--krx 를 주면 국내 키(~/.nh_futures/credentials.env)로 KRX 종목 1분봉만 받습니다.

결과는 results/data/ 에 저장합니다. 원본 응답(JSON)도 함께 남겨 응답 모양을 확인합니다.
"""
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path

from nh_client import NHClient, rows

OUT = Path(__file__).parent / "results" / "data"


def save(name: str, data) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  저장: {path.relative_to(Path(__file__).parent)}")
    return path


def fetch_minutes(client: NHClient, exch: str, sym: str, want: int) -> list[dict]:
    """nxt_key 로 이어 받으며 want 개까지 모읍니다."""
    bars: dict[tuple[str, str], dict] = {}
    nxt = "99999999"
    page = 0
    while len(bars) < want:
        # 국내 차트와 같이 2000개씩 받습니다 (9999 는 모의 서버가 느림)
        body = client.ovs_chart_minute(exch, sym, dcnt=1, req_qty=min(2000, want - len(bars)), nxt_key=nxt)
        page += 1
        if page == 1:
            save(f"raw_chart_minute_{sym}_page1.json", body)  # 응답 모양 확인용
        got = rows(body)
        print(f"  {page}쪽: {len(got)}개 (nxt_key={body.get('nxt_key')!r})")
        if not got:
            break
        before = len(bars)
        for r in got:
            bars[(r.get("ref_dt", ""), r.get("exch_tm", ""))] = r
        new_nxt = str(body.get("nxt_key") or "").strip()
        if len(bars) == before or not new_nxt or new_nxt == nxt or new_nxt.strip("9") == "" or new_nxt.strip("0") == "":
            break
        nxt = new_nxt
    return [bars[k] for k in sorted(bars)]


def fetch_krx_minutes(client: NHClient, sym: str, want: int) -> list[dict]:
    """국내 1분봉. 2000개씩은 2026-11-02 에 30초 시간 초과가 나서 800개씩 받습니다."""
    bars: dict[tuple[str, str], dict] = {}
    nxt = "99999999"
    while len(bars) < want:
        body = client.chart_minute(sym, dcnt=1, req_qty=min(800, want - len(bars)), nxt_key=nxt)
        got = rows(body)
        print(f"  {len(got)}개 (nxt_key={body.get('nxt_key')!r})")
        if not got:
            break
        before = len(bars)
        for r in got:
            bars[(r.get("ref_dt", ""), r.get("exch_tm", ""))] = r
        new_nxt = str(body.get("nxt_key") or "").strip()
        if len(bars) == before or not new_nxt or new_nxt == nxt or new_nxt.strip("9") == "" or new_nxt.strip("0") == "":
            break
        nxt = new_nxt
    return [bars[k] for k in sorted(bars)]


def write_csv(path: Path, bars: list[dict]) -> None:
    fields = ["ref_dt", "exch_tm", "glb_biz_dt", "open_pric", "high_pric", "low_pric", "close_pric",
              "cum_trd_qty", "cum_trd_amt", "diff_rt", "rt"]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(bars)
    print(f"  저장: {path.relative_to(Path(__file__).parent)} ({len(bars)}행)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exch", default="FSGX", help="F+거래소 (기본 FSGX)")
    ap.add_argument("--prd", default="MKU", help="품목코드 (기본 MKU)")
    ap.add_argument("--sym", default="MKUX26", help="종목코드 = 품목+월물 (기본 MKUX26, 11월물)")
    ap.add_argument("--bars", type=int, default=20000, help="받을 1분봉 수 (기본 20000)")
    ap.add_argument("--krx", help="KRX 종목코드 (예: A756B000). 주면 이 종목 1분봉만 받습니다")
    args = ap.parse_args()

    if args.krx:
        client = NHClient(profile="domestic")
        print(f"서버: {client.env} · 국내 키 · KRX {args.krx}")
        bars = fetch_krx_minutes(client, args.krx, args.bars)
        if bars:
            write_csv(OUT / f"bars_1m_{args.krx}.csv", bars)
            print(f"   기간: {bars[0].get('ref_dt')} {bars[0].get('exch_tm')} ~ {bars[-1].get('ref_dt')} {bars[-1].get('exch_tm')}")
        return

    client = NHClient(profile="overseas")
    print(f"서버: {client.env} ({client.base})")
    client.token()
    print(f"토큰 scopes: {client.scope} (S=모의, R=운영)")

    print("1) 품목별 세부정보 (통화)")
    detail = client.ovs_product_detail("FG2")
    save("ovs_product_detail_FG2.json", detail)
    for r in rows(detail):
        if r.get("prd_cd", "").strip() == args.prd:
            print("  ", {k: r.get(k) for k in ("prd_cd", "exch_cd", "ko_prd_nm", "cur_cd",
                                                "ctr_unt", "tick_unt", "tick_val")})

    print("2) 장운영정보")
    ops = client.ovs_market_operations("FG2")
    save("ovs_market_operations_FG2.json", ops)
    for r in rows(ops):
        if r.get("prd_cd", "").strip() == args.prd:
            print("  ", {k: r.get(k) for k in ("prd_cd", "trd_tm_inf", "trd_tm_inf_2", "sett_pric_tm_inf")})

    print("3) 현재가")
    price = client.ovs_price(args.exch, args.sym)
    save(f"price_{args.sym}.json", price)
    p = (rows(price) or [{}])[0]
    print(f"   {p.get('sym_nm')}  현재가 {p.get('last_pric')}  누적거래량 {p.get('cum_trd_qty')}  영업일 {p.get('biz_dt')}")

    print("4) 지난 1분봉")
    bars = fetch_minutes(client, args.exch, args.sym, args.bars)
    if bars:
        write_csv(OUT / f"bars_1m_{args.sym}.csv", bars)
        print(f"   기간: {bars[0].get('ref_dt')} {bars[0].get('exch_tm')} ~ {bars[-1].get('ref_dt')} {bars[-1].get('exch_tm')}")
    save(f"fetch_log_{args.sym}.json", {
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "environment": client.env,
        "exch_cd": args.exch,
        "sym": args.sym,
        "minute_bars": len(bars),
    })


if __name__ == "__main__":
    main()
