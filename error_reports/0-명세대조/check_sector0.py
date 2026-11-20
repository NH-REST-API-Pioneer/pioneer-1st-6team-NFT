"""0번 섹터 후보를 모의 서버 조회 호출로 검증한다 (11/20).

  A. 배열 응답 Occurs1 의 실제 모양 — 국내·해외 조회 API 전부 (웹 샘플은 단일 객체)
  B. 국내_투자자직전매매동향 응답에 Occurs1 / occurs1_ins_buy_net 이 있는지
  C. 해외_현재가 Iccurs1 을 배열·단일 객체 두 형태로 보냈을 때
  D. Content-Type 이 빈칸인 API 에 Content-Type 없이 / 다르게 보냈을 때

주문·정정·취소는 부르지 않는다. 계좌 API 는 값 없이 구조만 저장한다.
국내는 week02/nh_client.py(국내 모의 키), 해외는 week03/nh_client.py(해외 모의 키).

사용법: python3 -I check_sector0.py
"""
import importlib.util
import json
import sys
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 팀 저장소 안(error_reports/<섹터>/)에서도, 로컬 작업 폴더(오류제보/_tools)에서도 돌게 한다
REPO = next((p for p in HERE.parents if (p / "week02" / "nh_client.py").exists()), None) \
    or next(p / "pioneer-1st-6team-NFT" for p in HERE.parents if (p / "pioneer-1st-6team-NFT").exists())
LOG = HERE / "logs"
LOG.mkdir(exist_ok=True)
TODAY = date.today().strftime("%Y%m%d")


