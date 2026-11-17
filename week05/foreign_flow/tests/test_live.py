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




# ---------- 조합 규칙 (매일 갱신) ----------

def _panel_csv(dirp: Path, day: str, seed: int) -> None:
    idx = convert.minute_index(day)
    rng = np.random.default_rng(seed)
    fr, ind = rng.normal(0, 100, len(idx)), rng.normal(0, 50, len(idx))
    ret = rng.normal(0, 5, len(idx))
    ret[5:] += (fr[:-5] / 100 - ind[:-5] / 50)          # 외국인 사면 오르고, 개인 사면 내림
    p = pd.DataFrame({"011_frgn": np.cumsum(fr), "051_frgn": 0.0, "011_ind": np.cumsum(ind), "051_ind": 0.0,
                      "MINI_mid": 780 * np.exp(np.cumsum(ret) / 1e4), "MINI_spread": 0.04}, index=idx)
    p.index.name = "t"
    p.to_csv(dirp / f"{day}.csv")


@pytest.fixture
def combo_env(tmp_path, monkeypatch):
    monkeypatch.setattr(analyze, "PANEL_DIR", tmp_path)
    monkeypatch.setattr(analyze, "RESULTS", tmp_path)
    monkeypatch.setattr(analyze, "COMBO_DIR", tmp_path / "rules_combo")
    monkeypatch.setattr(analyze, "COMBO_CURRENT", tmp_path / "rules_combo.json")
    for i, d in enumerate(("20261117", "20261118", "20261119")):
        _panel_csv(tmp_path, d, i)
    return tmp_path


def test_add_signal_is_weighted_z_sum():
    f = pd.DataFrame({"frgn_fut_5": [10.0, -20.0], "ind_fut_5": [5.0, 5.0]})
    rule = {"signal": "combo_5", "parts": [{"signal": "frgn_fut_5", "w": 1.0, "sd": 10.0},
                                           {"signal": "ind_fut_5", "w": -1.0, "sd": 5.0}]}
    analyze.add_signal(f, rule)
    assert list(f["combo_5"]) == [0.0, -3.0]
    g = pd.DataFrame({"x": [1.0]})
    assert list(analyze.add_signal(g, {"signal": "x"}).columns) == ["x"]       # parts 없으면 그대로


def test_combo_train_writes_day_rule_once_and_only_from_past(combo_env):
    analyze.combo_train(["20261117"], "20261118", force=False)
    r = json.loads((combo_env / "rules_combo" / "for_20261118.json").read_text())
    assert r["train_days"] == ["20261117"] and r["for_day"] == "20261118" and r["train_trades"] >= analyze.MIN_TRADES
    assert json.loads((combo_env / "rules_combo.json").read_text()) == r       # 실행기가 읽는 현재 규칙
    assert [q["signal"] for q in r["parts"]] == ["frgn_fut_5", "ind_fut_5"] and r["h"] == 5 and r["sign"] == 1.0
    with pytest.raises(SystemExit):
        analyze.combo_train(["20261117"], "20261118", force=False)              # 그날 규칙은 한 번만
    with pytest.raises(SystemExit):
        analyze.combo_train(["20261117", "20261118"], "20261118", force=False)  # 매매일이 학습에 들어가면 안 됨


def test_combo_test_uses_each_days_own_rule(combo_env):
    analyze.combo_train(["20261117"], "20261118", force=False)
    analyze.combo_train(["20261117", "20261118"], "20261119", force=False)
    analyze.combo_test(["20261118", "20261119"])
    tr = pd.read_csv(combo_env / "test_trades_combo.csv")
    assert set(tr["day"].astype(str)) <= {"20261118", "20261119"} and len(tr)
    # 수요일 규칙 파일을 미래 날짜로 오염시키면 판정이 거부
    bad = json.loads((combo_env / "rules_combo" / "for_20261118.json").read_text())
    bad["train_days"] = ["20261118"]
    (combo_env / "rules_combo" / "for_20261118.json").write_text(json.dumps(bad))
    with pytest.raises(SystemExit):
        analyze.combo_test(["20261118"])


def test_live_loads_both_rules():
    order, shadow = live.load_rule(live.ORDER_RULE), live.load_rule(live.SHADOW_RULE)
    assert order["signal"] == "combo_5" and "parts" in order
    assert shadow["signal"] == "ind_fut_5"
