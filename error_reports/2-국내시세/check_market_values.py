"""2번 섹터 보강: 범위 밖 코드값을 오류 없이 받아들인 경우, 그 값이 실제로 쓰이는지(결과가 달라지는지) 비교.

check_market.py 에서 HTTP 200 으로 받아들여진 값마다, 정상 값으로 보낸 응답과 Occurs1 내용을 비교한다.
  같음  → 그 필드 값이 무시되거나, 틀린 값이 기본값으로 처리됨
  다름  → 값이 무언가를 바꿈
또 분봉 dcnt(묶음 개수)가 실제 봉 간격을 바꾸는지, 빈 칸으로만 오는 필드가 더 긴 구간에서도 빈지 본다.

사용법: python3 -I check_market_values.py
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
# 팀 저장소 안(error_reports/<섹터>/)에서도, 로컬 작업 폴더(오류제보/_tools)에서도 돌게 한다
REPO = next((p for p in HERE.parents if (p / "week02" / "nh_client.py").exists()), None) \
    or next(p / "pioneer-1st-6team-NFT" for p in HERE.parents if (p / "pioneer-1st-6team-NFT").exists())
sys.path.insert(0, str(REPO / "week02"))
from nh_client import NHClient  # noqa: E402

c = NHClient()
HDR = {"Authorization": f"Bearer {c.token()}", "Content-Type": "application/json"}
D = "/trade/v1/domestic/"


def call(path, body):
    time.sleep(0.15)
    r = requests.post(c.base + path, headers=HDR, json=body, timeout=40)
    return r.status_code, r.json()


def content(j):
    """비교 대상: 오류면 오류코드, 아니면 nxt_key 를 뺀 전체."""
    if "errorCode" in j:
        return ("오류", j["errorCode"])
    return json.dumps({k: v for k, v in j.items() if k != "nxt_key"}, sort_keys=True, ensure_ascii=False)


fut = call(D + "price-all-futures", {"prd": "75"})[1]
SYM = next(r["occurs1_sym_exch"] for r in fut["Occurs1"] if r["occurs1_sym_exch"].startswith("A75"))
results = {}


def compare(label, path, base, field, values):
    ref = content(call(path, base)[1])
    row = {}
    for v in values:
        st, j = call(path, {**base, field: v})
        got = content(j)
        row[v] = "오류 " + got[1] if isinstance(got, tuple) else ("기준과 같음" if got == ref else "기준과 다름")
    results[label] = {"기준": {field: base.get(field, "(생략)")}, "결과": row}
    print(f"{label:40s} 기준 {field}={base.get(field, '(생략)')!r:12s} → {row}")


compare("호가 exch_cd", D + "price-bid", {"exch_cd": "KSP", "sym": SYM}, "exch_cd", ["NDV", "XXX", "123"])
compare("차트(일) nght_tp", D + "chart-daily", {"nght_tp": "1", "sym": SYM, "req_qty": "5"}, "nght_tp", ["2", "9", "A"])
compare("차트(분) nght_tp", D + "chart-minute", {"nght_tp": "1", "sym": SYM, "req_qty": "5", "dcnt": "1"}, "nght_tp", ["2", "9"])
compare("차트(일) cidx_yn", D + "chart-daily", {"nght_tp": "1", "sym": SYM, "req_qty": "5", "cidx_yn": "0"}, "cidx_yn", ["1", "9"])
compare("차트(일) fut_cidx_exp_mod", D + "chart-daily", {"nght_tp": "1", "sym": SYM, "req_qty": "5", "fut_cidx_exp_mod": "0"}, "fut_cidx_exp_mod", ["1", "9"])
compare("차트(분) dcnt (기준 1)", D + "chart-minute", {"nght_tp": "1", "sym": SYM, "req_qty": "5", "dcnt": "1"}, "dcnt", ["0", "abc", "-1", "5"])
compare("투자자동향 inq_tp", D + "trend-investor", {"opt_prd": "011", "ref_dt": "20261120", "inq_tp": "1"}, "inq_tp", ["2", "9"])
compare("투자자동향 opt_prd", D + "trend-investor", {"opt_prd": "011", "ref_dt": "20261120", "inq_tp": "1"}, "opt_prd", ["019", "012"])
compare("투자자동향 ref_dt (기준 금 11/20)", D + "trend-investor", {"opt_prd": "011", "ref_dt": "20261120", "inq_tp": "1"}, "ref_dt", ["20261122", "20261119", "20991231"])
compare("차트(일) req_qty (기준 5)", D + "chart-daily", {"nght_tp": "1", "sym": SYM, "req_qty": "5"}, "req_qty", ["0", "-1"])

# dcnt 가 실제 봉 간격을 바꾸는지
print()
gaps = {}
for d in ("1", "5", "0", "abc"):
    st, j = call(D + "chart-minute", {"nght_tp": "1", "sym": SYM, "req_qty": "4", "dcnt": d})
    gaps[d] = [r["occurs1_exch_tm"] for r in j.get("Occurs1", [])]
    print(f"dcnt={d!r:6s} 봉 시각 {gaps[d]}")
results["dcnt 봉 시각"] = gaps

# 빈 칸으로만 오는 필드: 더 긴 구간에서
st, j = call(D + "chart-minute", {"nght_tp": "1", "sym": SYM, "req_qty": "300", "dcnt": "1"})
rows = j.get("Occurs1", [])
empty_min = sorted(k for k in rows[0] if all(str(r.get(k, "")).strip() == "" for r in rows)) if rows else []
st, j = call(D + "chart-daily", {"nght_tp": "1", "sym": SYM, "req_qty": "100"})
rows_d = j.get("Occurs1", [])
empty_day = sorted(k for k in rows_d[0] if all(str(r.get(k, "")).strip() == "" for r in rows_d)) if rows_d else []
st, j = call(D + "price-all-option", {"prd": "01", "exp_mon": "261210"})
rows_o = j.get("Occurs1", [])
empty_opt = sorted(k for k in rows_o[0] if all(str(r.get(k, "")).strip() == "" for r in rows_o)) if rows_o else []
results["항상 빈 필드"] = {"차트(분) 300봉": empty_min, "차트(일) 100봉": empty_day, "옵션전체시세 261행": empty_opt}
print("항상 빈 필드", results["항상 빈 필드"])

out = HERE / "logs" / f"market_values_{datetime.now():%Y%m%d_%H%M%S}.json"
out.write_text(json.dumps({"server": c.base, "sym": SYM, "results": results}, ensure_ascii=False, indent=1), encoding="utf-8")
print("저장:", out)
