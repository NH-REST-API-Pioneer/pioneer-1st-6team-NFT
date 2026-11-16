"""수집 원본 → 1분 표 (data/panel/YYYYMMDD.csv). 분석·실행기가 같은 표를 씁니다.

  .venv/bin/python convert.py 20261117        # 하루
  .venv/bin/python convert.py                 # data/raw 의 모든 날

시각 규칙 (미래 정보가 섞이지 않게):
  - 1분 표의 행 시각 t = 그 분이 끝난 시각 (09:01 행 = 09:00:00~09:00:59 에 일어난 일).
  - 투자자 동향은 30초 지연 스냅샷이라 "받은 시각(recv_ts)"으로 줄을 세웁니다. t 행에는 t 이전에 받은 마지막 값.
  - 가격·호가는 거래소 시각(exch_tm) 기준. t 행에는 t 이전 마지막 체결가·호가.

열 (종목 접두어 K200_ / MINI_):
  last, mid, spread, imb1(최우선 잔량 불균형 b/(a+b)), depth_imb(총잔량 불균형), vol(그 분 체결량),
  aggr(그 분 매수주도−매도주도 체결수량 = Δ(누적매수체결 − 누적매도체결)), opint, basis(마켓베이시스), und(기초자산)
투자자 열 (품목 011 선물·051 미니·012 콜·013 풋, 수량 기준):
  {품목}_{투자자}  그날 누적 순매수 (as-of).  투자자 = frgn ind inst(금융투자+보험+투신+은행+기타금융+연기금+국가)
  {품목}_frgn_tm   그 값의 서버 시각 (지연 확인용)
"""
from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from record import CONTRACTS, HERE, RAW_DIR

PANEL_DIR = HERE / "data" / "panel"
INST = ("scrt", "ins", "invt", "bank", "mb", "penfnd", "cntry")
SYM2NAME = {v: k for k, v in CONTRACTS.items()}


def _f(x) -> float:
    try:
        return float(str(x).strip())
    except (TypeError, ValueError):
        return np.nan


def _net(b: dict, g: str) -> float:
    return _f(b.get("ins_buy_net" if g == "ins" else f"{g}_net_buy"))


