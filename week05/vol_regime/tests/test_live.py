import json

import numpy as np
import pandas as pd
import pytest

import config
import engine
import live
from helpers import raw_bars, walk

T = pd.Timestamp


class FakeClient:
    """저장된 원본 봉을 시각 now 까지만 돌려줌 (진행 중 봉 포함 — 실제 API 처럼)."""

    def __init__(self, raws: dict, clock):
        self.raws = {k: v.assign(_ts=pd.to_datetime(v["ref_dt"] + v["exch_tm"].str.zfill(6), format="%Y%m%d%H%M%S"))
                     for k, v in raws.items()}
        self.clock = clock
        self.lag = pd.Timedelta(0)

    def token(self):
        return "t"

    def chart_minute(self, sym, exch, *, start_dt, end_dt, dcnt=5, req_qty=300, nxt_key="99999999", cidx_yn="0", **kw):
        df = self.raws[sym]
        df = df[df["_ts"] <= self.clock() - self.lag]
        if req_qty <= 200:
            df = df.iloc[-req_qty:]
        else:
            df = df[df["ref_dt"] >= start_dt]
        rows = [{f"occurs1_{k}": v for k, v in r.items() if k != "_ts"} for r in df.to_dict("records")]
        return {"Occurs1": rows[::-1], "nxt_key": ""}


class FakeBroker:
    def __init__(self, script=None, positions=None, checks=None):
        self.script = list(script or [])
        self.checks = list(checks or [])
        self.calls = []
        self.pos = positions

    def check(self, sym, ord_no):
        return self.checks.pop(0) if self.checks else ("working", None)

    def order(self, sym, side, qty, ref_px, why):
        st = self.script.pop(0) if self.script else "filled"
        self.calls.append((sym, side, qty, why, st))
        return st, (ref_px if st == "filled" else None), ("1" if st != "unknown" else "")

    def positions(self):
        if isinstance(self.pos, Exception):
            raise self.pos
        return dict(self.pos)


class Clock:
    def __init__(self, t):
        self.t = T(t)

    def __call__(self):
        return self.t


def _runner(tmp_path, client, broker, clock, orders=False):
    return live.Runner(orders=orders, client=client, broker=broker, clock=clock, sleep=lambda s: None,
                       root=tmp_path, booster=live.load_booster())


def _raw_csv(sym):
    return pd.read_csv(config.BARS_DIR / f"{sym}.csv", dtype={"ref_dt": str, "exch_tm": str})


def test_phase():
    assert live.phase(T("2026-11-18 00:30")) == "sleep"
    assert live.phase(T("2026-11-18 06:50")) == "warmup"
    assert live.phase(T("2026-11-18 07:00")) == "trade"
    assert live.phase(T("2026-11-18 23:59")) == "trade"


def test_replay_equals_backtest(tmp_path):
    clock = Clock("2026-09-22 06:50")
    client = FakeClient({"ESZ26": _raw_csv("ES"), "GCZ26": _raw_csv("GC")}, clock)
    r = _runner(tmp_path, client, FakeBroker(), clock)
    r.feed.warmup(clock())
    for t in pd.date_range("2026-09-22 07:05:08", "2026-09-23 00:00:08", freq="5min"):
        clock.t = t
        r.on_bar(t)
    got = sorted((e["sym"], T(e["bar"]), int(e["side"]), e["strat"]) for e in r.rec.events
                 if e["kind"] == "봉" and e.get("act") == "enter")
    bt = pd.read_csv(config.RESULTS / "backtest" / "switch_lgbm_three_mock.csv", parse_dates=["entry_bar"])
    bt = bt[bt["sym"].isin(["ES", "GC"]) & (bt["entry_bar"].dt.strftime("%Y-%m-%d") == "2026-09-22")]
    want = sorted(zip(bt["sym"], bt["entry_bar"], bt["side"].astype(int), bt["strat"]))
    assert len(want) >= 5 and got == want


def _synthetic(n_days=3, drop_last=False):
    ts, c = walk(288 * n_days, start="2026-11-16 08:00", sd=3e-4, seed=4, p0=4000)
    if drop_last:
        c = c.copy()
        c[-1] *= 0.98
    raw = raw_bars(ts, c)
    raw["high_pric"], raw["low_pric"] = c * 1.0002, c * 0.9998
    return raw


def test_drop_in_progress_bar(tmp_path):
    raw = _synthetic()
    clock = Clock("2026-11-17 10:02")
    f = live.Feed(FakeClient({"ESZ26": raw, "GCZ26": raw}, clock))
    f.warmup(clock())
    b = f.bars(clock())
    assert b["ES"].index[-1] == T("2026-11-17 09:55")     # 10:00 봉은 아직 진행 중


