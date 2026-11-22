"""4번 섹터: 해외 시세 7개 API 가 명세대로 동작하는지 확인 (조회만, 모의 서버, 해외 모의 키).

API 마다
  A. 기준 요청 → 응답 키가 명세 표와 같은지 (최상위·Occurs1 원소), 값 타입, 항상 빈 필드
  B. 필수(Y) 필드를 하나씩 빼고 / 빈 문자열로 → 거절되는지
  C. 설명에 코드값이 적힌 필드에 범위 밖 값 → 거절되는지, 받아들이면 결과가 기준과 같은지
  D. 선택 필드 기본값 / 검색 조건이 실제로 결과를 거르는지
  E. 품목별세부정보·장운영정보 nxt_key 페이지 넘김과 마지막 페이지 표시

다른 팀이 이미 제보한 현재가 거래소 코드·빈 배열·2종목, 차트 nxt_key·건수 상한·종료일·연결 일봉·봉 시각은 다시 보지 않는다.
사용법: python3 -I check_overseas_market.py
"""
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
    return f"HTTP {st} 정상 nrec={j.get('nrec')!r} Occurs1={n} nxt_key={str(j.get('nxt_key', ''))[:20]!r}"


def content(j):
    if not isinstance(j, dict) or "errorCode" in j:
        return ("오류", j.get("errorCode") if isinstance(j, dict) else None)
    return json.dumps({k: v for k, v in j.items() if k != "nxt_key"}, sort_keys=True, ensure_ascii=False)


def key_check(name, j):
    spec = SPEC[name]["response"]
    top = {f["key"] for f in spec if not f["parent"]}
    child = {f["key"] for f in spec if f["parent"] == "Occurs1"}
    occ = j.get("Occurs1")
    rows = occ if isinstance(occ, list) else []
    got_child = set(rows[0]) if rows else set()
    out = {"응답에만": sorted((set(j) - top) | (got_child - child if rows else set())),
           "명세에만": sorted((top - set(j) - ({"Occurs1"} if occ is None else set())) | (child - got_child if rows else set())),
           "문자열 아닌 값": sorted({k for k, v in j.items() if not isinstance(v, (str, list))} |
                               {k for r in rows for k, v in r.items() if not isinstance(v, str)})}
    if rows:
        out["모든 행이 빈 값"] = sorted(k for k in got_child if all(str(r.get(k, "")).strip() == "" for r in rows))
    return out


SYM, EX = "6AZ26", "FCME"
APIS = {
    "해외_현재가": (O + "price", {"Iccurs1": [{"iccurs1_exch_cd": EX, "iccurs1_sym": SYM}]}, {}),
    "해외_호가": (O + "bid-price", {"exch_cd": EX, "sym": SYM}, {"exch_cd": ["FCMEX", "XXXX"], "sym": ["6AZ99"]}),
    "해외_차트(일)": (O + "chart-daily", {"exch_cd": EX, "sym": SYM, "req_qty": "5"}, {"cidx_yn": ["9"], "req_qty": ["abc", "0"]}),
    "해외_차트(분)": (O + "chart-minute", {"exch_cd": EX, "sym": SYM, "req_qty": "5", "dcnt": "1"},
                   {"dcnt": ["0", "abc"], "cidx_yn": ["1", "9"], "req_qty": ["abc"]}),
    "해외_옵션전체시세": (O + "price-all-option", {"exch_cd": "OCME", "sym": "O_ESZ26"}, {"exch_cd": ["FCME", "OXXX"], "sym": ["O_ESZ99"]}),
    "해외_품목별세부정보": (O + "product-detail", {"nxt_key": ""},
                    {"fut_opt_div_cd": ["X"], "prd_grp_cd": ["FG7"], "sett_cd": ["9"], "prd_cd": ["9"], "exch_cd": ["XXX"]}),
    "해외_장운영정보": (O + "market-operations", {"nxt_key": ""},
                  {"fut_opt_div_cd": ["X"], "prd_grp_cd": ["FG7"], "prd_cd": ["9"], "exch_cd": ["XXX"]}),
}
report = {"run_at": datetime.now().isoformat(timespec="seconds"), "server": c.base, "apis": {}}
for name, (path, base, bad) in APIS.items():
    print(f"\n## {name}  {path}")
    row = {"path": path, "base": base}
    st, j = call(path, base)
    ref = content(j)
    row["A"] = brief(st, j)
    print("  A 기준", row["A"])
    if st == 200 and isinstance(j, dict) and j and "errorCode" not in j:
        row["A_keys"] = key_check(name, j)
        print("    키", json.dumps(row["A_keys"], ensure_ascii=False)[:300])
        row["A_sample"] = {k: (v[:2] if isinstance(v, list) else v) for k, v in j.items()}
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
            same = "" if st2 != 200 else (" (기준과 같음)" if content(j2) == ref else " (기준과 다름)")
            row["C"][f"{k}={v}"] = brief(st2, j2) + same
    for k, v in row["C"].items():
        print("  C", k, "→", v)
    report["apis"][name] = row

