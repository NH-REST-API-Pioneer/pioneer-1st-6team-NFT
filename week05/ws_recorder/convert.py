"""기록 원문(data/raw/YYYYMMDD/HH.jsonl.gz)을 종목별 표로 바꿈.

  data/csv/YYYYMMDD/trades_<월물>.csv.gz  체결(FA) 한 건 = 한 줄
  data/csv/YYYYMMDD/quotes_<월물>.csv.gz  호가(FB) 바뀔 때마다 한 줄, 매수·매도 5단계 가격·잔량·건수

열 뜻:
  recv_ts      이 컴퓨터가 받은 시각 (한국시간, ms)
  ko_exec_tm   NH 가 붙인 한국시간 (HHMMSS, 초 단위)
  exch_tm      거래소 시각 (HHMMSS, 미국 중부시간)
  last_pric / exec_qty / exec_div   체결가 / 체결수량 / 체결 방향(+ 매수 주도, - 매도 주도)
  bid_pric_1~5 / bid_qty_1~5 / bid_cnt_1~5   매수 1~5호가 가격 / 잔량 / 주문 건수 (ask_ 는 매도)
  bid_qty / ask_qty (quotes)        5단계 잔량 합

사용법 (week05/ws_recorder/ 에서):
  .venv/bin/python convert.py              # 기록된 날 전부
  .venv/bin/python convert.py 20261116     # 그날만
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import sys
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW_DIR = HERE / "data" / "raw"
CSV_DIR = HERE / "data" / "csv"

TRADE_COLS = ["recv_ts", "ref_dt", "exch_tm", "ko_exec_tm", "last_pric", "exec_qty", "exec_div", "cum_trd_qty",
              "bid_best_pric", "ask_best_pric", "bid_qty", "ask_qty", "seq_no"]
QUOTE_COLS = (["recv_ts", "exch_tm", "ko_exec_tm"]
              + [f"{s}_{k}_{i}" for s in ("bid", "ask") for k in ("pric", "qty", "cnt") for i in range(1, 6)]
              + ["bid_qty", "ask_qty", "bid_cnt", "ask_cnt"])
KINDS = {"FA": ("trades", TRADE_COLS), "FB": ("quotes", QUOTE_COLS)}


def _lines(p: Path):
    """한 줄씩. 기록 중에 끊긴 .gz 의 망가진 끝부분과 덜 쓴 마지막 줄은 버림."""
    opener = gzip.open if p.suffix == ".gz" else open
    try:
        with opener(p, "rt", encoding="utf-8") as f:
            for line in f:
                yield line
    except (EOFError, zlib.error, gzip.BadGzipFile, io.UnsupportedOperation):
        return


def convert_day(raw_day: Path, out_day: Path) -> dict[str, int]:
    out_day.mkdir(parents=True, exist_ok=True)
    files: dict[str, tuple] = {}
    counts: dict[str, int] = {}
    try:
        for p in sorted(raw_day.glob("*.jsonl*")):
            for line in _lines(p):
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                msg = rec.get("msg") or {}
                h = msg.get("header")
                if not h or h.get("action_code") not in KINDS:
                    continue
                kind, cols = KINDS[h["action_code"]]
                name = f"{kind}_{h.get('action_sym')}"
                if name not in files:
                    f = gzip.open(out_day / f"{name}.csv.gz", "wt", encoding="utf-8", newline="")
                    w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
                    w.writeheader()
                    files[name] = (f, w)
                files[name][1].writerow({"recv_ts": rec.get("recv_ts"), **(msg.get("body") or {})})
                counts[name] = counts.get(name, 0) + 1
    finally:
        for f, _ in files.values():
            f.close()
    return counts


def main(argv=None) -> int:
    days = (argv if argv is not None else sys.argv[1:]) or sorted(p.name for p in RAW_DIR.iterdir() if p.is_dir())
    for day in days:
        n = convert_day(RAW_DIR / day, CSV_DIR / day)
        print(day, dict(sorted(n.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
