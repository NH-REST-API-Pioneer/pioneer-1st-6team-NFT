"""2번 섹터 평일 장중 추가 조회 검증 (조회 전용, 모의 서버).

1. 만기 지난 종목(A0169000 등) vs 없는 종목(A999X999) vs 정상 종목 대조:
   - 국내_현재가, 국내_호가, 국내_차트(일), 국내_차트(분), 국내_종목예상체결가
   - 해외_현재가, 해외_호가 (명세 샘플 6AU26 vs 없는 종목 6AZ99 vs 정상 6AZ26)
2. R15 평일 장중 재확인:
   - 국내_차트(분) occurs1_rt (USD 선물 A756C000 + 코스피200 선물 A016C000)
   - 국내_옵션전체시세 occurs1_p_theo_pric_chg (261210, 270114 등)
3. 주문 시험 준비용 현재가 조회 (A756C000, 6AZ26)

주문·웹소켓은 호출하지 않는다.
사용법: python3 -I check_expired_and_live.py
"""
import importlib.util
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
REPO = next((p for p in HERE.parents if (p / "week02" / "nh_client.py").exists()), None) \
    or next(p / "pioneer-1st-6team-NFT" for p in HERE.parents if (p / "pioneer-1st-6team-NFT").exists())
LOG = HERE / "logs"
LOG.mkdir(exist_ok=True)


