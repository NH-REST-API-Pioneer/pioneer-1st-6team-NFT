"""A2 확인: Occurs1 이 있는 23개 조회 API를 모의 서버로 호출해 응답 구조를 기록한다.

주문·정정·취소는 부르지 않는다 (조회 API만).
계좌 API 응답에는 잔고 등이 있으므로 값은 남기지 않고 키·타입 구조만 저장한다.

사용법: python3 -I check_occurs.py   (팀 저장소 week02/nh_client.py 사용, 모의 서버)
"""
import json
import sys
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 팀 저장소 안(error_reports/<섹터>/)에서도, 로컬 작업 폴더(오류제보/_tools)에서도 돌게 한다
REPO = next((p for p in HERE.parents if (p / "week02" / "nh_client.py").exists()), None) \
    or next(p / "pioneer-1st-6team-NFT" for p in HERE.parents if (p / "pioneer-1st-6team-NFT").exists())
sys.path.insert(0, str(REPO / "week02"))
from nh_client import NHClient, NHError  # noqa: E402

LOG = HERE / "logs"
LOG.mkdir(exist_ok=True)
today = date.today().strftime("%Y%m%d")


def shape(v, depth=0):
    """값을 지우고 구조만 남긴다."""
    if isinstance(v, dict):
        return {k: shape(x, depth + 1) for k, x in v.items()}
    if isinstance(v, list):
        return [shape(v[0], depth + 1), f"... 총 {len(v)}개"] if v else []
    return type(v).__name__


c = NHClient()
fut = c.post("/trade/v1/domestic/price-all-futures", {"prd": "75"})
dom_sym = next(r["occurs1_sym_exch"] for r in fut["Occurs1"] if r["occurs1_sym_exch"].startswith("A75"))
print("국내 종목:", dom_sym)

D, O = "/trade/v1/domestic/", "/trade/v1/overseas/"
CASES = [
    ("domestic_price-bid", D + "price-bid", {"exch_cd": "KSP", "sym": dom_sym}, "market", True),
    ("domestic_chart-daily", D + "chart-daily", {"nght_tp": "1", "sym": dom_sym, "req_qty": "3"}, "market", True),
    ("domestic_chart-minute", D + "chart-minute", {"nght_tp": "1", "sym": dom_sym, "req_qty": "3", "dcnt": "1"}, "market", True),
    ("domestic_price-all-futures", D + "price-all-futures", {"prd": "75"}, "market", True),
    ("domestic_price-all-option", D + "price-all-option", {"prd": "01", "exp_mon": "261112"}, "market", True),
    ("domestic_price-expect-fill", D + "price-expect-fill", {"sym": dom_sym}, "market", True),
    ("domestic_product-detail", D + "product-detail", {}, "account", True),
    ("domestic_price-all-option-night", D + "price-all-option-night", {"prd": "01", "exp_mon": "261112"}, "market", True),
    ("domestic_open-interest", D + "open-interest", {"nxt_key": ""}, "account", False),
    ("domestic_order-error", D + "order-error", {"inq_dt": today, "trd_div_cd": "0", "nxt_key": ""}, "account", False),
    ("domestic_order-history", D + "order-history", {"trd_div": "0", "exec_div": "0", "trd_tm_div_cd": "0", "ord_div_cd": "0", "nxt_key": ""}, "account", False),
    ("overseas_price", O + "price", {"Iccurs1": {"iccurs1_exch_cd": "FCME", "iccurs1_sym": "6AZ26"}}, "market", True),
    ("overseas_chart-daily", O + "chart-daily", {"exch_cd": "FCME", "sym": "6AZ26", "req_qty": "3"}, "market", True),
    ("overseas_chart-minute", O + "chart-minute", {"exch_cd": "FCME", "sym": "6AZ26", "req_qty": "3"}, "market", True),
    ("overseas_price-all-option", O + "price-all-option", {"exch_cd": "OCME", "sym": "O_ESZ26"}, "market", True),
    ("overseas_product-detail", O + "product-detail", {"nxt_key": ""}, "account", True),
    ("overseas_market-operations", O + "market-operations", {"nxt_key": ""}, "account", True),
    ("overseas_order-error", O + "order-error", {"nxt_key": "", "inq_dt": today}, "account", False),
    ("overseas_order-history", O + "order-history", {"nxt_key": "", "inq_div": "0", "trd_div_cd": "0"}, "account", False),
    ("overseas_open-interest", O + "open-interest", {"nxt_key": ""}, "account", False),
    ("overseas_product-expiry", O + "product-expiry", {"nxt_key": "", "rem_dy": "30", "sym": "6AZ26"}, "account", True),
    ("overseas_open-interest-expiry", O + "open-interest-expiry", {"nxt_key": "", "inq_dy_cnt": "2000"}, "account", False),
    ("overseas_cumulative-pl", O + "cumulative-pl", {"inq_strt_dt": today[:6] + "01", "inq_ed_dt": today, "cvs_cur_cd": "USD"}, "account", False),
]

summary = []
for name, path, body, kind, keep_sample in CASES:
    try:
        res = c.post(path, body, kind=kind)
    except NHError as e:
        summary.append({"api": name, "error": f"HTTP {e.status}", "body": e.body[:300]})
        print(f"{name:34s} 오류 HTTP {e.status} {e.body[:120]}")
        continue
    occ = {k: v for k, v in res.items() if k.lower().startswith("occurs")}
    flat = sorted(k for k in res if k.lower().startswith("occurs1_"))
    row = {
        "api": name,
        "request": body,
        "top_keys": sorted(res.keys()),
        "occurs": {k: type(v).__name__ + (f"[{len(v)}]" if isinstance(v, list) else "") for k, v in occ.items()},
        "flat_occurs_keys_at_top": flat,
        "shape": shape(res),
    }
    if keep_sample:  # 시세·상품정보는 공개 데이터라 첫 원소 값까지 남긴다
        row["sample"] = {k: (v[:1] if isinstance(v, list) else v) for k, v in res.items()}
    summary.append(row)
    print(f"{name:34s} Occurs: {row['occurs'] or '없음'}  최상위 occurs1_* 키: {len(flat)}개  "
          f"msg: {res.get('msg_cd', res.get('rsp_cd', ''))} {str(res.get('msg_cntn', res.get('rsp_msg', '')))[:40]}")

out = LOG / f"occurs_{datetime.now():%Y%m%d_%H%M%S}.json"
out.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
print("저장:", out)
