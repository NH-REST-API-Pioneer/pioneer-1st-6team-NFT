"""보조 자료 — 투자자 동향 일별 마감값과 코스피200 선물 일봉 (모의 서버 조회만).

trend-investor 에 과거 날짜(ref_dt)를 넣으면 그날 장 마감 누적이 옵니다. 2026-11-17 확인으로는
9월부터 남아 있는데 드문드문입니다 (10/20 이후 매일, 9/1~10/19 는 35일 중 14일). 값이 없는 날은 tm=000000·0 으로 옵니다.

  .venv/bin/python fetch_daily.py            # → data/daily_investor.csv, data/daily_k200.csv
"""
from __future__ import annotations

import pandas as pd

from nh_client import NHClient, rows
from record import CONTRACTS, INVESTOR_PRDS, HERE

GROUPS = ("ind", "frgn", "scrt", "ins", "invt", "bank", "mb", "penfnd", "cntry", "etc")


def net(body: dict, g: str) -> float:
    k = "ins_buy_net" if g == "ins" else f"{g}_net_buy"      # 보험만 이름이 다름 (명세 그대로)
    return float(body.get(k) or 0)


def main(start: str = "2026-09-01") -> None:
    c = NHClient()
    c.token()
    out = []
    for d in pd.bdate_range(start, pd.Timestamp.now().normalize()):
        dt = d.strftime("%Y%m%d")
        for prd in INVESTOR_PRDS:
            for tp in ("1", "2"):
                b = c.post("/trade/v1/domestic/trend-investor", {"opt_prd": prd, "ref_dt": dt, "inq_tp": tp})
                if b.get("tm") in (None, "000000"):
                    continue
                row = {"date": dt, "prd": prd, "tp": tp, "tm": b.get("tm"),
                       "frgn_buy": float(b.get("frgn_buy") or 0), "frgn_sel": float(b.get("frgn_sel") or 0)}
                row.update({f"{g}_net": net(b, g) for g in GROUPS})
                out.append(row)
    df = pd.DataFrame(out)
    (HERE / "data").mkdir(exist_ok=True)
    df.to_csv(HERE / "data" / "daily_investor.csv", index=False)
    print(f"투자자 동향: {df.date.nunique()}일, {len(df)}줄 → data/daily_investor.csv")

    # 코스피200 선물 일봉 (연결, 주간) — nxt_key 로 이어 받고 날짜 중복 제거
    got, nxt = {}, "99999999"
    for _ in range(10):
        b = c.post("/trade/v1/domestic/chart-daily", {
            "nght_tp": "1", "sym": CONTRACTS["K200"], "inq_strt_dt": "20200101", "inq_ed_dt": "99999999",
            "req_qty": "9999", "cidx_yn": "1", "nxt_key": nxt, "fut_cidx_exp_mod": "0"})
        r = rows(b)
        for x in r:
            got[x["ref_dt"]] = x
        new = str(b.get("nxt_key") or "").strip()
        if not r or not new or new == nxt or new.strip("9") == "":
            break
        nxt = new
    px = pd.DataFrame([got[k] for k in sorted(got)])
    px.to_csv(HERE / "data" / "daily_k200.csv", index=False)
    print(f"코스피200 선물 일봉: {len(px)}일 {px.ref_dt.iloc[0]}~{px.ref_dt.iloc[-1]} → data/daily_k200.csv")


if __name__ == "__main__":
    main()
