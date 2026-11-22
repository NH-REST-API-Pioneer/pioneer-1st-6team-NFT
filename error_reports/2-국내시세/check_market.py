"""2번 섹터: 국내 시세 11개 API 가 명세대로 동작하는지 확인 (조회만, 모의 서버).

API 마다
  A. 기준 요청 → 응답 키가 명세 응답 표와 같은지 (최상위·Occurs1 원소), 값 타입
  B. 필수(Y) 필드를 하나씩 빼고 / 빈 문자열로 → 거절되는지
  C. 설명에 코드값이 적힌 필드에 범위 밖 값 → 거절되는지, 조용히 받아들이는지
  D. 선택(N) 필드 기본값: 빼고 보낸 결과와 명세 기본값을 적어 보낸 결과가 같은지

다른 팀이 이미 제보한 차트 nxt_key·req_qty 상한·기간 조회는 다시 보지 않는다.
사용법: python3 -I check_market.py
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

SPEC = json.loads((HERE.parent / "0-명세대조" / "spec" / "spec_web.json").read_text(encoding="utf-8"))
c = NHClient()
HDR = {"Authorization": f"Bearer {c.token()}", "Content-Type": "application/json"}
LOG = HERE / "logs"
LOG.mkdir(exist_ok=True)


def call(path, body):
    time.sleep(0.15)  # 시세·계좌 LIMIT 10/초 아래로
    r = requests.post(c.base + path, headers=HDR, json=body, timeout=40)
    try:
        j = r.json()
    except ValueError:
        j = {"_text": r.text[:200]}
    return r.status_code, j


def brief(status, j):
    """결과 한 줄: 오류면 코드·메시지, 정상이면 건수."""
    if not isinstance(j, dict):
        return f"HTTP {status} (JSON 아님)"
    if "errorCode" in j or "errorMessage" in j:
        return f"HTTP {status} {j.get('errorCode', '')} {str(j.get('errorMessage', ''))[:70]}"
    occ = j.get("Occurs1")
    n = len(occ) if isinstance(occ, list) else ("없음" if occ is None else type(occ).__name__)
    return f"HTTP {status} 정상 nrec={j.get('nrec')!r} Occurs1={n}"


def fingerprint(j):
    """기본값 비교용: 건수와 첫·끝 원소."""
    if not isinstance(j, dict):
        return None
    occ = j.get("Occurs1")
    if isinstance(occ, list):
        return (len(occ), json.dumps(occ[:1], sort_keys=True), json.dumps(occ[-1:], sort_keys=True))
    return json.dumps({k: v for k, v in j.items() if k not in ("nxt_key",)}, sort_keys=True)[:500]


def key_check(name, j):
    spec = SPEC[name]["response"]
    top = {f["key"] for f in spec if not f["parent"]}
    child = {f["key"] for f in spec if f["parent"] == "Occurs1"}
    got_top = set(j)
    occ = j.get("Occurs1")
    got_child = set(occ[0]) if isinstance(occ, list) and occ else set()
    nonstr = sorted({k for k, v in j.items() if not isinstance(v, (str, list))} |
                    ({k for row in occ for k, v in row.items() if not isinstance(v, str)} if isinstance(occ, list) else set()))
    out = {"응답에만": sorted((got_top - top) | (got_child - child if got_child else set())),
           "명세에만": sorted((top - got_top - ({"Occurs1"} if occ is None else set())) | (child - got_child if got_child else set())),
           "문자열 아닌 값": nonstr}
    if isinstance(occ, list) and occ:
        empty = sorted(k for k in got_child if all(str(row.get(k, "")).strip() == "" for row in occ))
        out["모든 행이 빈 값"] = empty
    return out


fut = call("/trade/v1/domestic/price-all-futures", {"prd": "75"})[1]
SYM = next(r["occurs1_sym_exch"] for r in fut["Occurs1"] if r["occurs1_sym_exch"].startswith("A75"))
REF_DT = "20261120"  # 마지막 영업일 (금)

D = "/trade/v1/domestic/"
APIS = {
    "국내_현재가": (D + "price", {"sym": SYM}, {}),
    "국내_호가": (D + "price-bid", {"exch_cd": "KSP", "sym": SYM}, {"exch_cd": ["NDV", "XXX"]}),
    "국내_차트(일)": (D + "chart-daily", {"nght_tp": "1", "sym": SYM, "req_qty": "5"},
                   {"nght_tp": ["2", "9"], "cidx_yn": ["9"], "fut_cidx_exp_mod": ["9"], "req_qty": ["0", "abc"]}),
    "국내_차트(분)": (D + "chart-minute", {"nght_tp": "1", "sym": SYM, "req_qty": "5", "dcnt": "1"},
                   {"nght_tp": ["2", "9"], "dcnt": ["0", "abc"], "cidx_yn": ["9"], "req_qty": ["0"]}),
    "국내_선물전체시세": (D + "price-all-futures", {"prd": "75"}, {"prd": ["99", "XX"]}),
    "국내_옵션전체시세": (D + "price-all-option", {"prd": "01", "exp_mon": "261210"}, {"prd": ["99"], "exp_mon": ["991231", "2612"]}),
    "국내_투자자직전매매동향": (D + "trend-investor", {"opt_prd": "011", "ref_dt": REF_DT, "inq_tp": "1"},
                        {"opt_prd": ["019", "999"], "inq_tp": ["9"], "ref_dt": ["20261122", "2026112"]}),
    "국내_종목예상체결가": (D + "price-expect-fill", {"sym": SYM}, {}),
    "국내_품목별세부정보": (D + "product-detail", {}, {}),
    "국내_현재가(야간)": (D + "price-night", {"sym": SYM}, {}),
    "국내_옵션전체시세(야간)": (D + "price-all-option-night", {"prd": "01", "exp_mon": "261210"}, {}),
}
# 기본값 시험: 선택 필드를 빼고 보낸 것 vs 명세 기본값을 적어 보낸 것
DEFAULTS = {
    "국내_차트(일)": {"inq_strt_dt": "00000000", "inq_ed_dt": "99999999", "cidx_yn": "0", "nxt_key": "99999999", "fut_cidx_exp_mod": "0"},
    "국내_차트(분)": {"inq_strt_dt": "00000000", "strt_tm": "000000", "inq_ed_dt": "99999999", "ed_tm": "235959",
                   "cidx_yn": "0", "nxt_key": "99999999", "fut_cidx_exp_mod": "0"},
    "국내_옵션전체시세": {"nxt_stkr_pric": "99999999"},
    "국내_투자자직전매매동향": {"inq_tp": "1"},
}

report = {"run_at": datetime.now().isoformat(timespec="seconds"), "server": c.base, "sym": SYM, "apis": {}}
for name, (path, base, bad) in APIS.items():
    print(f"\n## {name}  {path}")
    row = {"path": path, "base": base}
    st, j = call(path, base)
    row["A"] = brief(st, j)
    print("  A 기준", row["A"])
    if st == 200 and isinstance(j, dict) and "errorCode" not in j:
        row["A_keys"] = key_check(name, j)
        print("    키", json.dumps(row["A_keys"], ensure_ascii=False)[:300])
        occ = j.get("Occurs1")
        row["A_sample"] = {k: (v[:2] if isinstance(v, list) else v) for k, v in j.items()}
    req = [f["key"] for f in SPEC[name]["request"] if f["required"] == "Y" and not f["parent"]]
    row["B"] = {}
    for k in req:
        if k not in base:
            continue
        row["B"][f"{k} 없음"] = brief(*call(path, {x: v for x, v in base.items() if x != k}))
        row["B"][f"{k} 빈값"] = brief(*call(path, {**base, k: ""}))
    for k, v in row["B"].items():
        print("  B", k, "→", v)
    row["C"] = {}
    for k, vals in bad.items():
        for v in vals:
            row["C"][f"{k}={v}"] = brief(*call(path, {**base, k: v}))
    for k, v in row["C"].items():
        print("  C", k, "→", v)
    if name in DEFAULTS:
        s1, j1 = call(path, base)
        s2, j2 = call(path, {**base, **DEFAULTS[name]})
        same = fingerprint(j1) == fingerprint(j2)
        row["D"] = {"생략 vs 기본값 명시": "같음" if same else "다름", "생략": brief(s1, j1), "명시": brief(s2, j2)}
        print("  D 기본값", row["D"])
    report["apis"][name] = row

out = LOG / f"market_{datetime.now():%Y%m%d_%H%M%S}.json"
out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
print("\n저장:", out)
