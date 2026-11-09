"""앱 리플레이로 `strategy_orb_hg.py` 백테스트 — 앱의 1분봉 실행기(`backend.team_pyrun.replay.replay`)를 그대로 씀.

앱 코드는 고치지 않고 불러 쓰기만 합니다. 앱의 「파이썬 전략」 시장 목록에는 아직 구리가 없어서(`markets.market_of` 가
A75·MKU 만 받음), 리플레이 함수에 **구리 시장 값**(틱 0.0005 · US$12.5 · 종이 수수료 편도 $8 · 불리한 쪽 1틱)을
직접 넘깁니다. 체결 순서는 앱 그대로입니다: 그 분 시가로 진입·청산 → 종가와 고저로 손절·익절 → 그 봉으로 신호.

그 뒤 `orb_backtest.py` 의 구리 런던 08:00 거래(results/backtest/trades.csv)와 날마다 맞춰 봅니다.

사용법 (앱 폴더의 파이썬 환경으로):
  cd ~/Documents/NH-open-api-app && uv run python <이 폴더>/app_replay.py
  → results/app_replay/{journal.jsonl, summary.json, run.json, compare.csv, compare.json}
앱 폴더가 다른 곳이면 환경변수 NH_APP=<앱 폴더>.
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = Path(os.environ.get("NH_APP", Path.home() / "Documents" / "NH-open-api-app"))
sys.path.insert(0, str(APP))

from backend.team_pyrun.loader import load  # noqa: E402
from backend.team_pyrun.markets import Market  # noqa: E402
from backend.team_pyrun.replay import replay  # noqa: E402
from backend.team_pyrun.runner import Minute  # noqa: E402

SYM = "HGZ26"
DATA = HERE / "results" / "data" / f"{SYM}.csv"
BT = HERE / "results" / "backtest" / "trades.csv"
OUT = HERE / "results" / "app_replay"
FIRST_DAY, LAST_DAY, TEST_FROM = "20261001", "20261106", "20261026"   # orb_backtest.py 와 같은 기간
FEE = 8.0

#: 구리 시장 값 — 앱 `markets.OVERSEAS`(MKU) 꼴에 HG 틱·수수료만 바꾼 것. 리플레이는 휴장을 막지 않음(breaks 안 씀)
HG = Market(id="overseas", currency="USD", end_time="235500", session_start="160000", breaks=(),
            paper_fee=FEE, paper_slip_ticks=1, tick=0.0005, tick_value=12.5, min_day_bars=1)


def weekday(d: str) -> int:
    return date(int(d[:4]), int(d[4:6]), int(d[6:])).weekday()


def minutes() -> list[Minute]:
    """orb_backtest.load 와 같은 거르기: 숫자 아닌 줄·종가 0 은 뺌, 기간 안 평일만."""
    out = []
    for r in csv.DictReader(open(DATA)):
        d = r["ref_dt"]
        if not (FIRST_DAY <= d <= LAST_DAY) or weekday(d) >= 5:
            continue
        try:
            o, h, lo, c = (float(r[k]) for k in ("open_pric", "high_pric", "low_pric", "close_pric"))
        except ValueError:
            continue
        if c > 0:
            out.append(Minute(d, r["exch_tm"], o, h, lo, c, float(r["cum_trd_qty"] or 0)))
    return out


def signal_ranges(journal: Path) -> dict[str, dict]:
    """journal 의 진입 신호 줄(side 있음) → 날짜별 범위 끝·폭 (R 계산용)."""
    got = {}
    for line in journal.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("kind") == "signal" and row.get("side"):
            got[row["date"]] = row
    return got


def main() -> None:
    loaded = load(HERE, "strategy_orb_hg.py")          # 앱 로더의 약속 검사 그대로
    if OUT.exists():
        shutil.rmtree(OUT)
    mins = minutes()
    res = replay(loaded, None, mins, OUT, sym=SYM, market=HG, fee=FEE, slip_ticks=1)
    sig = signal_ranges(OUT / "journal.jsonl")

    bt = {r["day"]: r for r in csv.DictReader(open(BT)) if r["sym"] == SYM and r["london"] == "0800"}
    app = {}
    for t in res["trades"]:
        d = t["entry_date"]
        s = sig[d]
        stop = s["range_low"] if t["side"] == "buy" else s["range_high"]
        stop_ticks = abs(t["entry"] - stop) / HG.tick + 1
        risk = stop_ticks * HG.tick_value + 2 * FEE
        app[d] = {**t, "R": t["pnl_after_fee"] / risk}

    rows = []
    for d in sorted(set(bt) | set(app)):
        b, a = bt.get(d), app.get(d)
        b_trade = b is not None and not b.get("skip")
        row = {"day": d, "test": d >= TEST_FROM, "fri": weekday(d) == 4}
        if b_trade:
            row.update(bt_side="buy" if b["side"] == "1" else "sell", bt_entry=b["entry_t"], bt_exit=b["exit_t"],
                       bt_why=b["why"], bt_ticks=float(b["ticks"]), bt_usd=float(b["usd 공식 $8"]),
                       bt_R=float(b["R 공식 $8"]))
        elif b is not None:
            row["bt_why"] = b["skip"]
        if a:
            row.update(app_side=a["side"], app_entry=a["entry_time"], app_exit=a["exit_time"], app_why=a["reason"],
                       app_ticks=a["ticks"], app_usd=round(a["pnl_after_fee"], 2), app_R=round(a["R"], 3))
        same = (b_trade and a and row["bt_side"] == a["side"] and row["bt_entry"] == a["entry_time"]
                and row["bt_exit"] == a["exit_time"])
        row["same_trade"] = bool(same)
        row["tick_diff"] = round(a["ticks"] - row["bt_ticks"], 1) if b_trade and a else None
        rows.append(row)

    keys = ["day", "test", "fri", "same_trade", "tick_diff", "bt_side", "app_side", "bt_entry", "app_entry",
            "bt_exit", "app_exit", "bt_why", "app_why", "bt_ticks", "app_ticks", "bt_usd", "app_usd", "bt_R", "app_R"]
    with open(OUT / "compare.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    def part(name: str, pick) -> dict:
        rs = [r for r in rows if pick(r)]
        a = [r for r in rs if "app_usd" in r]
        b = [r for r in rs if "bt_usd" in r]
        return {"기간": name, "앱 거래": len(a), "앱 승": sum(r["app_usd"] > 0 for r in a),
                "앱 USD": round(sum(r["app_usd"] for r in a), 2), "앱 R": round(sum(r["app_R"] for r in a), 2),
                "앱 최악 USD": min((r["app_usd"] for r in a), default=None),
                "백테스트 거래": len(b), "백테스트 USD": round(sum(r["bt_usd"] for r in b), 2),
                "백테스트 R": round(sum(r["bt_R"] for r in b), 2),
                "같은 거래(방향·진입·청산 시각)": sum(r["same_trade"] for r in rs)}

    summary = [part("학습 ~10/23", lambda r: not r["test"]), part("검증 10/26~", lambda r: r["test"]),
               part("금요일", lambda r: r["fri"]), part("전체", lambda r: True)]
    why = {}
    for r in rows:
        if "app_why" in r:
            why[r["app_why"]] = why.get(r["app_why"], 0) + 1
    (OUT / "compare.json").write_text(json.dumps({
        "strategy": loaded.file, "sha256": loaded.sha256, "market": {"sym": SYM, "tick": HG.tick,
        "tick_value": HG.tick_value, "fee_per_fill": FEE, "slip_ticks": 1}, "days": [FIRST_DAY, LAST_DAY],
        "summary": summary, "app_exit_reasons": why}, ensure_ascii=False, indent=1))

    print(f"{loaded.name}: 봉 {len(mins):,}개, 앱 거래 {len(app)}건")
    for s in summary:
        print(f"  {s['기간']:10s} 앱 {s['앱 거래']:2d}건 {s['앱 승']:2d}승 {s['앱 USD']:+9,.2f} USD {s['앱 R']:+6.2f}R"
              f"  | 백테스트 {s['백테스트 거래']:2d}건 {s['백테스트 USD']:+9,.2f} USD {s['백테스트 R']:+6.2f}R"
              f"  | 같은 거래 {s['같은 거래(방향·진입·청산 시각)']}")
    print(f"  앱 청산 사유: {why}")
    diff = [r for r in rows if not r["same_trade"] or r["tick_diff"]]
    if diff:
        print("  백테스트와 다른 날:")
        for r in diff:
            print(f"    {r['day']} 백테스트 {r.get('bt_side', '-')} {r.get('bt_entry', '')}→{r.get('bt_exit', '')} "
                  f"{r.get('bt_why', '')} {r.get('bt_ticks', '')}틱 | 앱 {r.get('app_side', '-')} "
                  f"{r.get('app_entry', '')}→{r.get('app_exit', '')} {r.get('app_why', '')} {r.get('app_ticks', '')}틱")


if __name__ == "__main__":
    main()
