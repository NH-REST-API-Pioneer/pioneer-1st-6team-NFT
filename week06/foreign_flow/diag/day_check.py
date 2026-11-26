"""서버에서 그날 실행 기록·지연 요약. 맥에서:
  ssh root@<서버> 'cd /root/foreign-flow/week06/foreign_flow && .venv/bin/python diag/day_check.py 20261125 20261126'
"""
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, ".")    # foreign_flow 폴더에서 실행 (ssh 로 stdin 에 넣어도 됨)
import analyze  # noqa: E402
import convert  # noqa: E402

L = analyze.HERE / "data" / "live"
print("서버 시각", subprocess.run(["date"], capture_output=True, text=True).stdout.strip())
print("실행기", subprocess.run("pgrep -af 'live.py' | grep -v pgrep", shell=True, capture_output=True, text=True).stdout.strip() or "안 돎")
for day in sys.argv[1:]:
    print(f"\n################ {day}")
    for name in ("order", "order_norisk", "paper"):
        j = L / f"{day}_{name}.jsonl"
        print(f"=== {name}")
        if not j.exists():
            print("기록 없음")
            continue
        recs = [json.loads(x) for x in j.read_text().splitlines() if x.strip()]
        mins = [r for r in recs if r["kind"] == "minute"]
        print(f"분 {len(mins)}, 막힌 분 {sum(1 for r in mins if r.get('stale'))}")
        if name == "order":
            for r in recs:
                t = r.get("text", "")
                if r["kind"] == "event" and not t.startswith("시작 ("):
                    print(" ", r["at"][11:19], t[:90], {k: r[k] for k in ("reject", "error", "pnl", "pos") if k in r})
                if r["kind"] == "order" and not r.get("ok"):
                    print(" ", r["at"][11:19], "주문 실패", r.get("error") or r.get("msg_cntn"))
        c = L / f"{day}_{name}_trades.csv"
        if c.exists():
            t = pd.read_csv(c)
            cols = [x for x in ("entry_t", "exit_t", "side", "n", "z", "stop_pt", "entry_px", "exit_px", "pnl_krw", "why") if x in t]
            t["entry_t"], t["exit_t"] = t.entry_t.str[11:16], t.exit_t.str[11:16]
            print(t[cols].to_string(index=False))
            print(f"합계 {t.pnl_krw.sum():+,.0f}원, {len(t)}건, 계약 {int(t['n'].sum())}, 장중 최저 {t.pnl_krw.cumsum().min():+,.0f}")
        else:
            print("거래 없음")
    try:
        p = convert.build_day(day)
        f = analyze.features(p)
        print("시간대별 막힌 비율", f["stale"].groupby(p.index.hour).mean().round(2).to_dict())
        print("품목별 늦은 비율", {k: round(float(analyze.stale(p, k).mean()), 2) for k in ("011", "051")})
    except Exception as e:  # noqa: BLE001
        print("1분 표 실패", type(e).__name__, e)
