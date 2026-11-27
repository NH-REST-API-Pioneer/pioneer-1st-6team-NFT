import asyncio
import gzip
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import analyze  # noqa: E402
import convert  # noqa: E402
import record  # noqa: E402

DAY = "20261117"
K200, MINI = record.CONTRACTS["K200"], record.CONTRACTS["MINI"]


# ---------- 장 시간 ----------

def test_window_weekday_only_and_bounds():
    assert record.in_window(datetime(2026, 11, 17, 8, 50))
    assert record.in_window(datetime(2026, 11, 17, 15, 49))
    assert not record.in_window(datetime(2026, 11, 17, 15, 50))
    assert not record.in_window(datetime(2026, 11, 17, 8, 49))
    assert not record.in_window(datetime(2026, 11, 21, 10, 0))      # 토요일


# ---------- REST 조회 ----------

def test_rest_jobs_cover_all_investor_products_and_prices():
    jobs = record.rest_jobs()
    keys = {(k, key) for k, key, _, _ in jobs}
    for prd in record.INVESTOR_PRDS:
        for tp in record.INVESTOR_TPS:
            assert ("investor", f"{prd}:{tp}") in keys
    assert ("price", K200) in keys and ("price", MINI) in keys
    body = [b for k, key, _, b in jobs if key == "011:1"][0]("20261117")
    assert body == {"opt_prd": "011", "ref_dt": "20261117", "inq_tp": "1"}


def test_rest_loop_writes_each_job_and_survives_errors(tmp_path):
    calls = []

    def post(path, body):
        calls.append(path)
        if body.get("opt_prd") == "012":
            raise RuntimeError("일시 오류")
        return {"tm": "090030", "frgn_net_buy": "10"}

    stop = asyncio.Event()
    w = record.RestWriter(tmp_path)
    counts = Counter()

    async def go():
        t = asyncio.ensure_future(record.rest_loop(post, w, stop, every=0.05, counts=counts,
                                                   now=lambda: datetime(2026, 11, 17, 9, 0, 30)))
        await asyncio.sleep(0.12)
        stop.set()
        await t

    asyncio.run(go())
    lines = [json.loads(x) for x in (tmp_path / DAY / "rest.jsonl").read_text().splitlines()]
    n_jobs = len(record.rest_jobs())
    assert len(lines) >= n_jobs and len(lines) % n_jobs == 0
    errs = [x for x in lines if "_error" in x["body"]]
    assert errs and all(x["key"].startswith("012") for x in errs)      # 실패도 기록하고 계속


# ---------- 변환 ----------

def _ka(sym, tm, last, qty, cbuy, csel, tick="1"):
    return {"header": {"action_code": "KA", "action_sym": sym},
            "body": {"sym": sym, "tick_div": tick, "exch_tm": tm, "last_pric": str(last), "exec_qty": str(qty),
                     "cum_buy_exec_qty": str(cbuy), "cum_sel_exec_qty": str(csel), "opint": "100",
                     "mkt_bas": "0.5", "und_last_pric": "789.0"}}


def _kb(sym, tm, bid, ask, bq, aq):
    return {"header": {"action_code": "KB", "action_sym": sym},
            "body": {"sym": sym, "exch_tm": tm, "bid_pric_1": str(bid), "ask_pric_1": str(ask), "bid_qty_1": str(bq),
                     "ask_qty_1": str(aq), "bid_qty": str(bq * 5), "ask_qty": str(aq * 5)}}


def _write_raw(root: Path, ws_msgs, rest_rows):
    d = root / DAY
    d.mkdir(parents=True)
    with gzip.open(d / "09.jsonl.gz", "wt", encoding="utf-8") as f:
        for ts, m in ws_msgs:
            f.write(json.dumps({"recv_ts": ts, "msg": m}) + "\n")
    with (d / "rest.jsonl").open("w", encoding="utf-8") as f:
        for r in rest_rows:
            f.write(json.dumps(r) + "\n")


def test_convert_uses_only_past_values(tmp_path, monkeypatch):
    monkeypatch.setattr(convert, "RAW_DIR", tmp_path)
    ws = [
        ("x", {"code": "pingpong", "msg": "pingpong"}),
        ("x", _ka(MINI, "090010", 789.00, 3, 3, 0)),
        ("x", _ka(MINI, "090050", 789.10, 2, 3, 2)),
        ("x", _ka(MINI, "090105", 789.20, 4, 7, 2)),
        ("x", _ka(MINI, "090106", 0, 0, 7, 2, tick="0")),            # tick_div 0 은 버림
        ("x", _kb(MINI, "090020", 789.00, 789.02, 30, 10)),
        ("x", _kb(MINI, "090110", 789.18, 789.22, 10, 30)),
    ]
    rest = [
        {"recv_ts": "2026-11-17T09:00:31.000", "kind": "investor", "key": "011:1",
         "body": {"tm": "090000", "frgn_net_buy": "100", "ind_net_buy": "-50", "scrt_net_buy": "-50"}},
        {"recv_ts": "2026-11-17T09:01:01.000", "kind": "investor", "key": "011:1",
         "body": {"tm": "090030", "frgn_net_buy": "160", "ind_net_buy": "-80", "scrt_net_buy": "-80"}},
        {"recv_ts": "2026-11-17T09:01:02.000", "kind": "investor", "key": "011:2", "body": {"tm": "090030"}},
        {"recv_ts": "2026-11-17T09:01:03.000", "kind": "investor", "key": "051:1", "body": {"_error": "x"}},
    ]
    _write_raw(tmp_path, ws, rest)
    p = convert.build_day(DAY)
    t1, t2 = pd.Timestamp("2026-11-17 09:01"), pd.Timestamp("2026-11-17 09:02")
    # 09:01 행 = 09:00:59 까지
    assert p.at[t1, "MINI_last"] == pytest.approx(789.10)
    assert p.at[t1, "MINI_mid"] == pytest.approx(789.01)
    assert p.at[t1, "MINI_imb1"] == pytest.approx(0.75)
    assert p.at[t1, "MINI_vol"] == 5
    assert p.at[t1, "MINI_aggr"] == pytest.approx(1)       # (3−2) − 0 : 첫 체결 전 누적 0 기준
    assert p.at[t1, "011_frgn"] == 100                     # 09:01:01 에 받은 값은 아직 아님
    assert p.at[t2, "011_frgn"] == 160
    assert p.at[t2, "011_inst"] == -80
    assert p.at[t2, "MINI_last"] == pytest.approx(789.20)
    assert p.at[t2, "MINI_aggr"] == pytest.approx(4)       # (7−2) − (3−2)


