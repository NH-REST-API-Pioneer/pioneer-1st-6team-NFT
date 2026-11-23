"""3번 섹터: 국내 주문 API (신규·정정·취소, 야간) — 체결되지 않는 지정가 주문으로만 시험 (모의 서버).

사용자 확인(11/22): 「미체결 주문만」. 체결이 날 수 있는 시험은 하지 않는다.
  - 가격은 현재가보다 약 15원 낮은 매수 지정가 (체결 안 됨), 1계약
  - 매매구분·주문유형 범위 밖 값은 매도·시장가로 처리될 위험(→ 체결)이 있어 시험하지 않음
  - 마지막에 미체결 주문을 조회해 남은 것은 모두 취소

  O  신규 → 주문내역(목록 필드 대조) → 정정(가격 1틱) → 취소 → 주문내역
  R  거절 시험: 지정가 가격 생략, 체결조건 9, 없는 원주문 취소, 원주문보다 큰 수량으로 정정·취소, 정정 가격 생략, 야간 신규주문(「모의 미지원」)
  E  주문오류내역 목록 필드 대조

계좌번호·계좌명은 로그에 남기지 않는다.
사용법: python3 -I check_orders.py
"""
import json
import sys
import time
from datetime import date, datetime
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
assert c.env == "demo", "모의 서버에서만"
HDR = {"Authorization": f"Bearer {c.token()}", "Content-Type": "application/json"}
D = "/trade/v1/domestic/"
HIDE = {"occurs1_ac_no", "occurs1_ac_nm", "occurs1_user_ip_addr", "occurs1_ord_id"}
log, opened = [], []


def call(path, body, label):
    time.sleep(0.35)  # 주문 LIMIT 5/초 아래로
    r = requests.post(c.base + path, headers=HDR, json=body, timeout=40)
    try:
        j = r.json()
    except ValueError:
        j = {"_text": r.text[:200]}
    log.append({"t": datetime.now().isoformat(timespec="seconds"), "case": label, "path": path,
                "request": body, "status": r.status_code,
                "response": j if "Occurs1" not in j else {**{k: v for k, v in j.items() if k != "Occurs1"}, "Occurs1": f"{len(j['Occurs1'])}행"}})
    short = {k: v for k, v in j.items() if k != "Occurs1"} if isinstance(j, dict) else j
    print(f"{label:44s} HTTP {r.status_code} {json.dumps(short, ensure_ascii=False)[:170]}")
    return r.status_code, j


def accepted(st, j):
    return st == 200 and isinstance(j, dict) and str(j.get("ord_no", "")).strip("0 ") != ""


def history(label):
    st, j = call(D + "order-history", {"trd_div": "0", "exec_div": "0", "trd_tm_div_cd": "0", "ord_div_cd": "0", "nxt_key": ""}, label)
    return j.get("Occurs1", []) if isinstance(j, dict) else []


def row_of(rows, ord_no):
    return next((r for r in rows if str(r.get("occurs1_ord_no", "")).lstrip("0") == str(ord_no).lstrip("0")), None)


def status(r):
    keep = ("occurs1_ord_no", "occurs1_org_ord_no", "occurs1_ord_sts", "occurs1_ord_sts_nm", "occurs1_ord_pric",
            "occurs1_ord_qty", "occurs1_rem_ord_qty", "occurs1_mod_qty", "occurs1_cxl_qty", "occurs1_exec_qty", "occurs1_trd_div")
    return {k: r.get(k) for k in keep} if r else None


def keys_vs_spec(name, row):
    spec = {f["key"] for f in SPEC[name]["response"] if f["parent"] == "Occurs1"}
    got = set(row)
    return {"응답에만": sorted(got - spec), "명세에만": sorted(spec - got),
            "문자열 아닌 값": sorted(k for k, v in row.items() if not isinstance(v, str))}


# ---------- 준비 ----------
fut = requests.post(c.base + D + "price-all-futures", headers=HDR, json={"prd": "75"}, timeout=40).json()
SYM = next(r["occurs1_sym_exch"] for r in fut["Occurs1"] if r["occurs1_sym_exch"].startswith("A75"))
px = requests.post(c.base + D + "price", headers=HDR, json={"sym": SYM}, timeout=40).json()
last, low_limit = float(px["last_pric"]), float(px["lo"])
LP = f"{last - 15:.1f}"
assert float(LP) > low_limit, "하한가 아래"
print(f"종목 {SYM} 현재가 {last} 하한가 {low_limit} → 지정가 매수 {LP}")
def has_rem(r):
    try:
        return float(str(r.get("occurs1_rem_ord_qty", "0")).strip() or "0") > 0
    except ValueError:
        return str(r.get("occurs1_rem_ord_qty", "0")).strip("0. ") != ""


before = [r for r in history("주문내역 (시작 전)") if has_rem(r)]
print("시작 전 미체결", len(before), "건")

NEW = {"sym": SYM, "trd_div": "1", "ord_tp_cd": "1", "ord_qty": "1", "ord_pric": LP, "exec_cnd_cd": "1"}
result = {"sym": SYM, "limit_price": LP}

