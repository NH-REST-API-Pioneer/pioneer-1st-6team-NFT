"""MKU 수수료 확인 — 모의계좌에서 1계약 시장가 매수 → 바로 매도, 단계마다 예수금의 수수료를 비교.

사용법 (week03/leadlag/ 에서):
  python3 fee_test.py           # 조회만: 예수금·수수료·잔고·현재가 (주문 없음)
  python3 fee_test.py --go      # 실제 모의 주문: 매수 1 → 체결 확인 → 매도 1 → 체결 확인

안전장치: 모의 서버에서만 주문, MKU 장중(08:25~20:35)에만, 이미 MKU 포지션이 있으면 멈춤.
결과는 results/fee_test/ 에 JSON 으로 남깁니다 (예수금·주문내역 원본 포함).
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

from nh_client import NHClient, rows

HERE = Path(__file__).parent
FEE_KEYS = ("fee", "fut_fee", "etc_fee", "cls_pl_amt", "dps_tot_amt", "ord_psb_amt", "pl_amt")


def deposit(client: NHClient) -> dict:
    body = client.ovs_deposit("USD")
    return {k: body.get(k) for k in FEE_KEYS} | {"_raw": body}


def mku_position(client: NHClient, sym: str) -> list[dict]:
    return [r for r in rows(client.ovs_open_interest()) if r.get("sym", "").strip() == sym]


def wait_filled(client: NHClient, ord_no: str, timeout: float = 20) -> dict | None:
    """주문내역에서 그 주문번호가 전체체결(ord_sts 6)될 때까지 기다립니다."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        for r in rows(client.ovs_order_history("MKU")):
            if r.get("ord_no", "").strip().lstrip("0") == ord_no.strip().lstrip("0"):
                if r.get("ord_sts") == "6":
                    return r
                if r.get("ord_sts") in ("8", "9"):
                    raise SystemExit(f"주문 {ord_no} 거부/취소: {r.get('err_cd')} {r.get('rejt_rsn') or r.get('err_msg')}")
        time.sleep(1)
    return None


def diff(a: dict, b: dict) -> dict:
    out = {}
    for k in FEE_KEYS:
        try:
            out[k] = round(float(b[k] or 0) - float(a[k] or 0), 4)
        except (TypeError, ValueError):
            out[k] = None
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sym", default="MKUX26")
    ap.add_argument("--go", action="store_true", help="실제로 모의 주문을 냅니다")
    args = ap.parse_args()

    client = NHClient(profile="overseas")
    client.token()
    print(f"서버 {client.env} · scopes {client.scope}")
    p = (rows(client.ovs_price("FSGX", args.sym)) or [{}])[0]
    print(f"현재가 {p.get('last_pric')}  매수 {p.get('bid_pric')} / 매도 {p.get('ask_pric')}  체결시각 {p.get('trd_tm')}")
    d0 = deposit(client)
    print("예수금(USD) 시작:", {k: d0[k] for k in FEE_KEYS})
    pos = mku_position(client, args.sym)
    print("MKU 잔고:", [{k: r.get(k) for k in ("sym", "trd_tp", "opint_qty", "avg_pric", "pl_amt")} for r in pos] or "없음")

    log = {"started": datetime.now().isoformat(timespec="seconds"), "sym": args.sym, "price": p,
           "deposit_start": d0, "go": args.go}
    if args.go:
        hhmm = datetime.now().strftime("%H%M")
        if not ("0825" <= hhmm < "2035"):
            raise SystemExit(f"MKU 주간 세션(08:25~20:35)이 아닙니다: {hhmm}")
        if pos and any(float(r.get("opint_qty") or 0) for r in pos):
            raise SystemExit("이미 MKU 포지션이 있어 멈춥니다 (수수료 비교가 섞임).")

        buy = client.ovs_market_order(args.sym, "buy", 1)
        print("매수 주문:", {k: buy.get(k) for k in ("ord_no", "err_cd", "err_msg")})
        fb = wait_filled(client, buy.get("ord_no", ""))
        print("매수 체결:", fb and {k: fb.get(k) for k in ("exec_pric", "exec_qty", "exec_tm")})
        d1 = deposit(client)
        print("  매수 뒤 변화:", diff(d0, d1))

        sell = client.ovs_market_order(args.sym, "sell", 1)
        print("매도 주문:", {k: sell.get(k) for k in ("ord_no", "err_cd", "err_msg")})
        fs = wait_filled(client, sell.get("ord_no", ""))
        print("매도 체결:", fs and {k: fs.get(k) for k in ("exec_pric", "exec_qty", "exec_tm")})
        time.sleep(2)   # 예수금 반영 여유
        d2 = deposit(client)
        print("  매도 뒤 변화:", diff(d1, d2))
        print("  전체 변화:", diff(d0, d2))
        if fb and fs:
            ticks = (float(fs["exec_pric"]) - float(fb["exec_pric"])) / 0.0001
            print(f"  가격 손익: {ticks:+.0f}틱 = ${ticks * 2.5:+.2f}  → 수수료 = 실현손익 변화와 이 값의 차이로도 확인")
        print("MKU 잔고(끝):", [{k: r.get(k) for k in ("sym", "opint_qty")} for r in mku_position(client, args.sym)] or "없음")
        log |= {"buy": buy, "buy_fill": fb, "deposit_after_buy": d1, "sell": sell, "sell_fill": fs,
                "deposit_end": d2, "diff_total": diff(d0, d2)}

    out = HERE / "results" / "fee_test"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"fee_test_{datetime.now():%Y%m%d_%H%M%S}{'' if args.go else '_dry'}.json"
    path.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"저장: {path.relative_to(HERE)}")


if __name__ == "__main__":
    main()