def test_minute_index_spans_session():
    idx = convert.minute_index(DAY)
    assert idx[0] == pd.Timestamp("2026-11-17 09:01") and idx[-1] == pd.Timestamp("2026-11-17 15:45")


# ---------- 분석 ----------

def _panel(seed=0, effect=0.0):
    idx = convert.minute_index(DAY)
    rng = np.random.default_rng(seed)
    flow = rng.normal(0, 100, len(idx))
    ret = rng.normal(0, 1, len(idx))
    ret[1:] += effect * flow[:-1] / 100          # 외국인 흐름이 다음 분 수익을 밀어 올림
    mid = 789 * np.exp(np.cumsum(ret) / 1e4)
    return pd.DataFrame({"011_frgn": np.cumsum(flow), "051_frgn": 0.0, "012_frgn": 0.0, "013_frgn": 0.0,
                         "011_ind": -np.cumsum(flow), "051_ind": 0.0, "K200_aggr": 0.0, "MINI_aggr": 0.0,
                         "MINI_mid": mid, "MINI_spread": 0.02}, index=idx)


def test_features_are_backward_looking():
    p = _panel()
    f = analyze.features(p)
    t = f.index[100]
    assert f.at[t, "frgn_fut_5"] == pytest.approx(p["011_frgn"].iloc[100] - p["011_frgn"].iloc[95])
    assert f.at[t, "r_10"] == pytest.approx(np.log(p["MINI_mid"].iloc[110] / p["MINI_mid"].iloc[100]) * 1e4)


def test_usable_excludes_open_and_close_auction():
    idx = convert.minute_index(DAY)
    m = analyze.usable(idx, 15)
    hm = idx[m].strftime("%H:%M")
    assert hm[0] == "09:05" and hm[-1] == "15:20"


def test_simulate_no_overlap_and_cost():
    f = analyze.features(_panel(effect=0.8))
    tr = analyze.simulate(f, "frgn_fut_1", 10, 1.0, 1.0, float(f["frgn_fut_1"].std()))
    ts = [x["t"] for x in tr]
    assert len(ts) > 1 and all(b - a >= pd.Timedelta(minutes=10) for a, b in zip(ts, ts[1:]))
    assert all(x["net_bp"] < x["gross_bp"] for x in tr)


def test_effect_is_detected_by_regression():
    F = {DAY: analyze.features(_panel(effect=4.0))}   # 10분 수익엔 1분 효과가 묽어져서 크게
    D = analyze.stack(F, "frgn_fut_1", 10)
    res = analyze.nw_ols(D["frgn_fut_1"] / D["frgn_fut_1"].std(), D["r_10"], 10)
    assert res["b"] > 0 and res["t"] > 2


def test_nw_ols_matches_plain_ols_when_lag_zero():
    rng = np.random.default_rng(1)
    x = pd.Series(rng.normal(size=500))
    y = pd.Series(2.0 * x + rng.normal(size=500))
    r = analyze.nw_ols(x, y, 0)
    X = np.column_stack([np.ones(500), x])
    b, res, *_ = np.linalg.lstsq(X, y, rcond=None)
    e = y - X @ b
    se_white = np.sqrt((np.linalg.inv(X.T @ X) @ (X.T * e.values ** 2) @ X @ np.linalg.inv(X.T @ X))[1, 1])
    assert r["b"] == pytest.approx(b[1])
    assert r["t"] == pytest.approx(b[1] / se_white)


def test_chi2_sf_matches_known_critical_values():
    for x, k in ((3.841, 1), (5.991, 2), (7.815, 3), (9.488, 4), (11.070, 5)):
        assert analyze.chi2_sf(x, k) == pytest.approx(0.05, abs=2e-4)


def test_cochran_q_zero_when_days_agree_and_large_when_not():
    se = np.array([1.0, 1.0, 1.0, 1.0])
    same = analyze.cochran_q(np.array([2.0, 2.0, 2.0, 2.0]), se)
    assert same["q"] == 0 and same["q_p"] == 1.0
    split = analyze.cochran_q(np.array([5.0, 5.0, -5.0, -5.0]), se)
    assert split["q"] == pytest.approx(100.0) and split["q_p"] < 1e-10 and split["b_fixed"] == 0