def read_ws(day_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """KA(체결 tick_div=1)·KB(호가) 줄."""
    ka, kb = [], []
    for p in sorted(day_dir.glob("*.jsonl.gz")) + sorted(day_dir.glob("[0-2][0-9].jsonl")):
        opener = gzip.open if p.suffix == ".gz" else open
        with opener(p, "rt", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue                      # 강제 종료로 끝줄이 잘린 경우
                m = rec["msg"]
                h = m.get("header") if isinstance(m, dict) else None
                if not h:
                    continue
                b = m.get("body") or {}
                code, sym = h.get("action_code"), b.get("sym") or h.get("action_sym")
                if code == "KA" and str(b.get("tick_div")) == "1":
                    ka.append({"sym": sym, "exch_tm": b.get("exch_tm"), "last": _f(b.get("last_pric")),
                               "qty": _f(b.get("exec_qty")), "cbuy": _f(b.get("cum_buy_exec_qty")),
                               "csel": _f(b.get("cum_sel_exec_qty")), "opint": _f(b.get("opint")),
                               "basis": _f(b.get("mkt_bas")), "und": _f(b.get("und_last_pric"))})
                elif code == "KB":
                    kb.append({"sym": sym, "exch_tm": b.get("exch_tm"), "bid": _f(b.get("bid_pric_1")),
                               "ask": _f(b.get("ask_pric_1")), "bq": _f(b.get("bid_qty_1")),
                               "aq": _f(b.get("ask_qty_1")), "tbq": _f(b.get("bid_qty")),
                               "taq": _f(b.get("ask_qty"))})
    return pd.DataFrame(ka), pd.DataFrame(kb)


def read_rest(day_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    inv, px = [], []
    p = day_dir / "rest.jsonl"
    if not p.exists():
        return pd.DataFrame(), pd.DataFrame()
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        b = r.get("body") or {}
        if "_error" in b:
            continue
        if r["kind"] == "investor":
            prd, tp = r["key"].split(":")
            if tp != "1":
                continue                          # 표에는 수량 기준만 (금액은 원본에 남아 있음)
            row = {"recv": pd.Timestamp(r["recv_ts"]), "prd": prd, "tm": b.get("tm"),
                   "frgn": _net(b, "frgn"), "ind": _net(b, "ind"),
                   "inst": sum(np.nan_to_num(_net(b, g)) for g in INST)}
            inv.append(row)
        elif r["kind"] == "price":
            px.append({"recv": pd.Timestamp(r["recv_ts"]), "sym": r["key"], "rest_last": _f(b.get("last_pric"))})
    return pd.DataFrame(inv), pd.DataFrame(px)


def minute_index(day: str) -> pd.DatetimeIndex:
    d = pd.Timestamp(day)
    return pd.date_range(d + pd.Timedelta("09:01:00"), d + pd.Timedelta("15:45:00"), freq="1min")


def _exch_ts(day: str, tm: pd.Series) -> pd.Series:
    return pd.to_datetime(day + tm.astype(str).str.zfill(6), format="%Y%m%d%H%M%S")


def asof(idx: pd.DatetimeIndex, ts: pd.Series, values: pd.DataFrame) -> pd.DataFrame:
    """idx 각 시각 t 에 대해 ts < t 인 마지막 값."""
    df = values.copy()
    df["_ts"] = ts.values
    df = df.sort_values("_ts")
    left = pd.DataFrame({"_t": idx - pd.Timedelta("1ns")})
    out = pd.merge_asof(left, df, left_on="_t", right_on="_ts", direction="backward")
    return out.drop(columns=["_t", "_ts"]).set_index(idx)


def build_day(day: str) -> pd.DataFrame:
    dd = RAW_DIR / day
    idx = minute_index(day)
    ka, kb = read_ws(dd)
    inv, px = read_rest(dd)
    cols = {}
    for sym, name in SYM2NAME.items():
        a = ka[ka.sym == sym] if len(ka) else ka
        if len(a):
            ts = _exch_ts(day, a.exch_tm)
            last = asof(idx, ts, a[["last", "opint", "basis", "und"]])
            flow = (a.cbuy - a.csel).values
            g = pd.DataFrame({"ts": ts.values, "qty": a.qty.values, "flow": flow})
            g["m"] = g.ts.dt.floor("1min") + pd.Timedelta("1min")
            per = g.groupby("m").agg(vol=("qty", "sum"), flow_last=("flow", "last"))
            per = per.reindex(idx)
            cum = per.flow_last.ffill()
            aggr = cum.diff()
            # 누적 매수·매도 체결은 장 시작부터 센 값 → 첫 분은 0 기준. 단 장중에 켠 날(첫 체결이 09:01 뒤)은
            # 그 전 누적을 모르니 첫 값을 비움.
            k0 = cum.first_valid_index()
            if k0 is not None:
                aggr.loc[k0] = cum.loc[k0] if ts.min() < idx[0] else np.nan
            for c in last.columns:
                cols[f"{name}_{c}"] = last[c]
            cols[f"{name}_vol"] = per.vol.fillna(0.0)
            cols[f"{name}_aggr"] = aggr.fillna(0.0)
        q = kb[kb.sym == sym] if len(kb) else kb
        if len(q):
            ts = _exch_ts(day, q.exch_tm)
            b = asof(idx, ts, q[["bid", "ask", "bq", "aq", "tbq", "taq"]])
            cols[f"{name}_mid"] = (b.bid + b.ask) / 2
            cols[f"{name}_spread"] = b.ask - b.bid
            cols[f"{name}_imb1"] = b.bq / (b.bq + b.aq)
            cols[f"{name}_depth_imb"] = b.tbq / (b.tbq + b.taq)
        p = px[px.sym == sym] if len(px) else px
        if len(p):
            cols[f"{name}_rest_last"] = asof(idx, p.recv, p[["rest_last"]])["rest_last"]
    if len(inv):
        for prd, g in inv.groupby("prd"):
            g = g.drop_duplicates(["tm"], keep="first")
            v = asof(idx, g.recv, g[["frgn", "ind", "inst", "tm"]])
            for c in ("frgn", "ind", "inst"):
                cols[f"{prd}_{c}"] = v[c].astype(float)
            cols[f"{prd}_frgn_tm"] = v["tm"]
    out = pd.DataFrame(cols, index=idx)
    out.index.name = "t"
    return out


def main(days: list[str]) -> None:
    PANEL_DIR.mkdir(parents=True, exist_ok=True)
    days = days or sorted(p.name for p in RAW_DIR.iterdir() if p.is_dir())
    for day in days:
        df = build_day(day)
        df.to_csv(PANEL_DIR / f"{day}.csv")
        filled = df.notna().mean().round(2).to_dict()
        print(f"{day}: {len(df)}분, 채워진 비율 {filled}")


if __name__ == "__main__":
    main(sys.argv[1:])