def test_stale_bar_skipped(tmp_path):
    raw = _synthetic()
    clock = Clock("2026-11-17 10:05:08")
    client = FakeClient({"ESZ26": raw, "GCZ26": raw}, clock)
    r = _runner(tmp_path, client, FakeBroker(), clock)
    client.lag = pd.Timedelta("20min")     # 차트가 20분째 안 바뀜
    r.feed.warmup(clock())
    r.on_bar(clock())
    kinds = [e["kind"] for e in r.rec.events]
    assert "시세지연" in kinds and "봉" not in kinds


def _state(tmp_path, sym, entry_bar, side=1, strat="brk"):
    st = {"orders": False, "held": {"ES": 0, "GC": 0}, "engines": {"ES": None, "GC": None},
          "exit_pending": {}, "unknown": {}, "halt": None, "last_bar": {}, "open": {}}
    st["held"][sym] = side
    st["engines"][sym] = {"side": side, "strat": strat, "entry_bar": entry_bar, "regime": "high", "bars_held": 2}
    st["open"][sym] = {"entry_bar": entry_bar, "entry_px": 4000.0, "side": side, "strat": strat, "regime": "high"}
    (tmp_path / "state.json").write_text(json.dumps(st))
    return st


def test_restart_with_position(tmp_path):
    raw = _synthetic(drop_last=True)
    last = pd.to_datetime(raw["ref_dt"].iloc[-1] + raw["exch_tm"].iloc[-1], format="%Y%m%d%H%M%S")
    _state(tmp_path, "GC", str(last - pd.Timedelta("30min")))
    clock = Clock(last + pd.Timedelta("5min 8s"))
    br = FakeBroker()
    r = _runner(tmp_path, FakeClient({"ESZ26": raw, "GCZ26": raw}, clock), br, clock)
    assert r.held["GC"] == 1 and r.eng["GC"].pos.strat == "brk"
    r.feed.warmup(clock())
    r.on_bar(clock())
    assert ("GC", "sell", 1) in [c[:3] for c in br.calls]
    assert r.held["GC"] == 0 and r.eng["GC"].pos is None


def test_overnight_leftover(tmp_path):
    raw = _synthetic()
    _state(tmp_path, "ES", "2026-11-17 23:20", side=-1, strat="mr")
    clock = Clock("2026-11-18 08:05:08")
    br = FakeBroker()
    r = _runner(tmp_path, FakeClient({"ESZ26": raw, "GCZ26": raw}, clock), br, clock)
    r.feed.warmup(clock())
    r.on_bar(clock())
    assert br.calls[0][:3] == ("ES", "buy", 1) and "overnight" in br.calls[0][3]


def _enter_res(sym, side=1):
    return {"act": "enter", "side": side, "strat": "brk", "reason": "signal", "regime": "high", "bar": T("2026-11-17 10:00")}


def test_timeout_halts(tmp_path):
    clock = Clock("2026-11-17 10:05:08")
    br = FakeBroker(script=["timeout"], positions={"ES": 0, "GC": 0})
    r = _runner(tmp_path, FakeClient({"ESZ26": _synthetic(), "GCZ26": _synthetic()}, clock), br, clock, orders=True)
    r.eng["ES"].pos = engine.Position(1, "brk", T("2026-11-17 10:00"), "high")
    r.handle("ES", _enter_res("ES"), 4000.0)
    assert r.unknown and r.eng["ES"].pos is None and r.blocked()
    br.pos = {"ES": 1, "GC": 0}                     # 다음 봉 잔고에 체결돼 있음
    r.reconcile()
    assert not r.unknown and r.held["ES"] == 1 and r.eng["ES"].pos.side == 1 and not r.blocked()


def test_timeout_not_filled(tmp_path):
    clock = Clock("2026-11-17 10:05:08")
    br = FakeBroker(script=["timeout"], positions={"ES": 0, "GC": 0}, checks=[("rejected", None)])
    r = _runner(tmp_path, FakeClient({"ESZ26": _synthetic(), "GCZ26": _synthetic()}, clock), br, clock, orders=True)
    r.eng["ES"].pos = engine.Position(1, "brk", T("2026-11-17 10:00"), "high")
    r.handle("ES", _enter_res("ES"), 4000.0)
    r.reconcile()                                   # 잔고 그대로 → 체결 안 됨
    assert not r.unknown and r.held["ES"] == 0 and r.eng["ES"].pos is None and not r.blocked()


def test_mismatch_halts(tmp_path):
    clock = Clock("2026-11-17 10:05:08")
    br = FakeBroker(positions={"ES": 2, "GC": 0})
    r = _runner(tmp_path, FakeClient({"ESZ26": _synthetic(), "GCZ26": _synthetic()}, clock), br, clock, orders=True)
    r.reconcile()
    assert r.halt and any(e["kind"] == "잔고불일치" for e in r.rec.events)
    r.eng["GC"].pos = engine.Position(1, "brk", T("2026-11-17 10:00"), "high")
    r.handle("GC", _enter_res("GC"), 4000.0)
    assert br.calls == [] and r.eng["GC"].pos is None


