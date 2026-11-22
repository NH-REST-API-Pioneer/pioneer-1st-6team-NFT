import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import analyze  # noqa: E402
import convert  # noqa: E402
import live  # noqa: E402

DAY = "20261117"
RULE = {"signal": "ind_fut_5", "h": 5, "theta": 1.5, "sign": -1.0, "sd": 10.0, "risk": {"stop_pt": 0.0, "daily_loss_krw": 0}}


def T(hm: str) -> pd.Timestamp:
    return pd.Timestamp(f"2026-11-17 {hm}")


class FakeBroker(live.PaperBroker):
    """호가 고정, 체결 기록만. fail_next 면 다음 주문 하나 실패."""

    def __init__(self, journal):
        super().__init__(None, journal)
        self.orders, self.fail_next, self.real_pos = [], False, None

    def quote(self):
        return {"bid": 100.0, "ask": 100.02, "last": 100.0}

    def fill(self, side, why, q):
        if self.fail_next:
            self.fail_next = False
            return None
        self.orders.append(side)
        return super().fill(side, why, q)

    def position(self):
        return self.pos if self.real_pos is None else self.real_pos


@pytest.fixture
def trader(tmp_path):
    j = live.Journal(tmp_path / "j.jsonl")
    b = FakeBroker(j)
    return live.Trader(b, RULE, j), b


def test_decide_respects_theta_stale_and_sign():
    assert live.decide(2.0, False, RULE) == -1          # 개인 순매수 많음 → 매도
    assert live.decide(-2.0, False, RULE) == 1
    assert live.decide(1.5, False, RULE) == 0           # 문턱은 초과여야 함
    assert live.decide(5.0, True, RULE) == 0            # 늦은 스냅샷이면 안 들어감
    assert live.decide(np.nan, False, RULE) == 0


def test_hold_h_minutes_then_exit_and_reenter_same_minute(trader):
    tr, b = trader
    tr.step(T("10:00"), 2.0, False)
    assert b.orders == [-1] and tr.exit_at == T("10:05")
    for m in ("10:01", "10:02", "10:03", "10:04"):
        tr.step(T(m), -3.0, False)                      # 보유 중엔 반대 신호여도 새 진입 없음
    assert b.orders == [-1]
    tr.step(T("10:05"), -3.0, False)                    # 청산 → 같은 분에 새 진입 (simulate 와 같음)
    assert b.orders == [-1, 1, 1] and len(tr.trades) == 1


def test_no_entry_outside_window(trader):
    tr, b = trader
    tr.step(T("09:04"), 3.0, False)
    tr.step(T("15:31"), 3.0, False)                     # h=5 → 마지막 진입 15:30
    assert b.orders == []
    tr.step(T("15:30"), 3.0, False)
    assert b.orders == [-1]


def test_failed_exit_retries_next_minute(trader):
    tr, b = trader
    tr.step(T("10:00"), 2.0, False)
    b.fail_next = True
    tr.step(T("10:05"), 0.0, False)
    assert tr.side == -1                                # 아직 들고 있음
    tr.step(T("10:06"), 0.0, False)
    assert tr.side == 0 and b.orders == [-1, 1]


def test_position_mismatch_halts_new_entries(trader):
    tr, b = trader
    b.real_pos = 2                                      # 잔고가 실행기 생각과 다름
    tr.step(T("10:00"), 2.0, False)
    assert tr.halted
    tr.side, b.real_pos = 0, 0
    tr.step(T("10:10"), 2.0, False)
    assert b.orders == [-1]                             # 멈춘 뒤엔 새로 안 들어감


def test_trade_pnl_uses_fill_prices_and_fees(trader):
    tr, b = trader
    tr.step(T("10:00"), -2.0, False)                    # 매수 100.02
    tr.step(T("10:05"), 0.0, False)                     # 매도 100.00
    x = tr.trades[0]
    assert x["pnl_pt"] == pytest.approx(-0.02)
    assert x["pnl_krw"] == round(-0.02 * analyze.PT_KRW - round(analyze.fee_krw(100.02), 2) - round(analyze.fee_krw(100.0), 2))   # 종이 체결은 수수료율


def _rest_line(recv, prd, tm, ind, frgn=0):
    return {"recv_ts": recv, "kind": "investor", "key": f"{prd}:1",
            "body": {"tm": tm, "frgn_net_buy": str(frgn), "ind_net_buy": str(ind)}}


