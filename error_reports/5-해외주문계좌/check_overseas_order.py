"""5번 섹터: 해외_주문(R06) 필수 여부 모순 검증 — 체결되지 않는 지정가 1계약 (모의 서버, 해외 모의 키).

명세 모순 (R06):
  - sym:        필수 N / 설명 「필수입력」
  - ord_qty:    필수 Y / 설명 「신규, 정정 필수입력」
  - org_ord_no: 필수 Y (샘플·기본값 0) / 설명 「정정, 취소 필수입력」

시험 순서 (모두 CME 장중, 현재가보다 훨씬 낮은 매수 지정가 1계약):
  T1. 신규(qt_div_cd=1)에서 sym 생략 → 거절되는지 (필수 칸 N vs 설명 필수입력)
  T2. 신규(qt_div_cd=1)에서 org_ord_no 생략 → 거절/접수 여부 (필수 칸 Y vs 설명 정정·취소 필수)
  T3. 정상 신규(org_ord_no="0") → 응답 11개 키 대조 + 해외_주문내역 Occurs1 52개 키 대조
  T4. 취소(qt_div_cd=3)에서 ord_qty 생략 → 거절/접수 여부 (필수 칸 Y vs 설명 신규·정정 필수)
  T5. 남은 미체결 전부 정상 취소 + 해외_주문내역/해외_주문오류내역 대조

계좌번호·사용자 ID·IP 는 로그에 남기지 않는다.
사용법: /opt/anaconda3/bin/python -I check_overseas_order.py
"""
import json
import sys
import time
from datetime import date, datetime
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
REPO = next((p for p in HERE.parents if (p / "week03" / "nh_client.py").exists()), None) \
    or next(p / "pioneer-1st-6team-NFT" for p in HERE.parents if (p / "pioneer-1st-6team-NFT").exists())
sys.path.insert(0, str(REPO / "week03"))
from nh_client import NHClient  # noqa: E402

SPEC = json.loads((HERE.parent / "0-명세대조" / "spec" / "spec_web.json").read_text(encoding="utf-8"))
c = NHClient()
assert c.env == "demo", "모의 서버에서만"
HDR = {"Authorization": f"Bearer {c.token()}", "Content-Type": "application/json"}
O = "/trade/v1/overseas/"
log, opened = [], []


def call(path, body, label):
    time.sleep(0.35)  # 주문 LIMIT 5/초 아래로
    r = requests.post(c.base + path, headers=HDR, json=body, timeout=40)
    try:
        j = r.json()
    except ValueError:
        j = {"_text": r.text[:200]}
    sanitized = j if not isinstance(j, dict) or "Occurs1" not in j else {
        **{k: v for k, v in j.items() if k != "Occurs1"},
        "Occurs1": f"{len(j['Occurs1'])}행",
    }
    log.append({"t": datetime.now().isoformat(timespec="seconds"), "case": label,
                "path": path, "request": body, "status": r.status_code, "response": sanitized})
    print(f"{label:48s} HTTP {r.status_code} {json.dumps(sanitized, ensure_ascii=False)[:180]}")
    return r.status_code, j


def accepted(st, j):
    return (st == 200 and isinstance(j, dict)
            and str(j.get("ord_no", "")).strip("0 ") != ""
            and str(j.get("err_cd", "")).strip("0 ") == "")


def keys_vs_spec(api_name, obj, parent=""):
    spec_keys = {f["key"] for f in SPEC[api_name]["response"] if f["parent"] == parent}
    got = set(obj)
    return {
        "응답에만": sorted(got - spec_keys),
        "명세에만": sorted(spec_keys - got),
        "문자열 아닌 값": sorted(k for k, v in obj.items() if not isinstance(v, str)),
    }


def rem_qty(row):
    for k in ("occurs1_rem_qty", "occurs1_unexec_qty", "occurs1_ord_rem_qty", "occurs1_rem_ord_qty"):
        if k in row:
            try:
                return float(str(row[k]).strip() or "0")
            except ValueError:
                pass
    return 0.0


