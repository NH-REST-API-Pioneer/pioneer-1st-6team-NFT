"""원문(data/jsonl/YYYYMMDD/*.jsonl.gz) → 일자·종목별 Parquet + 품질 로그.

  data/raw/YYYYMMDD_<종목>.parquet   틱 한 줄 = 메시지 하나 (호가 kind=quote, 체결 kind=trade)
  data/quality/YYYYMMDD.json        끊김·중복·역순·호가 공백·두 다리 수신 시각 차이

틱 열 (명세 1단계):
  recv_ts_local_ms  이 컴퓨터가 받은 시각 (epoch ms)
  exch_ts           거래소 시각 HHMMSS (초 단위). KRX = exch_tm (한국시간), MKU = ko_exec_tm (NH 가 붙인 한국시간)
  exch_tm_raw       원문 exch_tm (MKU 는 싱가포르 시간)
  symbol, kind, conn
  bid, ask          호가 메시지 = 1호가, 체결 메시지 = 그 순간 1호가 (bid_best_pric/ask_best_pric)
  bid_qty, ask_qty  1호가 잔량 (호가 메시지만. 체결 메시지에는 잔량이 없어 비움)
  last, last_qty    체결가·체결량 (체결 메시지만)
  bid_pric_1~5 / bid_qty_1~5 / ask_pric_1~5 / ask_qty_1~5   호가 5단계 (KRX 는 10단계 중 5단계만)
  tot_bid_qty, tot_ask_qty   총잔량 (KRX 는 10단계 합, MKU 는 5단계 합)

사용법 (week05/basis_obi/ 에서): .venv/bin/python -m collector.build [YYYYMMDD ...]   (없으면 기록된 날 전부)
"""
from __future__ import annotations

import gzip
import io
import json
import sys
import zlib
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
JSONL_DIR = ROOT / "data" / "jsonl"
RAW_DIR = ROOT / "data" / "raw"
QUALITY_DIR = ROOT / "data" / "quality"
KST = ZoneInfo("Asia/Seoul")

KRX_HOURS = ("08:45", "15:45")
GAP_S = 5.0
LEVELS = 5
KINDS = {"KA": "trade", "KB": "quote", "FA": "trade", "FB": "quote"}
PAYLOAD = (["exch_ts", "bid", "ask", "bid_qty", "ask_qty", "last", "last_qty"]
           + [f"{s}_{k}_{i}" for i in range(1, LEVELS + 1) for s in ("bid", "ask") for k in ("pric", "qty")]
           + ["tot_bid_qty", "tot_ask_qty", "cum_trd_qty"])
COLS = ["recv_ts_local_ms", "symbol", "kind", "conn", "exch_tm_raw"] + PAYLOAD


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return np.nan


def recv_ms(iso: str) -> int:
    return int(round(datetime.fromisoformat(iso).replace(tzinfo=KST).timestamp() * 1000))


def parse(rec: dict) -> dict | None:
    msg = rec.get("msg") or {}
    h = msg.get("header")
    if not h or h.get("action_code") not in KINDS:
        return None
    code, b = h["action_code"], msg.get("body") or {}
    kind = KINDS[code]
    domestic = code in ("KA", "KB")
    r = {c: np.nan for c in PAYLOAD}
    r.update(recv_ts_local_ms=recv_ms(rec["recv_ts"]), symbol=b.get("sym") or h.get("action_sym"), kind=kind,
             conn=rec.get("conn", ""), exch_tm_raw=b.get("exch_tm", ""),
             exch_ts=b.get("exch_tm", "") if domestic else b.get("ko_exec_tm", ""))
    if kind == "quote":
        r["bid"], r["ask"] = _f(b.get("bid_pric_1")), _f(b.get("ask_pric_1"))
        r["bid_qty"], r["ask_qty"] = _f(b.get("bid_qty_1")), _f(b.get("ask_qty_1"))
        for i in range(1, LEVELS + 1):
            for s in ("bid", "ask"):
                r[f"{s}_pric_{i}"], r[f"{s}_qty_{i}"] = _f(b.get(f"{s}_pric_{i}")), _f(b.get(f"{s}_qty_{i}"))
        r["tot_bid_qty"], r["tot_ask_qty"] = _f(b.get("bid_qty")), _f(b.get("ask_qty"))
    else:
        r["bid"], r["ask"] = _f(b.get("bid_best_pric")), _f(b.get("ask_best_pric"))
        r["last"], r["last_qty"] = _f(b.get("last_pric")), _f(b.get("exec_qty"))
        r["cum_trd_qty"] = _f(b.get("cum_trd_qty"))      # 같은 초·같은 값 체결 두 건을 중복으로 세지 않게
    return r


