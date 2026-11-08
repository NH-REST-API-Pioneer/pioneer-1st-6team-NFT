"""NH 해외선물 품목 전체 목록 — 스크리닝 1단계.

품목별세부정보(계약단위·틱)와 장운영정보(거래시간)를 거래소·품목 필터 없이 전부 받아 하나로 합칩니다.
조회만 합니다 (주문 없음). 결과: results/data/products_all.csv, 원본 JSON 도 같이 저장.

  python3 list_products.py
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from nh_client import NHClient, rows

OUT = Path(__file__).parent / "results" / "data"


def fetch_all(client: NHClient, path: str, body: dict) -> list[dict]:
    """nxt_key 로 끝까지 이어 받습니다."""
    out, nxt = [], ""
    for _ in range(50):
        res = client.post(path, {**body, "nxt_key": nxt}, kind="account")
        got = rows(res)
        out += got
        new = str(res.get("nxt_key") or "").strip()
        if not got or not new or new == nxt:
            break
        nxt = new
    return out


def main() -> None:
    client = NHClient()
    client.token()
    print(f"서버 {client.env}, scopes {client.scope}")
    blank = {"cur_cd": "", "exch_cd": "", "fut_opt_div_cd": "F", "prd_cd": "", "prd_grp_cd": "", "prd_nm": ""}
    detail = fetch_all(client, "/trade/v1/overseas/product-detail", {**blank, "sett_cd": ""})
    ops = fetch_all(client, "/trade/v1/overseas/market-operations", blank)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "raw_product_detail_all.json").write_text(json.dumps(detail, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / "raw_market_operations_all.json").write_text(json.dumps(ops, ensure_ascii=False, indent=1), encoding="utf-8")

    hours = {(r.get("exch_cd"), r.get("prd_cd")): r for r in ops}
    fields = ["exch_cd", "prd_cd", "ko_prd_nm", "prd_grp_nm", "cur_cd", "ctr_unt", "tick_unt", "tick_val",
              "trd_tm_inf", "trd_tm_inf_2", "nxd_trd_tm_inf"]
    merged = []
    for r in detail:
        h = hours.get((r.get("exch_cd"), r.get("prd_cd")), {})
        merged.append({**{k: r.get(k, "") for k in fields}, **{k: h.get(k, "") for k in fields[-3:]}})
    with (OUT / "products_all.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(merged)
    print(f"세부정보 {len(detail)}개, 장운영 {len(ops)}개 → results/data/products_all.csv")


if __name__ == "__main__":
    main()