# ---------- O: 신규 → 정정 → 취소 ----------
st, j = call(D + "order", NEW, "O1 신규 지정가 매수 1")
result["O1_keys"] = sorted(j) if isinstance(j, dict) else None
o1 = j.get("ord_no") if accepted(st, j) else None
if o1:
    opened.append(o1)
    time.sleep(1.5)
    rows = history("H1 주문내역 (신규 뒤)")
    r1 = row_of(rows, o1)
    result["H1_row"] = status(r1)
    result["H1_keys"] = keys_vs_spec("국내_주문내역", r1) if r1 else "주문내역에 없음"
    print("   신규 행", result["H1_row"], "\n   키 대조", result["H1_keys"])
    mod_price = f"{float(LP) - 0.1:.1f}"
    st, j = call(D + "modify", {**NEW, "ord_pric": mod_price, "org_ord_no": o1}, "O2 정정 (가격 1틱 아래)")
    result["O2_keys"] = sorted(j) if isinstance(j, dict) else None
    o2 = j.get("ord_no") if accepted(st, j) else None
    if o2:
        opened.append(o2)
    time.sleep(1.5)
    rows = history("H2 주문내역 (정정 뒤)")
    result["H2_rows"] = [status(row_of(rows, o)) for o in (o1, o2) if o]
    print("   정정 뒤", result["H2_rows"])
    target = o2 or o1
    # R: 큰 수량으로 정정·취소 (원주문 1계약)
    call(D + "modify", {**NEW, "ord_pric": mod_price, "ord_qty": "2", "org_ord_no": target}, "R5 정정 수량 2 (원주문 1)")
    call(D + "cancel", {"sym": SYM, "trd_div": "1", "ord_qty": "2", "org_ord_no": target}, "R6 취소 수량 2 (원주문 1)")
    st, j = call(D + "modify", {k: v for k, v in {**NEW, "org_ord_no": target}.items() if k != "ord_pric"}, "R7 정정 가격 생략 (명세 필수)")
    if accepted(st, j):
        opened.append(j["ord_no"]); target = j["ord_no"]
    st, j = call(D + "cancel", {"sym": SYM, "trd_div": "1", "ord_qty": "1", "org_ord_no": target}, "O3 취소")
    result["O3_keys"] = sorted(j) if isinstance(j, dict) else None
    time.sleep(1.5)
    rows = history("H3 주문내역 (취소 뒤)")
    result["H3_rows"] = [status(r) for r in rows if str(r.get("occurs1_ord_no", "")).lstrip("0") in
                         {str(o).lstrip("0") for o in opened} or str(r.get("occurs1_org_ord_no", "")).lstrip("0") in {str(o).lstrip("0") for o in opened}]
    print("   취소 뒤", result["H3_rows"])

# ---------- R: 거절 시험 ----------
st, j = call(D + "order", {k: v for k, v in NEW.items() if k != "ord_pric"}, "R1 신규 지정가인데 가격 생략 (명세 N)")
if accepted(st, j):
    opened.append(j["ord_no"])
st, j = call(D + "order", {**NEW, "exec_cnd_cd": "9"}, "R2 신규 체결조건 9")
if accepted(st, j):
    opened.append(j["ord_no"])
call(D + "cancel", {"sym": SYM, "trd_div": "1", "ord_qty": "1", "org_ord_no": "9999999"}, "R3 없는 원주문 취소")
st, j = call(D + "order-night", NEW, "R4 야간 신규주문 (「모의 미지원」)")
night = j.get("ord_no") if accepted(st, j) else None

# ---------- E: 주문오류내역 ----------
st, j = call(D + "order-error", {"inq_dt": date.today().strftime("%Y%m%d"), "trd_div_cd": "0", "nxt_key": ""}, "E1 주문오류내역 (오늘)")
erows = j.get("Occurs1", []) if isinstance(j, dict) else []
result["E1_rows"] = len(erows)
result["E1_keys"] = keys_vs_spec("국내_주문오류내역", erows[0]) if erows else "행 없음"
result["E1_msgs"] = [(r.get("occurs1_msg_cd"), r.get("occurs1_msg")) for r in erows][:10]
print("   주문오류내역", result["E1_rows"], "행", result["E1_keys"], result["E1_msgs"])

# ---------- 정리: 미체결 모두 취소 ----------
time.sleep(1.5)
rows = history("정리 전 주문내역")
left = [r for r in rows if has_rem(r) and r not in before]
for r in left:
    call(D + "cancel", {"sym": r["occurs1_sym"], "trd_div": r.get("occurs1_trd_div") or "1",
                        "ord_qty": str(int(float(r["occurs1_rem_ord_qty"]))), "org_ord_no": r["occurs1_ord_no"]}, "정리 취소 " + r["occurs1_ord_no"])
if night:
    call(D + "cancel-night", {"sym": SYM, "trd_div": "1", "ord_qty": "1", "org_ord_no": night}, "정리 야간 취소")
time.sleep(1.5)
rows = history("정리 뒤 주문내역")
still = [status(r) for r in rows if has_rem(r)]
result["남은 미체결"] = still
print("남은 미체결:", still or "없음")

out = HERE / "logs" / f"orders_{datetime.now():%Y%m%d_%H%M%S}.json"
text = json.dumps({"server": c.base, "result": result, "log": log}, ensure_ascii=False, indent=1)
out.write_text(text, encoding="utf-8")
print("저장:", out)