# ---------- 품질 ----------

def _hhmm_ms(day: str, hhmm: str) -> int:
    return recv_ms(f"{day[:4]}-{day[4:6]}-{day[6:]}T{hhmm}:00.000")


def _clock(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, KST).strftime("%H:%M:%S.%f")[:-3]


def _pct(a: np.ndarray) -> dict:
    if not len(a):
        return {"n": 0, "p50": None, "p95": None, "max": None}
    return {"n": int(len(a)), "p50": float(np.percentile(a, 50)), "p95": float(np.percentile(a, 95)),
            "max": float(a.max())}


def quality(df: pd.DataFrame, events: list[dict], day: str, gap_s: float = GAP_S,
            krx: str | None = None, mku: str | None = None, hours=KRX_HOURS) -> dict:
    t0, t1 = _hhmm_ms(day, hours[0]), _hhmm_ms(day, hours[1])
    out = {"day": day, "krx_hours": list(hours), "gap_s": gap_s, "symbols": {}}
    df = df.sort_values("recv_ts_local_ms", kind="stable") if len(df) else df
    for sym, g in (df.groupby("symbol") if len(df) else []):
        s = {}
        for kind, k in g.groupby("kind"):
            pay = k[PAYLOAD].astype(str)
            dup = (pay == pay.shift(1)).all(axis=1)
            ts = k["exch_ts"].astype(str)
            ooo = (ts < ts.shift(1)) & ts.shift(1).notna() & (ts != "")
            s[kind] = {"n": int(len(k)), "duplicates": int(dup.iloc[1:].sum()), "out_of_order": int(ooo.sum())}
        q = g[(g.kind == "quote") & (g.recv_ts_local_ms >= t0) & (g.recv_ts_local_ms <= t1)]["recv_ts_local_ms"].values
        d = np.diff(q) / 1000.0
        big = np.where(d >= gap_s)[0]
        s["quote_gaps"] = {"count": int(len(big)), "max_s": float(d.max()) if len(d) else 0.0,
                           "total_s": float(d[big].sum()),
                           "list": [{"start": _clock(int(q[i])), "secs": round(float(d[i]), 3)} for i in big[:50]]}
        out["symbols"][sym] = s
    syms = list(out["symbols"])
    krx = krx or next((x for x in syms if x.startswith("A")), None)
    mku = mku or next((x for x in syms if x.startswith("MKU")), None)
    lags = []
    if krx and mku:
        q = df[df.symbol.isin([krx, mku])]          # 호가·체결 모두 (체결에도 그 순간 1호가가 있음)
        last = {krx: None, mku: None}
        for ms, sym in zip(q.recv_ts_local_ms.values, q.symbol.values):
            other = mku if sym == krx else krx
            if last[other] is not None and t0 <= ms <= t1:
                lags.append((ms - last[other]) / 1000.0)
            last[sym] = ms
    out["leg_lag_s"] = {"krx": krx, "mku": mku, "what": "한 다리 메시지가 올 때 다른 다리 마지막 메시지의 나이 (장중, 호가·체결)",
                        **_pct(np.array(lags))}
    disc: dict = {}
    for i, e in enumerate(events):
        c = disc.setdefault(e["conn"], {"count": 0, "resub_data": 0, "list": []})
        if e["kind"] == "resub_data":
            c["resub_data"] += 1
        if e["kind"] != "disconnect":
            continue
        back = next((x for x in events[i + 1:] if x["conn"] == e["conn"] and x["kind"] == "connect"), None)
        down = ((datetime.fromisoformat(back["ts"]) - datetime.fromisoformat(e["ts"])).total_seconds()
                if back else None)
        c["count"] += 1
        c["list"].append({"at": e["ts"], "reconnected": back["ts"] if back else None, "down_s": down,
                          "why": e.get("note", "")})
    out["disconnects"] = disc
    return out