def load_client(rel):
    spec = importlib.util.spec_from_file_location(rel.replace("/", "_")[:-3], REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.NHClient()


dom = load_client("week02/nh_client.py")
ovs = load_client("week03/nh_client.py")
assert dom.env == "demo" and ovs.env == "demo", "모의 서버에서만"

HDR_D = {"Authorization": f"Bearer {dom.token()}", "Content-Type": "application/json"}
HDR_O = {"Authorization": f"Bearer {ovs.token()}", "Content-Type": "application/json"}


def call(client, hdr, path, body):
    time.sleep(0.15)
    r = requests.post(client.base + path, headers=hdr, json=body, timeout=30)
    try:
        j = r.json()
    except ValueError:
        j = {"_text": r.text[:200]}
    return r.status_code, j


def summarize(st, j):
    if not isinstance(j, dict):
        return {"status": st, "raw": str(j)[:200]}
    if "errorCode" in j or "errorMessage" in j:
        return {"status": st, "errorCode": j.get("errorCode"), "errorMessage": j.get("errorMessage"), "errorStatus": j.get("errorStatus")}
    occ = j.get("Occurs1")
    if isinstance(occ, list):
        nonempty = sum(1 for row in occ for v in row.values() if str(v).strip() != "")
        return {"status": st, "nrec": j.get("nrec"), "occurs1_len": len(occ), "occurs1_nonempty_cells": nonempty,
                "top_nonempty": {k: v for k, v in j.items() if k != "Occurs1" and str(v).strip() != ""}}
    total_keys = len(j)
    empty_keys = sum(1 for v in j.values() if str(v).strip() == "")
    nonempty_sample = {k: v for k, v in j.items() if str(v).strip() != ""}
    return {"status": st, "total_keys": total_keys, "empty_keys": empty_keys,
            "nonempty_count": len(nonempty_sample),
            "nonempty_sample": dict(list(nonempty_sample.items())[:8])}


# Find active symbols
fut75 = call(dom, HDR_D, "/trade/v1/domestic/price-all-futures", {"prd": "75"})[1]["Occurs1"]
SYM_USD = next(r["occurs1_sym_exch"] for r in fut75 if r["occurs1_sym_exch"].startswith("A75"))
fut01 = call(dom, HDR_D, "/trade/v1/domestic/price-all-futures", {"prd": "01"})[1]["Occurs1"]
SYM_K200 = next(r["occurs1_sym_exch"] for r in fut01 if r["occurs1_sym_exch"].startswith("A01"))

out = {
    "run_at": datetime.now().isoformat(timespec="seconds"),
    "server": dom.base,
    "active_syms": {"usd": SYM_USD, "k200": SYM_K200, "ovs": "6AZ26"},
    "expired_vs_invalid": {},
    "r15_intraday": {},
    "live_prices": {},
}

# 1. 만기 지난 종목 vs 없는 종목 vs 정상 종목
print("=== 1. 만기 지난 종목(A0169000·A756B000) vs 없는 종목(A999X999) 대조 ===")
dom_syms = [
    ("정상_USD", SYM_USD),
    ("정상_K200", SYM_K200),
    ("만기_A0169000(명세샘플)", "A0169000"),
    ("만기_A756B000(11월물)", "A756B000"),
    ("없는종목_A999X999", "A999X999"),
    ("빈값", ""),
]
for api_nm, path, mk in (
    ("국내_현재가", "/trade/v1/domestic/price", lambda s: {"sym": s}),
    ("국내_호가", "/trade/v1/domestic/price-bid", lambda s: {"exch_cd": "KSP", "sym": s}),
    ("국내_차트(일)", "/trade/v1/domestic/chart-daily", lambda s: {"nght_tp": "1", "sym": s, "req_qty": "3"}),
    ("국내_차트(분)", "/trade/v1/domestic/chart-minute", lambda s: {"nght_tp": "1", "sym": s, "req_qty": "3", "dcnt": "1"}),
    ("국내_종목예상체결가", "/trade/v1/domestic/price-expect-fill", lambda s: {"sym": s}),
):
    out["expired_vs_invalid"][api_nm] = {}
    for lbl, sym in dom_syms:
        st, j = call(dom, HDR_D, path, mk(sym))
        s = summarize(st, j)
        out["expired_vs_invalid"][api_nm][f"{lbl} ({sym})"] = s
        print(f"  {api_nm:12s} {lbl:24s} -> {json.dumps(s, ensure_ascii=False)[:180]}")

# 해외 만기 종목(6AU26) vs 없는 종목(6AZ99) vs 정상(6AZ26)
ovs_syms = [("정상_6AZ26", "6AZ26"), ("만기_6AU26(명세샘플)", "6AU26"), ("없는종목_6AZ99", "6AZ99")]
for api_nm, path, mk in (
    ("해외_현재가", "/trade/v1/overseas/price", lambda s: {"Iccurs1": [{"iccurs1_exch_cd": "FCME", "iccurs1_sym": s}]}),
    ("해외_호가", "/trade/v1/overseas/bid-price", lambda s: {"exch_cd": "FCME", "sym": s}),
):
    out["expired_vs_invalid"][api_nm] = {}
    for lbl, sym in ovs_syms:
        st, j = call(ovs, HDR_O, path, mk(sym))
        s = summarize(st, j)
        out["expired_vs_invalid"][api_nm][f"{lbl} ({sym})"] = s
        print(f"  {api_nm:12s} {lbl:24s} -> {json.dumps(s, ensure_ascii=False)[:180]}")

# 2. R15 평일 장중 재확인
print("\n=== 2. R15 평일 장중 재확인 ===")
for lbl, sym in (("USD선물", SYM_USD), ("코스피200선물", SYM_K200)):
    st, j = call(dom, HDR_D, "/trade/v1/domestic/chart-minute", {"nght_tp": "1", "sym": sym, "req_qty": "30", "dcnt": "1"})
    rows = j.get("Occurs1", [])
    nonempty_rt = [r.get("occurs1_rt") for r in rows if str(r.get("occurs1_rt", "")).strip() != ""]
    nonempty_diff = [r.get("occurs1_diff_rt") for r in rows[:3]]
    out["r15_intraday"][f"차트(분)_{lbl}_{sym}"] = {
        "rows": len(rows), "latest_tm": rows[0].get("occurs1_exch_tm") if rows else None,
        "occurs1_rt_nonempty_count": len(nonempty_rt), "sample_diff_rt": nonempty_diff,
    }
    print(" ", f"차트(분) {lbl}({sym}):", out["r15_intraday"][f"차트(분)_{lbl}_{sym}"])

for exp in ("261210", "270114"):
    st, j = call(dom, HDR_D, "/trade/v1/domestic/price-all-option", {"prd": "01", "exp_mon": exp})
    rows = j.get("Occurs1", [])
    c_chg = sum(1 for r in rows if str(r.get("occurs1_c_theo_pric_chg", "")).strip() != "")
    p_chg = sum(1 for r in rows if str(r.get("occurs1_p_theo_pric_chg", "")).strip() != "")
    p_theo = sum(1 for r in rows if str(r.get("occurs1_p_theo_pric", "")).strip() != "")
    out["r15_intraday"][f"옵션전체시세_01_{exp}"] = {
        "rows": len(rows), "c_theo_pric_chg_nonempty": c_chg,
        "p_theo_pric_nonempty": p_theo, "p_theo_pric_chg_nonempty": p_chg,
    }
    print(" ", f"옵션전체시세 {exp}:", out["r15_intraday"][f"옵션전체시세_01_{exp}"])

# 3. 주문 시험용 현재가 확인
print("\n=== 3. 주문 시험용 현재가 ===")
_, dp = call(dom, HDR_D, "/trade/v1/domestic/price", {"sym": SYM_USD})
out["live_prices"]["domestic_usd"] = {
    "sym": SYM_USD, "sym_nm": dp.get("sym_nm"), "last_pric": dp.get("last_pric"),
    "bid_pric": dp.get("bid_pric"), "ask_pric": dp.get("ask_pric"),
    "lo": dp.get("lo"), "up": dp.get("up"), "exec_tm": dp.get("exec_tm"),
}
_, ob = call(ovs, HDR_O, "/trade/v1/overseas/bid-price", {"exch_cd": "FCME", "sym": "6AZ26"})
out["live_prices"]["overseas_6AZ26"] = {
    "sym": "6AZ26", "last_pric": ob.get("last_pric"), "bid_best_pric_1": ob.get("bid_best_pric_1"),
    "ask_best_pric_1": ob.get("ask_best_pric_1"), "qt_tm": ob.get("qt_tm"),
}
print("  국내 USD:", out["live_prices"]["domestic_usd"])
print("  해외 6AZ26:", out["live_prices"]["overseas_6AZ26"])

f = LOG / f"expired_live_{datetime.now():%Y%m%d_%H%M%S}.json"
f.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
print("\n저장:", f)