def test_exit_reject_retries(tmp_path):
    clock = Clock("2026-11-17 10:05:08")
    br = FakeBroker(script=["rejected"], positions={"ES": 1, "GC": 0})
    r = _runner(tmp_path, FakeClient({"ESZ26": _synthetic(), "GCZ26": _synthetic()}, clock), br, clock, orders=True)
    r.held["ES"] = 1
    r.open["ES"] = {"entry_bar": "2026-11-17 09:00", "entry_px": 4000.0, "side": 1, "strat": "brk", "regime": "high"}
    r.handle("ES", {"act": "exit", "side": 1, "strat": "brk", "reason": "channel", "regime": "high",
                    "bar": T("2026-11-17 10:00")}, 4010.0)
    assert "ES" in r.exit_pending and r.blocked("ES")
    r.retry_exits({"ES": 4011.0})
    assert "ES" not in r.exit_pending and r.held["ES"] == 0


def test_tz_guard(monkeypatch):
    monkeypatch.setattr(live.time, "strftime", lambda f: "+0000")
    with pytest.raises(SystemExit):
        live.main([])


def test_single_instance_lock(tmp_path):
    lock = tmp_path / "live.lock"
    h = live.single_instance(lock)
    with pytest.raises(SystemExit):
        live.single_instance(lock)
    h.close()
    live.single_instance(lock).close()      # 앞 프로세스가 끝나면 다시 잡힘



# ---------------------------------------------------------------- 검토 뒤 추가 (중복 주문·시간·상태 파일)
from nh_client import NHError  # noqa: E402


def _r(tmp_path, br, t="2026-11-17 10:05:08", orders=True):
    clock = Clock(t)
    return _runner(tmp_path, FakeClient({"ESZ26": _synthetic(), "GCZ26": _synthetic()}, clock), br, clock,
                   orders=orders), clock


class _RaisingClient:
    def __init__(self, status):
        self.status = status

    def ovs_market_order(self, *a):
        raise NHError("/trade/v1/overseas/order", self.status, "timeout")


def test_post_timeout_is_unknown(tmp_path):
    rec = live.Recorder(tmp_path, lambda: T("2026-11-17 10:05"))
    assert live.Broker(_RaisingClient(0), rec, True).order("ES", "buy", 1, 1.0, "x")[0] == "unknown"
    assert live.Broker(_RaisingClient(503), rec, True).order("ES", "buy", 1, 1.0, "x")[0] == "unknown"
    assert live.Broker(_RaisingClient(400), rec, True).order("ES", "buy", 1, 1.0, "x")[0] == "error"


def test_unknown_entry_blocks(tmp_path):
    br = FakeBroker(script=["unknown"], positions={"ES": 0, "GC": 0})
    r, _ = _r(tmp_path, br)
    r.eng["ES"].pos = engine.Position(1, "brk", T("2026-11-17 10:00"), "high")
    r.handle("ES", _enter_res("ES"), 4000.0)
    assert "ES" in r.unknown and r.blocked() and r.eng["ES"].pos is None


def test_halt_sends_no_orders(tmp_path):
    br = FakeBroker(positions={"ES": 1, "GC": 0})
    r, _ = _r(tmp_path, br)
    r.held["ES"], r.halt = 1, "테스트"
    r.exit_pending["ES"] = {"reason": "channel"}
    r.retry_exits({"ES": 4000.0, "GC": 4000.0})
    r.handle("ES", {"act": "exit", "side": 1, "strat": "brk", "reason": "time", "regime": "high",
                    "bar": T("2026-11-17 10:00")}, 4000.0)
    assert br.calls == []


def test_exit_pending_resolved_by_zero_balance(tmp_path):
    br = FakeBroker(positions={"ES": 0, "GC": 0})      # 앞선 청산이 사실은 체결돼 있었음
    r, _ = _r(tmp_path, br)
    r.held["ES"] = 1
    r.exit_pending["ES"] = {"reason": "flat"}
    r.reconcile()
    assert r.held["ES"] == 0 and "ES" not in r.exit_pending and r.halt is None
    r.retry_exits({"ES": 4000.0, "GC": 4000.0})
    assert br.calls == []


def test_unknown_working_order_not_dropped(tmp_path):
    br = FakeBroker(script=["timeout"], positions={"ES": 0, "GC": 0}, checks=[("working", None)] * 10)
    r, _ = _r(tmp_path, br)
    r.eng["ES"].pos = engine.Position(1, "brk", T("2026-11-17 10:00"), "high")
    r.handle("ES", _enter_res("ES"), 4000.0)
    r.reconcile()
    assert "ES" in r.unknown and r.halt is None          # 아직 접수 중 → 「체결 안 됨」으로 단정하지 않음
    for _ in range(6):
        r.reconcile()
    assert r.halt