# ---------- D. 기본값·검색 조건 ----------
print("\n## D 기본값·검색 조건")
D = {}
for name, path, base, dflt in (
        ("해외_차트(일)", O + "chart-daily", {"exch_cd": EX, "sym": SYM, "req_qty": "5"},
         {"inq_strt_dt": "00000000", "inq_ed_dt": "99999999", "cidx_yn": "0", "nxt_key": "99999999"}),
        ("해외_차트(분)", O + "chart-minute", {"exch_cd": EX, "sym": SYM, "req_qty": "5", "dcnt": "1"},
         {"inq_strt_dt": "00000000", "strt_tm": "000000", "inq_ed_dt": "99999999", "ed_tm": "235959", "cidx_yn": "0", "nxt_key": "99999999"})):
    same = content(call(path, base)[1]) == content(call(path, {**base, **dflt})[1])
    D[f"{name} 생략 vs 기본값 명시"] = "같음" if same else "다름"
bars = {}
for d in ("1", "5", "0", "abc", None):
    body = {"exch_cd": EX, "sym": SYM, "req_qty": "4", **({"dcnt": d} if d is not None else {})}
    st, j = call(O + "chart-minute", body)
    bars[str(d) if d is not None else "(생략=기본 5)"] = [(r.get("occurs1_ref_dt"), r.get("occurs1_exch_tm")) for r in j.get("Occurs1", [])] if isinstance(j, dict) else brief(st, j)
D["해외 분봉 dcnt 별 봉 시각"] = bars
st, full = call(O + "product-detail", {"nxt_key": ""})
for k, v, f in (("fut_opt_div_cd", "F", "occurs1_fut_opt_div_cd"), ("prd_grp_cd", "FG2", "occurs1_prd_grp_cd"), ("sett_cd", "1", None),
                ("exch_cd", "CME", "occurs1_exch_cd"), ("cur_cd", "USD", "occurs1_cur_cd")):
    st, j = call(O + "product-detail", {"nxt_key": "", k: v})
    rows = j.get("Occurs1", []) if isinstance(j, dict) else []
    vals = sorted({r.get(f) for r in rows}) if f and rows else None
    D[f"품목별세부정보 {k}={v}"] = {"결과": brief(st, j), "응답의 해당 필드 값들": vals[:8] if vals else vals,
                                "전체와 같음": content(j) == content(full)}
for k, v in (("prd_cd", "1"), ("prd_cd", "2")):
    for nm in ("", "Australian", "6A"):
        st, j = call(O + "market-operations", {"nxt_key": "", k: v, "prd_nm": nm})
        rows = j.get("Occurs1", []) if isinstance(j, dict) else []
        D[f"장운영정보 {k}={v} prd_nm={nm!r}"] = brief(st, j) + (f" 첫 품목 {rows[0].get('occurs1_prd_cd')}/{rows[0].get('occurs1_prd_nm')}" if rows else "")
for k, v in D.items():
    print(" ", k, "→", json.dumps(v, ensure_ascii=False)[:230])
report["D"] = D

# ---------- E. 페이지 넘김 ----------
print("\n## E nxt_key 페이지 넘김")
E = {}
for name, path in (("해외_품목별세부정보", O + "product-detail"), ("해외_장운영정보", O + "market-operations")):
    key, pages, total, seen, keys = "", 0, 0, set(), []
    while pages < 15:
        st, j = call(path, {"nxt_key": key})
        if st != 200 or not isinstance(j, dict) or "errorCode" in j:
            keys.append(brief(st, j)); break
        rows = j.get("Occurs1", [])
        pages += 1; total += len(rows)
        ids = {json.dumps(r, sort_keys=True) for r in rows}
        dup = len(ids & seen); seen |= ids
        nk = str(j.get("nxt_key", ""))
        keys.append({"쪽": pages, "nrec": j.get("nrec"), "행": len(rows), "앞 쪽과 겹침": dup, "nxt_key": nk[:40]})
        if not nk.strip() or nk == key or not rows:
            break
        key = nk
    E[name] = {"쪽 수": pages, "총 행": total, "서로 다른 행": len(seen), "쪽별": keys}
    print(" ", name, json.dumps(E[name], ensure_ascii=False)[:600])
report["E"] = E

out = LOG / f"overseas_market_{datetime.now():%Y%m%d_%H%M%S}.json"
out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
print("\n저장:", out)