def history(label):
    st, j = call(O + "order-history", {"nxt_key": "", "inq_div": "0", "trd_div_cd": "0"}, label)
    return j.get("Occurs1", []) if isinstance(j, dict) else []


def safe_order_row(r):
    if not r:
        return None
    keep = ("occurs1_ord_no", "occurs1_org_ord_no", "occurs1_sym", "occurs1_ord_sts",
            "occurs1_trd_tp", "occurs1_qt_tp", "occurs1_ord_tp", "occurs1_ord_div",
            "occurs1_ord_pric", "occurs1_ord_qty", "occurs1_exec_qty", "occurs1_unexec_qty",
            "occurs1_rem_qty", "occurs1_cxl_qty", "occurs1_mod_qty")
    return {k: r[k] for k in keep if k in r}


# 준비: 6AZ26 현재가 조회 -> 훨씬 아래 안전한 매수 지정가 (약 6% 아래, 0.00005 단위)
SYM = "6AZ26"
px = requests.post(c.base + O + "bid-price", headers=HDR, json={"exch_cd": "FCME", "sym": SYM}, timeout=30).json()
last = float(px["last_pric"])
# 0.00005 틱 단위 정렬, 현재가보다 약 0.04000(6%) 낮게
lp_val = round(round((last - 0.04) / 0.00005) * 0.00005, 5)
LP = f"{lp_val:.5f}"
print(f"종목 {SYM} 현재가 {last:.5f} → 안전 매수 지정가 {LP} (1계약)")

before_rows = history("H0 시작 전 주문내역")
before_nos = {str(r.get("occurs1_ord_no", "")).lstrip("0") for r in before_rows}

BASE_NEW = {
    "qt_div_cd": "1",
    "sym": SYM,
    "trd_div_cd": "1",
    "glb_ord_tp_cd": "2",
    "ord_qty": "1",
    "ord_pric": LP,
    "stop_pric": "",
    "org_ord_no": "0",
}
result = {"sym": SYM, "last_pric": f"{last:.5f}", "limit_price": LP}

# T1: 신규 sym 생략 (명세 필수 N vs 설명 「필수입력」)
st, j = call(O + "order", {k: v for k, v in BASE_NEW.items() if k != "sym"}, "T1 신규 sym 생략 (명세 N vs 필수입력)")
if accepted(st, j):
    opened.append(j["ord_no"])

# T1b: 신규 sym 빈 문자열
st, j = call(O + "order", {**BASE_NEW, "sym": ""}, "T1b 신규 sym 빈값")
if accepted(st, j):
    opened.append(j["ord_no"])

# T2: 신규 org_ord_no 생략 (명세 필수 Y vs 설명 「정정, 취소 필수입력」)
st, j = call(O + "order", {k: v for k, v in BASE_NEW.items() if k != "org_ord_no"}, "T2 신규 org_ord_no 생략 (명세 Y vs 정정·취소 필수)")
t2_ord = j.get("ord_no") if accepted(st, j) else None
if t2_ord:
    opened.append(t2_ord)
    result["T2_accepted_ord_no"] = t2_ord

# T2b: 신규 org_ord_no 빈 문자열
st, j = call(O + "order", {**BASE_NEW, "org_ord_no": ""}, "T2b 신규 org_ord_no 빈값")
if accepted(st, j):
    opened.append(j["ord_no"])

# T3: 정상 신규 (T2에서 이미 접수된 주문이 없다면 1건만 신규)
target = t2_ord
if not target:
    st, j = call(O + "order", BASE_NEW, "T3 정상 신규 지정가 매수 1 (org_ord_no=0)")
    result["T3_resp_keys"] = keys_vs_spec("해외_주문", j, parent="") if isinstance(j, dict) and "errorCode" not in j else None
    if accepted(st, j):
        target = j["ord_no"]
        opened.append(target)
