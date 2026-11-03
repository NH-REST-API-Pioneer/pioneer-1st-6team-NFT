"""실시간 기록(raw.jsonl)을 ws_trader.Engine 에 그대로 다시 흘려 종이 거래를 재현 — 기준값만 바꿔 본다.

사용법 (week03/leadlag/ 에서):
  python3 replay_live.py --day 20261103                   # 충격 기준 0.5·0.6·0.7·0.8·1.0원
  python3 replay_live.py --day 20261103 --move 0.7 -v     # 거래 목록까지

체결은 ws_trader 의 종이 체결과 같다 (신호 순간 반대 호가). 시각은 recv_ts.
손절 횟수 한도는 하루 전체에 적용 (재시작으로 초기화되지 않음).
"""
from __future__ import annotations

import argparse
import json
from dataclasses import replace
from datetime import datetime

from ws_trader import Engine, LiveParams, open_raw


def stream(day: str):
    for line in open_raw(day):
        r = json.loads(line)
        h, b = r["msg"].get("header") or {}, r["msg"].get("body") or {}
        code = h.get("action_code")
        if code not in ("KA", "FB"):
            continue
        t = datetime.fromisoformat(r["recv_ts"])
        yield t.timestamp(), t.strftime("%H%M%S"), code, b


def replay(day: str, p: LiveParams) -> list[dict]:
    e = Engine(p)
    trades, nxt = [], None

    def act(a, now, hhmmss):
        if not a:
            return
        if a["act"] == "enter":
            e.filled_entry(a["side"], a["ref"], now, a["time"], "")
            trades.append({"in": hhmmss, "side": a["side"], "dk": a.get("dk"), "rho": a.get("rho"), "entry": a["ref"]})
        else:
            r = e.filled_exit(a["ref"], now, a["reason"])
            trades[-1].update(out=hhmmss, exit=a["ref"], reason=a["reason"], **r)

    for now, hhmmss, code, b in stream(day):
        nxt = nxt or now
        while nxt <= now:                       # 1초 타이머
            act(e.on_timer(nxt, datetime.fromtimestamp(nxt).strftime("%H%M%S")), nxt, hhmmss)
            nxt += 1
        if code == "KA":
            if b.get("tick_div") == "1" and float(b.get("exec_qty") or 0):
                act(e.on_krx(float(b["last_pric"]), now, b.get("exch_tm", "")[:6]), now, hhmmss)
        else:
            try:
                bid, ask = float(b["bid_pric_1"]), float(b["ask_pric_1"])
            except (KeyError, ValueError):
                continue
            act(e.on_quote(bid, ask, now, b.get("ko_exec_tm", "")[:6]), now, hhmmss)
    return [t for t in trades if "out" in t]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=datetime.now().strftime("%Y%m%d"))
    ap.add_argument("--move", type=float, nargs="*", default=[0.5, 0.6, 0.7, 0.8, 1.0])
    ap.add_argument("-v", action="store_true")
    args = ap.parse_args()
    print(f"{args.day} 종이 거래 재현 (나머지 값은 ws_trader 기본값)")
    for mv in args.move:
        tr = replay(args.day, replace(LiveParams(), move_won=mv))
        net = sum(t["net_usd"] for t in tr)
        win = sum(t["net_usd"] > 0 for t in tr)
        print(f"  {mv:.1f}원: {len(tr):>2}건  {win}승  순손익 ${net:+.1f}  (틱 합 {sum(t['gross_ticks'] for t in tr):+.0f}, 비용 뺀 {sum(t['net_ticks'] for t in tr):+.1f})")
        if args.v:
            for t in tr:
                print(f"     {t['in']}→{t['out']} {'매수' if t['side'] > 0 else '매도'} dk {t['dk']:+.1f} rho {t['rho']:+.2f} "
                      f"{t['reason']} {t['gross_ticks']:+.0f}틱 ${t['net_usd']:+.1f}")


if __name__ == "__main__":
    main()
