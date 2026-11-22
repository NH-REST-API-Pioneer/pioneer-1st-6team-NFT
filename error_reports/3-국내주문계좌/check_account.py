"""3번 섹터: 국내 계좌 조회 API 6개가 명세대로 동작하는지 확인 (주문은 내지 않음, 모의 서버).

  주문가능수량 · 잔고내역 · 주문오류내역 · 주문내역 · 예수금조회 · 미결제약정(영업일 기준)

API 마다
  A. 기준 요청 → 응답 키가 명세 표와 같은지 (최상위·Occurs1 원소), 값 타입
  B. 필수(Y) 필드를 하나씩 빼고 / 빈 문자열로 → 거절되는지
  C. 설명에 코드값이 적힌 필드에 범위 밖 값 → 거절되는지, 조용히 받아들이는지
계좌 값(금액·수량·계좌번호)은 로그에 남기지 않는다. 키·타입·건수만.

사용법: python3 -I check_account.py
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
BIZ = "20261120"  # 마지막 영업일 (금)


def call(path, body):
    time.sleep(0.15)  # 계좌 LIMIT 10/초 아래로
    r = requests.post(c.base + path, headers=HDR, json=body, timeout=40)
    try:
        j = r.json()
    except ValueError:
        j = {"_text": r.text[:200]}
    return r.status_code, j


def brief(status, j):
    if not isinstance(j, dict):
        return f"HTTP {status} (JSON 아님)"
    if "errorCode" in j or "errorMessage" in j:
        return f"HTTP {status} {j.get('errorCode', '')} {str(j.get('errorMessage', ''))[:80]}"
    occ = j.get("Occurs1")
    n = len(occ) if isinstance(occ, list) else ("없음" if occ is None else type(occ).__name__)
    return f"HTTP {status} 정상 nrec={j.get('nrec')!r} Occurs1={n} 키{len(j)}개"


def shape(j):
    """값 없이 구조만: 최상위 키와 타입, Occurs1 원소 키."""
    occ = j.get("Occurs1")
    return {"top": {k: type(v).__name__ for k, v in j.items() if k != "Occurs1"},
            "occurs1_rows": len(occ) if isinstance(occ, list) else None,
            "occurs1_keys": sorted(occ[0]) if isinstance(occ, list) and occ else []}


def key_check(name, j):
    spec = SPEC[name]["response"]
    top = {f["key"] for f in spec if not f["parent"]}
    child = {f["key"] for f in spec if f["parent"] == "Occurs1"}
    occ = j.get("Occurs1")
    got_child = set(occ[0]) if isinstance(occ, list) and occ else set()
    nonstr = sorted({k for k, v in j.items() if not isinstance(v, (str, list))} |
                    ({k for row in occ for k, v in row.items() if not isinstance(v, str)} if isinstance(occ, list) else set()))
    return {"응답에만": sorted((set(j) - top) | (got_child - child if got_child else set())),
            "명세에만(최상위)": sorted(top - set(j) - ({"Occurs1"} if occ is None else set())),
            "명세에만(Occurs1)": sorted(child - got_child) if got_child else "행 없음 (비교 못 함)",
            "문자열 아닌 값": nonstr}


fut = call("/trade/v1/domestic/price-all-futures", {"prd": "75"})[1]
SYM = next(r["occurs1_sym_exch"] for r in fut["Occurs1"] if r["occurs1_sym_exch"].startswith("A75"))
PRICE = next(r["occurs1_last_pric"] for r in fut["Occurs1"] if r["occurs1_sym_exch"] == SYM)

D = "/trade/v1/domestic/"
APIS = {
    "국내_주문가능수량": (D + "orderable-quantity", {"ord_sts": "1", "sym": SYM, "trd_div": "1", "ord_pric": PRICE},
                   {"ord_sts": ["3", "9"], "trd_div": ["9"], "ord_pric": ["0", "abc"]}),
    "국내_잔고내역": (D + "open-interest", {"nxt_key": ""}, {}),
    "국내_주문오류내역": (D + "order-error", {"inq_dt": BIZ, "trd_div_cd": "0", "nxt_key": ""},
                   {"trd_div_cd": ["9"], "inq_dt": ["2026112", "20261122"]}),
    "국내_주문내역": (D + "order-history", {"trd_div": "0", "exec_div": "0", "trd_tm_div_cd": "0", "ord_div_cd": "0", "nxt_key": ""},
                 {"trd_div": ["9"], "exec_div": ["9"], "trd_tm_div_cd": ["2", "9"], "ord_div_cd": ["9"]}),
    "국내_예수금조회": (D + "deposit", {"biz_dt": BIZ}, {"biz_dt": ["2026112", "20991231"]}),
    "국내_미결제약정(영업일 기준)": (D + "open-position", {"trd_dt": BIZ, "nxt_key": ""}, {"trd_dt": ["2026112", "20261122"]}),
}

report = {"run_at": datetime.now().isoformat(timespec="seconds"), "server": c.base, "sym": SYM, "apis": {}}
for name, (path, base, bad) in APIS.items():
    print(f"\n## {name}  {path}")
    row = {"path": path, "base_fields": sorted(base)}
    st, j = call(path, base)
    row["A"] = brief(st, j)
    print("  A 기준", row["A"])
    if st == 200 and isinstance(j, dict) and "errorCode" not in j:
        row["A_keys"] = key_check(name, j)
        row["A_shape"] = shape(j)
        print("    키", json.dumps(row["A_keys"], ensure_ascii=False)[:300])
    row["B"] = {}
    for k in [f["key"] for f in SPEC[name]["request"] if f["required"] == "Y" and not f["parent"]]:
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
    report["apis"][name] = row

out = LOG / f"account_{datetime.now():%Y%m%d_%H%M%S}.json"
out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
print("\n저장:", out)
