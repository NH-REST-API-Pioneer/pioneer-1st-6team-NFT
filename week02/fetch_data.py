"""미국달러 선물 데이터 받기 — 품목 정보·월물 목록·현재가·지난 1분봉.

사용법 (week02/ 에서):
  python3 fetch_data.py                 # 모의 서버, 최근월물 자동 선택, 1분봉 최대 20000개
  python3 fetch_data.py --bars 5000
  python3 fetch_data.py --sym A756B000  # 종목코드 직접 지정

조회 API 만 부릅니다 (주문 없음).

결과는 results/data/ 에 저장합니다. 원본 응답(JSON)도 함께 남겨 응답 모양을 확인합니다.
"""
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path

from nh_client import NHClient, rows

USD_PRD = "75"  # 품목코드 75 = USD (price-all-futures 명세)
OUT = Path(__file__).parent / "results" / "data"


def save(name: str, data) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  저장: {path.relative_to(Path(__file__).parent)}")
    return path


def pick_front_month(futures: list[dict]) -> dict:
    """일반 미국달러 선물(A75…) 중 만기년월이 가장 가까운 월물 (= 최근월물).

    품목 75 목록에는 「미국달러 플렉스 F」(G75…, 일자별 만기) 200여 개가 섞여 오므로 뺍니다.
    """
    live = [f for f in futures
            if f.get("exp_mon") and str(f.get("sym_exch", "")).startswith("A75")
            and "플렉스" not in str(f.get("sym_nm", ""))]
    if not live:
        raise SystemExit("월물 목록이 비었습니다. results/data/usd_futures_all.json 원본을 확인하세요.")
    return min(live, key=lambda f: f["exp_mon"])


def fetch_minutes(client: NHClient, sym: str, want: int) -> list[dict]:
    """nxt_key 로 이어 받으며 want 개까지 모읍니다."""
    bars: dict[tuple[str, str], dict] = {}
    nxt = "99999999"
    page = 0
    while len(bars) < want:
        # 한 번에 9999개를 달라고 하면 모의 서버가 10초 안에 답하지 못해 2000개씩 받습니다
        body = client.chart_minute(sym, dcnt=1, req_qty=min(2000, want - len(bars)), nxt_key=nxt)
        page += 1
        if page == 1:
            save(f"raw_chart_minute_{sym}_page1.json", body)  # 응답 모양 확인용
        got = rows(body)
        print(f"  {page}쪽: {len(got)}개 (nxt_key={body.get('nxt_key')!r})")
        if not got:
            break
        for r in got:
            bars[(r.get("ref_dt", ""), r.get("exch_tm", ""))] = r
        new_nxt = str(body.get("nxt_key") or "").strip()
        if not new_nxt or new_nxt == nxt or new_nxt.strip("9") == "":
            break
        nxt = new_nxt
    return [bars[k] for k in sorted(bars)]


def write_csv(path: Path, bars: list[dict]) -> None:
    fields = ["ref_dt", "exch_tm", "open_pric", "high_pric", "low_pric", "close_pric",
              "cum_trd_qty", "cum_trd_amt", "opint", "diff_rt", "rt"]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(bars)
    print(f"  저장: {path.relative_to(Path(__file__).parent)} ({len(bars)}행)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sym", help="종목코드 (안 주면 최근월물 자동 선택)")
    ap.add_argument("--bars", type=int, default=20000, help="받을 1분봉 수 (기본 20000)")
    args = ap.parse_args()

    client = NHClient()
    print(f"서버: {client.env} ({client.base})")
    client.token()
    print(f"토큰 scopes: {client.scope} (S=모의, R=운영)")

    print("1) 품목별 세부정보")
    detail = client.product_detail()
    save("product_detail.json", detail)
    usd = [r for r in rows(detail) if "달러" in str(r.get("dtl_cd_ko_nm", ""))]
    for r in usd:
        print("  ", {k: r.get(k) for k in ("dtl_cd_ko_nm", "cd_val_inf_1", "tick_val", "mltplr",
                                            "cd_val_inf_4", "sett_nm", "exp_dt", "init_mrgn")})

    print("2) 미국달러 선물 월물 목록")
    allf = client.all_futures(USD_PRD)
    save("usd_futures_all.json", allf)
    futures = rows(allf)
    regular = [f for f in futures if "플렉스" not in str(f.get("sym_nm", ""))]
    print(f"   전체 {len(futures)}개 중 일반 선물 {len(regular)}개 (플렉스 제외)")
    for f in sorted(regular, key=lambda f: f.get("exp_mon", ""))[:4]:
        print(f"   {f.get('exp_mon')}  {f.get('sym_exch')}  {f.get('sym_nm')}  현재가 {f.get('last_pric')}")
    sym = args.sym or pick_front_month(futures)["sym_exch"]
    print(f"   → 사용할 종목: {sym}")

    print("3) 현재가")
    price = client.price(sym)
    save(f"price_{sym}.json", price)
    print(f"   {price.get('sym_nm')}  현재가 {price.get('last_pric')}  잔존일수 {price.get('rem_dy')}")

    print("4) 지난 1분봉")
    bars = fetch_minutes(client, sym, args.bars)
    if bars:
        write_csv(OUT / f"bars_1m_{sym}.csv", bars)
        print(f"   기간: {bars[0].get('ref_dt')} {bars[0].get('exch_tm')} ~ {bars[-1].get('ref_dt')} {bars[-1].get('exch_tm')}")
    save("fetch_log.json", {
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "environment": client.env,
        "sym": sym,
        "minute_bars": len(bars),
    })


if __name__ == "__main__":
    main()
