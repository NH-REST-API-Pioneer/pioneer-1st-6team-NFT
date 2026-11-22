"""5번 섹터: 해외 계좌 조회 API 8개가 명세대로 동작하는지 확인 (주문은 내지 않음, 모의 서버, 해외 모의 키).

  주문가능수량 · 주문오류내역 · 주문내역 · 잔고내역 · 예수금조회 · 만기일조회 · 보유포지션만기조회 · 누적손익현황

API 마다
  A. 기준 요청 → 응답 키가 명세 표와 같은지 (최상위·Occurs1 원소), 값 타입
  B. 필수(Y) 필드를 하나씩 빼고 / 빈 문자열로 → 거절되는지
  C. 설명에 값이 정해진 필드에 범위 밖 값 → 거절되는지, 받아들이면 결과가 기준과 같은지
  D. 명세 기본값·조건부 필수·샘플과 설명이 다른 곳
계좌 값(금액·수량·계좌번호)은 로그에 남기지 않는다. 키·타입·건수, 결과가 같은지만.

다른 팀이 이미 제보한 예수금 biz_dt 빈 문자열(team-04), 장 밖 해외 주문 5016(team-04)은 다시 보지 않는다.
사용법: python3 -I check_overseas_account.py
"""
import hashlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
# 팀 저장소 안(error_reports/<섹터>/)에서도, 로컬 작업 폴더(오류제보/_tools)에서도 돌게 한다
REPO = next((p for p in HERE.parents if (p / "week03" / "nh_client.py").exists()), None) \
    or next(p / "pioneer-1st-6team-NFT" for p in HERE.parents if (p / "pioneer-1st-6team-NFT").exists())
sys.path.insert(0, str(REPO / "week03"))
from nh_client import NHClient  # noqa: E402  (해외 모의 키)

SPEC = json.loads((HERE.parent / "0-명세대조" / "spec" / "spec_web.json").read_text(encoding="utf-8"))
c = NHClient()
HDR = {"Authorization": f"Bearer {c.token()}", "Content-Type": "application/json"}
O = "/trade/v1/overseas/"
LOG = HERE / "logs"
LOG.mkdir(exist_ok=True)
TODAY = datetime.now().strftime("%Y%m%d")
BIZ = "20261120"


def call(path, body):
    time.sleep(0.15)
    r = requests.post(c.base + path, headers=HDR, json=body, timeout=40)
    try:
        j = r.json()
    except ValueError:
        j = {"_text": r.text[:200]}
    return r.status_code, j


def brief(st, j):
    if not isinstance(j, dict) or not j:
        return f"HTTP {st} 본문 {'비어 있음' if not j else 'JSON 아님'}"
    if "errorCode" in j or "errorMessage" in j:
        return f"HTTP {st} {j.get('errorCode', '')} {str(j.get('errorMessage', ''))[:80]}"
    occ = j.get("Occurs1")
    n = len(occ) if isinstance(occ, list) else ("없음" if occ is None else type(occ).__name__)
    return f"HTTP {st} 정상 nrec={j.get('nrec')!r} Occurs1={n} 키{len(j)}개"


def digest(j):
    """값을 남기지 않고 같은지만 비교하는 지문."""
    if not isinstance(j, dict) or "errorCode" in j:
        return "오류"
    return hashlib.md5(json.dumps({k: v for k, v in j.items() if k != "nxt_key"}, sort_keys=True).encode()).hexdigest()[:8]


def key_check(name, j):
    spec = SPEC[name]["response"]
    top = {f["key"] for f in spec if not f["parent"]}
    child = {f["key"] for f in spec if f["parent"] == "Occurs1"}
    occ = j.get("Occurs1")
    rows = occ if isinstance(occ, list) else []
    got_child = set(rows[0]) if rows else set()
    return {"응답에만": sorted((set(j) - top) | (got_child - child if rows else set())),
            "명세에만(최상위)": sorted(top - set(j) - ({"Occurs1"} if occ is None else set())),
            "명세에만(Occurs1)": sorted(child - got_child) if rows else "행 없음 (비교 못 함)",
            "문자열 아닌 값": sorted({k for k, v in j.items() if not isinstance(v, (str, list))} |
                               {k for r in rows for k, v in r.items() if not isinstance(v, str)}),
            "Occurs1 행 수": len(rows)}