def summary(q: dict) -> str:
    """품질 로그를 사람이 읽을 몇 줄로."""
    L = [f"== {q['day']} 품질 (장중 {q['krx_hours'][0]}~{q['krx_hours'][1]}, 공백 기준 {q['gap_s']:.0f}초)"]
    for sym, s in q["symbols"].items():
        parts = [f"{k} {v['n']:,}건 (중복 {v['duplicates']}, 역순 {v['out_of_order']})"
                 for k, v in s.items() if k in ("quote", "trade")]
        g = s["quote_gaps"]
        gap = (f"호가 공백 {g['count']}번 (최장 {g['max_s']:.1f}초, 합 {g['total_s']:.0f}초)"
               if "quote" in s else "호가 없음 (확인 필요)")
        L.append(f"  {sym}: " + " · ".join(parts) + " · " + gap)
    lag = q["leg_lag_s"]
    if lag["n"]:
        L.append(f"  두 다리 수신 시각 차이: p50 {lag['p50']:.2f}초 · p95 {lag['p95']:.2f}초 · max {lag['max']:.1f}초"
                 f" (n={lag['n']:,})")
    else:
        L.append("  두 다리 수신 시각 차이: 계산할 표본 없음 (p95 -)")
    for conn, d in q["disconnects"].items():
        down = [x["down_s"] for x in d["list"] if x["down_s"] is not None]
        L.append(f"  {conn} 끊김 {d['count']}번" + (f" (가장 길게 {max(down):.0f}초)" if down else "")
                 + f" · 장중 시세 없어 재구독 {d['resub_data']}번")
    if not q["disconnects"]:
        L.append("  끊김 0번")
    return "\n".join(L)


# ---------- 하루 만들기 ----------

def _lines(p: Path):
    opener = gzip.open if p.suffix == ".gz" else open
    try:
        with opener(p, "rt", encoding="utf-8") as f:
            yield from f
    except (EOFError, zlib.error, gzip.BadGzipFile, io.UnsupportedOperation):
        return


def read_day(jsonl_root: Path, day: str) -> tuple[pd.DataFrame, list[dict]]:
    d = Path(jsonl_root) / day
    files = sorted(set(d.glob("*.jsonl.gz")) | set(d.glob("[0-9][0-9].jsonl")))
    rows = []
    for p in files:
        for line in _lines(p):
            try:
                r = parse(json.loads(line))
            except (ValueError, KeyError):
                continue
            if r:
                rows.append(r)
    df = pd.DataFrame(rows, columns=COLS)
    if len(df):
        df = df.sort_values("recv_ts_local_ms", kind="stable").reset_index(drop=True)
    ev_file = d / "events.jsonl"
    events = [json.loads(x) for x in ev_file.read_text(encoding="utf-8").splitlines() if x.strip()] \
        if ev_file.exists() else []
    return df, events


def build_day(jsonl_root: Path, day: str, raw_out: Path = RAW_DIR, quality_out: Path = QUALITY_DIR) -> dict:
    df, events = read_day(jsonl_root, day)
    raw_out, quality_out = Path(raw_out), Path(quality_out)
    raw_out.mkdir(parents=True, exist_ok=True)
    quality_out.mkdir(parents=True, exist_ok=True)
    for sym, g in df.groupby("symbol"):
        g.reset_index(drop=True).to_parquet(raw_out / f"{day}_{sym}.parquet", index=False)
    q = quality(df, events, day)
    (quality_out / f"{day}.json").write_text(json.dumps(q, ensure_ascii=False, indent=2))
    return {"rows": int(len(df)), "symbols": {s: int(n) for s, n in df.symbol.value_counts().items()}}


def main(argv=None) -> int:
    days = (argv if argv is not None else sys.argv[1:]) or sorted(
        p.name for p in JSONL_DIR.iterdir() if p.is_dir()) if JSONL_DIR.exists() else []
    for day in days:
        print(day, build_day(JSONL_DIR, day))
        print(summary(json.loads((QUALITY_DIR / f"{day}.json").read_text())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