def load_client(rel):
    spec = importlib.util.spec_from_file_location(rel.replace("/", "_"), REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


dom_mod = load_client("week02/nh_client.py")
ovs_mod = load_client("week03/nh_client.py")
dom, ovs = dom_mod.NHClient(), ovs_mod.NHClient()
out = {"run_at": datetime.now().isoformat(timespec="seconds"), "server": dom.base, "A": [], "B": None, "C": [], "D": []}


def shape(v):
    if isinstance(v, dict):
        return {k: shape(x) for k, x in v.items()}
    if isinstance(v, list):
        return [shape(v[0]), f"총 {len(v)}개"] if v else []
    return type(v).__name__


def call(client, mod, path, body, kind):
    try:
        return client.post(path, body, kind=kind), None
    except mod.NHError as e:
        return None, f"HTTP {e.status} {e.body[:200]}"


# ---------- A. Occurs1 모양 ----------
fut = dom.post("/trade/v1/domestic/price-all-futures", {"prd": "75"})
dsym = next(r["occurs1_sym_exch"] for r in fut["Occurs1"] if r["occurs1_sym_exch"].startswith("A75"))
D_, O_ = "/trade/v1/domestic/", "/trade/v1/overseas/"
CASES = [
    ("dom", "domestic_price-bid", D_ + "price-bid", {"exch_cd": "KSP", "sym": dsym}, "market", True),
    ("dom", "domestic_chart-daily", D_ + "chart-daily", {"nght_tp": "1", "sym": dsym, "req_qty": "3"}, "market", True),
    ("dom", "domestic_chart-minute", D_ + "chart-minute", {"nght_tp": "1", "sym": dsym, "req_qty": "3", "dcnt": "1"}, "market", True),
    ("dom", "domestic_price-all-futures", D_ + "price-all-futures", {"prd": "75"}, "market", True),
    ("dom", "domestic_price-all-option", D_ + "price-all-option", {"prd": "01", "exp_mon": "261210"}, "market", True),
    ("dom", "domestic_price-expect-fill", D_ + "price-expect-fill", {"sym": dsym}, "market", True),
    ("dom", "domestic_product-detail", D_ + "product-detail", {}, "account", True),
    ("dom", "domestic_open-interest", D_ + "open-interest", {"nxt_key": ""}, "account", False),
    ("dom", "domestic_order-error", D_ + "order-error", {"inq_dt": TODAY, "trd_div_cd": "0", "nxt_key": ""}, "account", False),
    ("dom", "domestic_order-history", D_ + "order-history", {"trd_div": "0", "exec_div": "0", "trd_tm_div_cd": "0", "ord_div_cd": "0", "nxt_key": ""}, "account", False),
    ("dom", "domestic_open-position", D_ + "open-position", {"trd_dt": TODAY, "nxt_key": ""}, "account", False),
    ("ovs", "overseas_price", O_ + "price", {"Iccurs1": [{"iccurs1_exch_cd": "FCME", "iccurs1_sym": "6AZ26"}]}, "market", True),
    ("ovs", "overseas_chart-daily", O_ + "chart-daily", {"exch_cd": "FCME", "sym": "6AZ26", "req_qty": "3"}, "market", True),
    ("ovs", "overseas_chart-minute", O_ + "chart-minute", {"exch_cd": "FCME", "sym": "6AZ26", "req_qty": "3"}, "market", True),
    ("ovs", "overseas_price-all-option", O_ + "price-all-option", {"exch_cd": "OCME", "sym": "O_ESZ26"}, "market", True),
    ("ovs", "overseas_product-detail", O_ + "product-detail", {"nxt_key": ""}, "account", True),
    ("ovs", "overseas_market-operations", O_ + "market-operations", {"nxt_key": ""}, "account", True),
    ("ovs", "overseas_order-error", O_ + "order-error", {"nxt_key": "", "inq_dt": TODAY}, "account", False),
    ("ovs", "overseas_order-history", O_ + "order-history", {"nxt_key": "", "inq_div": "0", "trd_div_cd": "0"}, "account", False),
    ("ovs", "overseas_open-interest", O_ + "open-interest", {"nxt_key": ""}, "account", False),
    ("ovs", "overseas_product-expiry", O_ + "product-expiry", {"nxt_key": "", "rem_dy": "30", "sym": "6AZ26"}, "account", True),
    ("ovs", "overseas_open-interest-expiry", O_ + "open-interest-expiry", {"nxt_key": "", "inq_dy_cnt": "2000"}, "account", False),
    ("ovs", "overseas_cumulative-pl", O_ + "cumulative-pl", {"inq_strt_dt": TODAY[:6] + "01", "inq_ed_dt": TODAY, "cvs_cur_cd": "USD"}, "account", False),
]
for side, name, path, body, kind, public in CASES:
    client, mod = (dom, dom_mod) if side == "dom" else (ovs, ovs_mod)
    res, err = call(client, mod, path, body, kind)
    row = {"api": name, "request": body}
    if err:
        row["error"] = err
        print(f"A {name:32s} {err[:110]}")
    else:
        occ = {k: (f"list[{len(v)}]" if isinstance(v, list) else type(v).__name__) for k, v in res.items() if k.lower().startswith("occurs")}
        row.update(top_keys=sorted(res), occurs=occ, shape=shape(res))
        if public:
            row["sample"] = {k: (v[:1] if isinstance(v, list) else v) for k, v in res.items()}
        print(f"A {name:32s} Occurs: {occ or '없음'}  nrec={res.get('nrec')!r}")
    out["A"].append(row)

# ---------- B. 투자자동향 ----------
res, err = call(dom, dom_mod, D_ + "trend-investor", {"opt_prd": "011", "ref_dt": TODAY, "inq_tp": "1"}, "market")
out["B"] = {"request": {"opt_prd": "011", "ref_dt": TODAY, "inq_tp": "1"}, "error": err,
            "keys": sorted(res) if res else None, "has_Occurs1": bool(res and "Occurs1" in res),
            "has_occurs1_ins_buy_net": bool(res and "occurs1_ins_buy_net" in json.dumps(res)),
            "has_ins_buy_net": bool(res and "ins_buy_net" in res), "sample": res}
print("B trend-investor", err or f"키 {len(res)}개, Occurs1 {'있음' if 'Occurs1' in res else '없음'}, ins_buy_net {'있음' if 'ins_buy_net' in res else '없음'}")

# ---------- C. Iccurs1 형태 ----------
for label, body in (("배열 [ {…} ]", {"Iccurs1": [{"iccurs1_exch_cd": "FCME", "iccurs1_sym": "6AZ26"}]}),
                    ("단일 객체 {…}", {"Iccurs1": {"iccurs1_exch_cd": "FCME", "iccurs1_sym": "6AZ26"}})):
    res, err = call(ovs, ovs_mod, O_ + "price", body, "market")
    summ = err or f"nrec={res.get('nrec')!r} 현재가={(res.get('Occurs1') or [{}])[0].get('occurs1_last_pric') if isinstance(res.get('Occurs1'), list) else res.get('Occurs1')}"
    out["C"].append({"case": label, "request": body, "result": summ, "response": res})
    print("C", label, summ)

# ---------- D. Content-Type ----------
import requests  # noqa: E402

for name, path, body in (("domestic_price-bid", D_ + "price-bid", {"exch_cd": "KSP", "sym": dsym}),
                         ("domestic_order-history", D_ + "order-history", {"trd_div": "0", "exec_div": "0", "trd_tm_div_cd": "0", "ord_div_cd": "0", "nxt_key": ""}),
                         ("domestic_price (대조군, 명세에 application/json)", D_ + "price", {"sym": dsym})):
    for ct in ("application/json", None, "text/plain"):
        dom.throttles["account"].wait()
        h = {"Authorization": f"Bearer {dom.token()}", "appkey": dom.app_key, "appsecret": dom.app_secret}
        if ct:
            h["Content-Type"] = ct
        r = requests.post(dom.base + path, headers=h, data=json.dumps(body), timeout=30)
        try:
            j = r.json()
        except ValueError:
            j = {"text": r.text[:150]}
        brief = {k: v for k, v in j.items() if k in ("errorCode", "errorMessage", "code", "message", "rsp_cd", "msg_cd", "msg_cntn")} or {"키": sorted(j)[:6]}
        out["D"].append({"api": name, "content_type": ct or "(없음)", "status": r.status_code, "brief": brief})
        print(f"D {name:48s} CT={ct or '(없음)':18s} HTTP {r.status_code} {json.dumps(brief, ensure_ascii=False)[:120]}")

f = LOG / f"sector0_{datetime.now():%Y%m%d_%H%M%S}.json"
f.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
print("저장:", f)
