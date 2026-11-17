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
RULE = {"signal": "ind_fut_5", "h": 5, "theta": 1.5, "sign": -1.0, "sd": 10.0}


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
    assert x["pnl_krw"] == round(-0.02 * analyze.PT_KRW - 2 * analyze.FEE_KRW)


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
    f_live = live.investor_features(inv, DAY, t)
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
    f = live.investor_features(inv, DAY, T("09:20"))
    z, stale = live.signal_at(f, T("09:20"), RULE)
    assert stale and live.decide(99.0, stale, RULE) == 0


def test_rule_file_is_investor_signal():
    r = live.load_rule()
    assert r["signal"].startswith(live.INVESTOR_SIGS)