def test_live_signal_matches_panel_signal(tmp_path, monkeypatch):
    """실행기가 rest.jsonl 에서 바로 계산한 신호 = convert → analyze 로 만든 신호."""
    monkeypatch.setattr(convert, "RAW_DIR", tmp_path)
    d = tmp_path / DAY
    d.mkdir()
    rng = np.random.default_rng(0)
    lines, ind = [], 0
    for i, t in enumerate(pd.date_range(T("09:00:00"), T("10:00:00"), freq="30s")):
        ind += int(rng.integers(-50, 50))
        tm = (t - pd.Timedelta("30s")).strftime("%H%M%S")
        recv = (t + pd.Timedelta("0.4s")).isoformat(timespec="milliseconds")
        lines += [_rest_line(recv, "011", tm, ind), _rest_line(recv, "051", tm, ind * 3)]
    (d / "rest.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    inv, _ = convert.read_rest(d)
    t = T("09:40")
    f_live = live.market_features(inv, pd.DataFrame(), DAY, t)
    idx = convert.minute_index(DAY)
    p = pd.DataFrame(convert.investor_cols(inv, idx), index=idx)
    p["MINI_mid"], p["MINI_spread"] = 100.0, 0.02
    f_panel = analyze.features(p)
    assert f_live.at[t, "ind_fut_5"] == pytest.approx(f_panel.at[t, "ind_fut_5"])
    z, stale = live.signal_at(f_live, t, RULE)
    assert not stale and z == pytest.approx(f_panel.at[t, "ind_fut_5"] / RULE["sd"])


def test_stale_snapshot_blocks_signal(tmp_path, monkeypatch):
    monkeypatch.setattr(convert, "RAW_DIR", tmp_path)
    d = tmp_path / DAY
    d.mkdir()
    lines = []
    for t in pd.date_range(T("09:00:00"), T("09:30:00"), freq="30s"):
        recv = (t + pd.Timedelta("0.4s")).isoformat(timespec="milliseconds")
        lines += [_rest_line(recv, p, "090000", 5) for p in ("011", "051")]   # 서버 시각이 09:00 에 멈춤
    (d / "rest.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    inv, _ = convert.read_rest(d)
    f = live.market_features(inv, pd.DataFrame(), DAY, T("09:20"))
    z, stale = live.signal_at(f, T("09:20"), RULE)
    assert stale and live.decide(99.0, stale, RULE) == 0




class MovingBroker(FakeBroker):
    """호가를 테스트에서 바꿀 수 있는 가짜."""

    def __init__(self, journal):
        super().__init__(journal)
        self.m = 100.0

    def quote(self):
        return {"bid": self.m - 0.01, "ask": self.m + 0.01, "last": self.m}


def test_stop_loss_exits_early_and_can_reenter(tmp_path):
    j = live.Journal(tmp_path / "j.jsonl")
    b = MovingBroker(j)
    tr = live.Trader(b, RULE, j, stop_pt=1.0, daily_loss_krw=0)
    tr.step(T("10:00"), -2.0, False)                 # 개인 매도 많음 → 매수
    assert tr.side == 1
    b.m = 99.5
    tr.step(T("10:01"), np.nan, False)               # −0.5pt: 그대로
    assert tr.side == 1
    b.m = 98.99
    tr.step(T("10:02"), -2.0, False)                 # −1.01pt: 손절, 같은 분에 다시 진입
    assert tr.trades[-1]["why"].startswith("손절") and tr.side == 1


def test_daily_loss_limit_blocks_new_entries(tmp_path):
    j = live.Journal(tmp_path / "j.jsonl")
    b = MovingBroker(j)
    tr = live.Trader(b, RULE, j, stop_pt=0, daily_loss_krw=100_000)
    tr.step(T("10:00"), -2.0, False)
    b.m = 97.0                                       # −3pt = −15만 원
    tr.step(T("10:05"), -2.0, False)                 # 시간 청산 → 한도 → 진입 안 함
    assert tr.loss_halted and tr.side == 0 and len(tr.trades) == 1
    tr.step(T("10:06"), -2.0, False)
    assert tr.side == 0


def test_risk_comes_from_rule_file():
    j = live.Journal(Path("/tmp") / "ff_risk_test.jsonl")
    tr = live.Trader(FakeBroker(j), dict(RULE, risk={"stop_pt": 2.5, "daily_loss_krw": 300_000}), j)
    assert tr.stop_pt == 2.5 and tr.daily_loss_krw == 300_000
    tr0 = live.Trader(FakeBroker(j), {k: v for k, v in RULE.items() if k != "risk"}, j)
    assert tr0.stop_pt == 0 and tr0.daily_loss_krw == 0          # risk 가 없으면 끔


def test_live_loads_all_rule_files():
    order = live.load_rule(live.ORDER_RULE)
    papers = [live.load_rule(p) for p in live.PAPER_RULES]
    assert order["risk"]["daily_loss_krw"] > 0
    assert papers[0]["signal"] == order["signal"] and papers[0]["risk"]["daily_loss_krw"] == 0   # 같은 규칙, 위험관리 끔
    assert all(r["valid_from"] == "20261123" and max(r["train_days"]) < r["valid_from"] for r in [order, *papers])


def test_replay_matches_simulate_rule():
    for day in ("20261117", "20261118", "20261120"):
        for path in (live.ORDER_RULE, *live.PAPER_RULES):
            r = live.load_rule(path)
            out = Path("/tmp") / f"ff_replay_test_{day}.jsonl"
            tr = live.replay(day, r, out)
            out.unlink(missing_ok=True)
            sim = live.simulate_rule(day, r)
            assert [(x["entry_t"], x["exit_t"]) for x in tr] == [(x["t"], x["exit_t"]) for x in sim]
            assert sum(x["pnl_krw"] for x in tr) == pytest.approx(sum(x["krw"] for x in sim), abs=len(sim) * 2)


# ---------- 웹소켓 체결 읽기 ----------

def _ka(tm, cbuy, csel, sym="A056C000", recv="2026-11-17T10:00:00.000"):
    return json.dumps({"recv_ts": recv, "msg": {"header": {"action_code": "KA", "action_sym": sym},
                       "body": {"sym": sym, "tick_div": "1", "exch_tm": tm, "last_pric": "780.00", "exec_qty": "1",
                                "cum_buy_exec_qty": str(cbuy), "cum_sel_exec_qty": str(csel)}}}) + "\n"


def test_ws_tail_reads_new_lines_partial_line_and_hour_rollover(tmp_path):
    d = tmp_path / DAY
    d.mkdir()
    plain = d / "10.jsonl"
    plain.write_text(_ka("100001", 10, 5) + _ka("100010", 12, 5) + _ka("100020", 13, 5)[:30])   # 끝줄은 쓰는 중
    ws = live.WsTail(d)
    assert len(ws.poll()) == 2
    with plain.open("a") as f:
        f.write(_ka("100020", 13, 5)[30:] + _ka("100030", 13, 9))
    assert len(ws.poll()) == 4
    with plain.open("a") as f:
        f.write(_ka("105959", 20, 9))                          # 읽기 전에 시간이 바뀜 → gz 로 넘어감
    import record
    record._gzip_into(plain)
    (d / "11.jsonl").write_text(_ka("110001", 21, 9))
    ka = ws.poll()
    assert len(ka) == 6 and list(ka.exch_tm) == ["100001", "100010", "100020", "100030", "105959", "110001"]
    assert len(ws.poll()) == 6                                 # 다시 읽어도 중복 없음


def test_ws_tail_restart_reads_gz_then_plain(tmp_path):
    d = tmp_path / DAY
    d.mkdir()
    import gzip
    with gzip.open(d / "10.jsonl.gz", "wt") as f:              # 같은 시간에 수집기를 다시 켠 경우: 앞부분은 gz, 뒤는 평문
        f.write(_ka("100001", 10, 5))
    (d / "10.jsonl").write_text(_ka("100500", 11, 5))
    ka = live.WsTail(d).poll()
    assert list(ka.exch_tm) == ["100001", "100500"]


def test_live_aggr_matches_panel_aggr(tmp_path, monkeypatch):
    """실행기가 웹소켓 파일에서 바로 계산한 aggr = convert.build_day 1분 표의 aggr."""
    monkeypatch.setattr(convert, "RAW_DIR", tmp_path)
    d = tmp_path / DAY
    d.mkdir()
    rng = np.random.default_rng(3)
    lines, cb, cs = [], 0, 0
    for s in range(8 * 60, 75 * 60, 7):                         # 08:58 ~ 10:15, 7초마다 체결
        cb += int(rng.integers(0, 5)); cs += int(rng.integers(0, 5))
        tm = (pd.Timestamp("2026-11-17 09:00") + pd.Timedelta(seconds=s - 120)).strftime("%H%M%S")
        for sym in ("A016C000", "A056C000"):
            lines.append(_ka(tm, cb * (5 if sym == "A056C000" else 1), cs))
    (d / "09.jsonl").write_text("".join(lines))
    panel = analyze.features(convert.build_day(DAY).assign(MINI_mid=np.nan, MINI_spread=np.nan))
    ka = live.WsTail(d).poll()
    for hm in ("09:06", "09:30", "10:10"):
        t = T(hm)
        f = live.market_features(pd.DataFrame(), ka, DAY, t)
        assert f.at[t, "aggr_5"] == pytest.approx(panel.at[t, "aggr_5"])
    assert live.ws_age_s(ka, DAY, T("10:10")) < 10
    assert live.ws_age_s(ka, DAY, T("10:30")) > live.WS_STALE_S


def test_ws_stale_blocks_only_trade_signal_rules():
    f = pd.DataFrame({"aggr_5": [30.0], "ind_fut_5": [30.0], "stale": [False]}, index=[T("10:00")])
    ra = {"signal": "aggr_5", "sd": 10.0}
    ri = {"signal": "ind_fut_5", "sd": 10.0}
    assert live.signal_at(f, T("10:00"), ra, ws_stale=True)[1] is True
    assert live.signal_at(f, T("10:00"), ri, ws_stale=True)[1] is False
    assert live.signal_at(f, T("10:00"), ra, ws_stale=False) == (3.0, False)