APIS = {
    "해외_주문가능수량": (O + "orderable-quantity", {"sym": "6AZ26", "glb_ord_tp_cd": "2", "ord_pric": "0.66000"},
                   {"glb_ord_tp_cd": ["9", "3"], "ord_pric": ["abc", "0"], "sym": ["6AZ99"]}),
    "해외_주문오류내역": (O + "order-error", {"nxt_key": "", "inq_dt": BIZ}, {"inq_dt": ["2026112", "20261122"]}),
    "해외_주문내역": (O + "order-history", {"nxt_key": "", "inq_div": "0", "trd_div_cd": "0"},
                 {"inq_div": ["9"], "trd_div_cd": ["9"], "prd_cd": ["XXX"]}),
    "해외_잔고내역": (O + "open-interest", {"nxt_key": ""}, {}),
    "해외_예수금조회": (O + "deposit", {"cur_cd": "USD", "cvs_div": "1"},
                  {"cvs_div": ["2", "9"], "cur_cd": ["KRW", "XXX"], "biz_dt": [BIZ, "20261122", "2026112"]}),
    "해외_만기일조회": (O + "product-expiry", {"nxt_key": "", "rem_dy": "30", "sym": "6AZ26"},
                  {"fut_opt_div_cd": ["X"], "rem_dy": ["0", "abc", "999"], "sym": ["6AZ99"]}),
    "해외_보유포지션만기조회": (O + "open-interest-expiry", {"nxt_key": "", "inq_dy_cnt": "2000"},
                     {"nxt_key": ["000"], "inq_dy_cnt": ["abc", "0"], "und_sym": ["6A", "XX"]}),
    "해외_누적손익현황": (O + "cumulative-pl", {"inq_strt_dt": "20261101", "inq_ed_dt": BIZ, "cvs_cur_cd": "USD"},
                   {"cvs_cur_cd": ["KRW", "EUR"], "cur_cd": ["XXX"], "inq_strt_dt": ["20261130", "2026110"]}),
}
report = {"run_at": datetime.now().isoformat(timespec="seconds"), "server": c.base, "apis": {}}
for name, (path, base, bad) in APIS.items():
    print(f"\n## {name}  {path}")
    row = {"path": path, "base": base}
    st, j = call(path, base)
    ref = digest(j)
    row["A"] = brief(st, j)
    print("  A 기준", row["A"])
    if st == 200 and isinstance(j, dict) and j and "errorCode" not in j:
        row["A_keys"] = key_check(name, j)
        print("    키", json.dumps(row["A_keys"], ensure_ascii=False)[:320])
    row["B"] = {}
    for k in [f["key"] for f in SPEC[name]["request"] if f["required"] == "Y" and not f["parent"]]:
        if k in base:
            row["B"][f"{k} 없음"] = brief(*call(path, {x: v for x, v in base.items() if x != k}))
            row["B"][f"{k} 빈값"] = brief(*call(path, {**base, k: ""}))
    for k, v in row["B"].items():
        print("  B", k, "→", v)
    row["C"] = {}
    for k, vals in bad.items():
        for v in vals:
            st2, j2 = call(path, {**base, k: v})
            same = "" if st2 != 200 or not isinstance(j2, dict) or "errorCode" in j2 else (" (기준과 같음)" if digest(j2) == ref else " (기준과 다름)")
            row["C"][f"{k}={v}"] = brief(st2, j2) + same
    for k, v in row["C"].items():
        print("  C", k, "→", v)
    report["apis"][name] = row

# ---------- D. 명세에 적힌 조건 ----------
print("\n## D")
D = {}
st, j = call(O + "product-expiry", {"nxt_key": "", "rem_dy": "30"})
D["만기일조회 sym 빼기 (명세 필수)"] = brief(st, j)
st, j = call(O + "orderable-quantity", {"sym": "6AZ26", "glb_ord_tp_cd": "4", "ord_pric": "0.66000"})
D["주문가능수량 STOP-L 인데 stop_pric 없음 (명세: STOP 일 때 필수)"] = brief(st, j)
st, j = call(O + "orderable-quantity", {"sym": "6AZ26", "glb_ord_tp_cd": "1", "ord_pric": "0"})
D["주문가능수량 시장가 ord_pric 0"] = brief(st, j)
st, j = call(O + "cumulative-pl", {"inq_strt_dt": BIZ, "inq_ed_dt": "20261101", "cvs_cur_cd": "USD"})
D["누적손익 시작일 > 종료일"] = brief(st, j)
st, j = call(O + "deposit", {"cur_cd": "USD"})
D["예수금 cvs_div 빼기 (명세 필수)"] = brief(st, j)
for nk in ("", "000"):
    st, j = call(O + "open-interest-expiry", {"nxt_key": nk, "inq_dy_cnt": "2000"})
    D[f"보유포지션만기 nxt_key={nk!r}"] = brief(st, j)
st, j = call(O + "open-interest-expiry", {"nxt_key": ""})
D["보유포지션만기 inq_dy_cnt 생략 (명세 「공백: 2000일」)"] = brief(st, j)
for k, v in D.items():
    print(" ", k, "→", v)
report["D"] = D

out = LOG / f"overseas_account_{datetime.now():%Y%m%d_%H%M%S}.json"
out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
print("\n저장:", out)