def test_unknown_resolved_by_order_history(tmp_path):
    br = FakeBroker(script=["timeout"], positions={"ES": 1, "GC": 0}, checks=[("filled", 4001.5)])
    r, _ = _r(tmp_path, br)
    r.eng["ES"].pos = engine.Position(1, "brk", T("2026-11-17 10:00"), "high")
    r.handle("ES", _enter_res("ES"), 4000.0)
    r.reconcile()
    assert not r.unknown and r.held["ES"] == 1 and r.open["ES"]["entry_px"] == 4001.5


def test_no_order_in_cme_break(tmp_path):
    _state(tmp_path, "ES", "2026-11-17 23:20", side=-1, strat="mr")
    raw = _synthetic()
    clock = Clock("2026-11-18 07:00:08")                 # 겨울 KST 07~08 은 CME 휴장
    br = FakeBroker()
    r = _runner(tmp_path, FakeClient({"ESZ26": raw, "GCZ26": raw}, clock), br, clock)
    r.feed.warmup(clock())
    r.on_bar(clock())
    assert br.calls == [] and "ES" in r.exit_pending
    clock.t = T("2026-11-18 08:05:08")
    r.on_bar(clock())
    assert br.calls and br.calls[0][:3] == ("ES", "buy", 1)


def test_stale_feed_still_flattens(tmp_path):
    raw = _synthetic()
    clock = Clock("2026-11-17 23:50:08")
    client = FakeClient({"ESZ26": raw, "GCZ26": raw}, clock)
    _state(tmp_path, "GC", "2026-11-17 22:00")
    br = FakeBroker()
    r = _runner(tmp_path, client, br, clock)
    client.lag = pd.Timedelta("20min")
    r.feed.warmup(clock())
    r.on_bar(clock())
    assert ("GC", "sell", 1) in [c[:3] for c in br.calls]
    assert any(e["kind"] == "시세지연" for e in r.rec.events)


def test_reconcile_failure_keeps_exits(tmp_path):
    raw = _synthetic(drop_last=True)
    last = pd.to_datetime(raw["ref_dt"].iloc[-1] + raw["exch_tm"].iloc[-1], format="%Y%m%d%H%M%S")
    st = _state(tmp_path, "GC", str(last - pd.Timedelta("30min")))
    st["orders"] = True
    (tmp_path / "state.json").write_text(json.dumps(st))
    clock = Clock(last + pd.Timedelta("5min 8s"))
    br = FakeBroker(positions=NHError("/x", 0, "down"))
    r = _runner(tmp_path, FakeClient({"ESZ26": raw, "GCZ26": raw}, clock), br, clock, orders=True)
    r.feed.warmup(clock())
    r.on_bar(clock())
    assert ("GC", "sell", 1) in [c[:3] for c in br.calls]
    assert any(e["kind"] == "잔고조회오류" for e in r.rec.events)


def test_corrupt_state_halts_not_crash(tmp_path):
    (tmp_path / "state.json").write_text('{"orders": false, "held": {"ES"')
    r, _ = _r(tmp_path, FakeBroker(), orders=False)
    assert r.halt and not (tmp_path / "state.json").exists()
    assert list(tmp_path.glob("state.json.bad-*"))


def test_state_write_atomic(tmp_path, monkeypatch):
    seen = []
    real = live.os.replace
    monkeypatch.setattr(live.os, "replace", lambda a, b: (seen.append((str(a), str(b))), real(a, b)))
    r, _ = _r(tmp_path, FakeBroker(), orders=False)
    r._save_state()
    assert seen and seen[-1][1].endswith("state.json")
    assert json.loads((tmp_path / "state.json").read_text())["orders"] is False


def test_mode_roots_separate():
    assert live.root_for(False) != live.root_for(True)
    assert live.root_for(True).parent == live.root_for(False).parent == live.LIVE_DIR


def test_corrupt_token_file(tmp_path, monkeypatch):
    import nh_client
    monkeypatch.setattr(nh_client, "HOME_DIR", tmp_path)
    monkeypatch.setenv("NH_APP_KEY", "k" * 8)
    monkeypatch.setenv("NH_APP_SECRET", "s")
    c = nh_client.NHClient()
    (tmp_path / "token_overseas_demo.json").write_text("{broken")

    class Res:
        status_code, text = 200, ""

        def json(self):
            return {"access_token": "new", "scopes": "S", "expires_in": 3600}

    monkeypatch.setattr(c.session, "post", lambda *a, **k: Res())
    assert c.token() == "new"
