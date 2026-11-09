"""대회용 개장 돌파 후보 종목 1분봉 받기 — 조회 API 만 (주문 없음).

차트(분)는 시간대 필터가 먹지 않아 하루 전체를 받습니다. 시각은 한국 시간. 401(저장 토큰 무효)이면 토큰을 새로 받아 한 번 더.
해외 모의 키는 3주차 leadlag/nh_client.py 가 읽습니다 (고치지 않음).

사용법 (week04/contest_orb/ 에서):  python3 fetch_bars.py [종목 ...] [--since 20260915]
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent.parent / "week03" / "leadlag"))
from nh_client import NHClient, NHError, rows  # noqa: E402

OUT = HERE / "results" / "data"
SYMBOLS = ["SIZ26", "PLF27", "GCZ26", "HGZ26", "CLZ26", "NGZ26", "NQZ26", "6EZ26"]   # HOZ26·RBZ26 은 4주차 폴더에서 복사
FIELDS = ["ref_dt", "exch_tm", "open_pric", "high_pric", "low_pric", "close_pric", "cum_trd_qty", "glb_biz_dt"]


def post(c, body):
    try:
        return c.post("/trade/v1/overseas/chart-minute", body)
    except NHError as e:
        if "HTTP 401" not in str(e):
            raise
        c._token, c._token_until = None, 0.0
        c._token_file().unlink(missing_ok=True)
        c.token()
        return c.post("/trade/v1/overseas/chart-minute", body)


def fetch(c, sym, since):
    bars, nxt = {}, "99999999"
    while True:
        body = post(c, {"exch_cd": "FCME", "sym": sym, "inq_strt_dt": since, "strt_tm": "000000", "inq_ed_dt": "99999999",
                        "ed_tm": "235959", "req_qty": "500", "dcnt": "1", "cidx_yn": "0", "nxt_key": nxt})
        got = rows(body)
        for r in got:
            bars[(r["ref_dt"], r["exch_tm"])] = {k: r.get(k) for k in FIELDS}
        new = str(body.get("nxt_key") or "").strip()
        if not got or not new or new == nxt or new.strip("9") == "" or min(r["ref_dt"] for r in got) < since:
            break
        nxt = new
    out = [bars[k] for k in sorted(bars) if k[0] >= since]
    with open(OUT / f"{sym}.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(out)
    print(f"  {sym}: {len(out)}개 {out[0]['ref_dt']} ~ {out[-1]['ref_dt']}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("syms", nargs="*")
    ap.add_argument("--since", default="20260915")
    a = ap.parse_args()
    c = NHClient(profile="overseas")
    if c.env != "demo":
        raise SystemExit("모의 서버만")
    for s in a.syms or SYMBOLS:
        fetch(c, s, a.since)


if __name__ == "__main__":
    main()