else:
    result["T2_resp_keys"] = keys_vs_spec("해외_주문", j, parent="") if isinstance(j, dict) and "errorCode" not in j else None

if target:
    time.sleep(2.0)
    rows = history("H1 주문내역 (신규 뒤)")
    my_row = next((r for r in rows if str(r.get("occurs1_ord_no", "")).lstrip("0") == str(target).lstrip("0")), None)
    if not my_row and rows:
        my_row = rows[0]
    result["H1_row"] = safe_order_row(my_row)
    result["H1_keys"] = keys_vs_spec("해외_주문내역", my_row, parent="Occurs1") if my_row else "주문내역에 없음"
    print("  H1 행:", result["H1_row"])
    print("  H1 키 대조:", result["H1_keys"])

    # T4: 취소(qt_div_cd=3)에서 ord_qty 생략 (명세 필수 Y vs 설명 「신규, 정정 필수입력」)
    cxl_no_qty = {
        "qt_div_cd": "3",
        "sym": SYM,
        "trd_div_cd": "1",
        "glb_ord_tp_cd": "2",
        "ord_pric": LP,
        "stop_pric": "",
        "org_ord_no": str(target),
    }
    st, j = call(O + "order", cxl_no_qty, "T4 취소 ord_qty 생략 (명세 Y vs 신규·정정 필수)")
    t4_ok = accepted(st, j)
    result["T4_cancel_without_qty_accepted"] = t4_ok

    # T4b: 취소에서 ord_qty 빈 문자열 (T4 거절 시)
    if not t4_ok:
        st, j = call(O + "order", {**cxl_no_qty, "ord_qty": ""}, "T4b 취소 ord_qty 빈값")
        if accepted(st, j):
            t4_ok = True

# 정리: 이번 시험에서 연 주문 중 남은 미체결 전부 정상 취소
time.sleep(2.0)
rows = history("H2 정리 전 주문내역")
for ord_no in list(dict.fromkeys(opened)):
    r = next((x for x in rows if str(x.get("occurs1_ord_no", "")).lstrip("0") == str(ord_no).lstrip("0")), None)
    # 상태 확인 후 미취소면 정상 취소
    if r is None or rem_qty(r) > 0 or str(r.get("occurs1_ord_sts", "")) not in ("취소", "전량취소", "취소완료"):
        call(O + "order", {
            "qt_div_cd": "3",
            "sym": SYM,
            "trd_div_cd": "1",
            "glb_ord_tp_cd": "2",
            "ord_qty": "1",
            "ord_pric": LP,
            "stop_pric": "",
            "org_ord_no": str(ord_no),
        }, f"T5 정리 정상 취소 {ord_no}")

time.sleep(2.0)
rows_after = history("H3 정리 뒤 주문내역")
result["H3_rows"] = [safe_order_row(r) for r in rows_after if str(r.get("occurs1_ord_no", "")).lstrip("0") not in before_nos]

# E1: 해외_주문오류내역 (오늘)
st, j = call(O + "order-error", {"nxt_key": "", "inq_dt": date.today().strftime("%Y%m%d")}, "E1 해외_주문오류내역 (오늘)")
erows = j.get("Occurs1", []) if isinstance(j, dict) else []
result["E1_rows"] = len(erows)
result["E1_keys"] = keys_vs_spec("해외_주문오류내역", erows[0], parent="Occurs1") if erows else "행 없음"
result["E1_msgs"] = [(r.get("occurs1_err_cd") or r.get("occurs1_msg_cd"), r.get("occurs1_err_msg") or r.get("occurs1_msg")) for r in erows][:10]
print("  E1 오류내역:", result["E1_rows"], "행", result["E1_keys"], result["E1_msgs"])

out = HERE / "logs" / f"overseas_order_{datetime.now():%Y%m%d_%H%M%S}.json"
out.parent.mkdir(exist_ok=True)
out.write_text(json.dumps({"server": c.base, "result": result, "log": log}, ensure_ascii=False, indent=1), encoding="utf-8")
print("\n저장:", out)
