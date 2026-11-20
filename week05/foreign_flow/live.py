"""미니코스피200 모의매매 실행기 (수~금).

  주문 규칙 = results/rules_regime.json (11/20~: 조합 신호 + 외국인 흐름 지속성 국면 필터, 11/19 밤 고정)
             (11/18~19 는 results/rules_combo.json — 조합 규칙은 지금도 밤마다 갱신하고 판정은 analyze.py 로 데이터 위에서)
  기록만    = results/rules.json       (화요일 고정 개인 반대 규칙, 같은 시각에 호가로 종이 체결만 기록)

  .venv/bin/python live.py                     # 모의계좌 주문 (기본)
  .venv/bin/python live.py --paper             # 주문 없이 호가로 종이 체결
  .venv/bin/python live.py --replay 20261117   # 저장된 1분 표로 판단만 다시 돌려 analyze.simulate 와 같은지 확인
  ./run_live.sh                                # cron 용 (이미 돌고 있으면 바로 끝남)

어떻게 도나:
  - 투자자 동향은 직접 조회하지 않고, 같은 서버에서 도는 수집기(record.py)가 쓰는 data/raw/오늘/rest.jsonl 을 읽음.
    → API 호출·웹소켓 접속이 하나도 늘지 않음. 수집기가 죽으면 값이 늦어져서 진입이 저절로 멈춤.
  - 매 분 t+3초에 rest.jsonl 을 읽어 convert.investor_cols → analyze.features 로 t 행 신호를 계산 (분석과 같은 코드).
  - 서버 시각이 120초 넘게 늦은 스냅샷(analyze.STALE_S)이면 진입 안 함.
  - |z| > θ 이고 포지션이 없으면 sign(z)·rule.sign 방향 시장가 1계약, h 분 뒤 분 경계에서 시장가 청산.
  - 08:55 전·15:35 뒤·주말엔 바로 끝남. 진입은 09:05~(15:35 − h), 15:35 에는 무조건 청산. 끝나거나 SIGTERM 이면 청산하고 끝냄.
  - 시작할 때 미니 잔고가 남아 있으면(전에 죽은 경우) 먼저 청산.
  - 체결가는 주문내역, 수수료는 예수금 fee 차이 (2주차 runner.py 의 MockAccountBroker 와 같은 방식).
기록: data/live/YYYYMMDD.jsonl·_trades.csv (주문 규칙), YYYYMMDD_shadow.jsonl·_shadow_trades.csv (기록만 하는 규칙).
계좌번호 등은 지움.
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

import analyze
import convert
from record import CONTRACTS, HERE, RAW_DIR, now_kst, single_instance

SYM = CONTRACTS["MINI"]
LIVE_DIR = HERE / "data" / "live"     # 서버에서 쓰는 곳 (git 밖). 밤에 맥으로 복사해서 results/live/ 에 올림
ORDER_RULE = analyze.REGIME_FILE
SHADOW_RULE = analyze.RESULTS / "rules.json"
LOCK = HERE / "data" / "live.lock"
LAG_S = 3                     # 분 경계 뒤 이만큼 기다렸다 판단 (수집기의 :00 조회가 파일에 들어올 시간)
PRIVATE_KEYS = {"ac_no", "ac_nm", "user_ip_addr", "ord_id"}
INVESTOR_SIGS = ("frgn_fut_", "frgn_opt_", "ind_fut_")


def num(v) -> float | None:
    try:
        s = str(v).strip()
        return float(s) if s else None
    except (TypeError, ValueError):
        return None


def scrub(obj):
    if isinstance(obj, dict):
        return {k: scrub(v) for k, v in obj.items() if k not in PRIVATE_KEYS}
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    return obj


class Journal:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.f = path.open("a", encoding="utf-8")

    def write(self, kind: str, **data) -> None:
        rec = {"kind": kind, "at": now_kst().isoformat(timespec="seconds"), **scrub(data)}
        self.f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        self.f.flush()
        if kind != "minute":
            print(f"[{rec['at'][11:19]}] {kind:6s} {data.get('text', '')} "
                  f"{ {k: v for k, v in data.items() if k not in ('text', 'raw')} }", flush=True)


# ---------------------------------------------------------------- 신호
def load_rule(path: Path = ORDER_RULE) -> dict:
    rule = json.loads(path.read_text())
    sigs = [q["signal"] for q in rule["parts"]] if "parts" in rule else [rule["signal"]]
    if not all(x.startswith(INVESTOR_SIGS) for x in sigs):
        raise SystemExit(f"실행기는 투자자 동향 신호만 계산합니다: {sigs}")
    return rule


def investor_features(inv: pd.DataFrame, day: str, upto: pd.Timestamp) -> pd.DataFrame:
    """rest.jsonl 투자자 줄 → upto 까지 1분 신호 (analyze.features 그대로). 가격 열은 비워 둠."""
    idx = convert.minute_index(day)
    idx = idx[idx <= upto]
    p = pd.DataFrame(convert.investor_cols(inv, idx), index=idx)
    p["MINI_mid"], p["MINI_spread"] = np.nan, np.nan
    return analyze.features(p)


def signal_at(f: pd.DataFrame, t: pd.Timestamp, rule: dict) -> tuple[float, bool]:
    """(z, 늦은 스냅샷 여부). 값이 없으면 z = nan."""
    if t not in f.index:
        return np.nan, True
    z = f.at[t, rule["signal"]] / rule["sd"]
    return float(z) if np.isfinite(z) else np.nan, bool(f.at[t, "stale"])


def entry_window(t: pd.Timestamp, h: int) -> bool:
    return bool(analyze.usable(pd.DatetimeIndex([t]), h)[0])


def decide(z: float, stale: bool, rule: dict) -> int:
    """들어갈 방향 (+1 매수, −1 매도, 0 없음)."""
    if stale or not np.isfinite(z) or abs(z) <= rule["theta"]:
        return 0
    return int(np.sign(z) * rule["sign"])


# ---------------------------------------------------------------- 체결 담당
class PaperBroker:
    mode = "paper"

    def __init__(self, client, journal: Journal):
        self.c, self.j = client, journal
        self.pos = 0
        self.last_fill_fee: float | None = None   # 종이 체결 수수료는 체결가 × 0.00145% (analyze.fee_krw)

    def quote(self) -> dict:
        try:
            b = self.c.price(SYM)
            return {"bid": num(b.get("bid_pric")), "ask": num(b.get("ask_pric")), "last": num(b.get("last_pric"))}
        except Exception as e:  # noqa: BLE001 — 기록용
            return {"error": str(e)[:200]}

    def fill(self, side: int, why: str, q: dict) -> float | None:
        px = q.get("ask") if side > 0 else q.get("bid")
        if not px:
            return None
        self.pos += side
        self.last_fill_fee = round(analyze.fee_krw(px), 2)
        self.j.write("fill", side=side, price=px, source="paper", fee=self.last_fill_fee, text=f"종이 체결 · {why}")
        return px

    def position(self) -> int | None:
        return self.pos


class MockAccountBroker(PaperBroker):
    """모의계좌 시장가 주문 → 주문내역으로 체결가 → 예수금 fee 차이로 수수료."""
    mode = "mock-account"

    def __init__(self, client, journal: Journal):
        super().__init__(client, journal)
        self.today = now_kst().strftime("%Y%m%d")
        self.last_fee: float | None = None
        self.last_fill_fee = None
        self.account("시작")

    def account(self, why: str = "") -> dict | None:
        try:
            dep = self.c.deposit(self.today)
            snap = {k: num(dep.get(k)) for k in ("fee", "fut_cls_pl_amt", "pl_tot_pric", "ord_psb_tot_amt")}
        except Exception as e:  # noqa: BLE001 — 기록용 조회라 매매를 멈추지 않음
            self.j.write("event", text=f"예수금 조회 실패 ({why})", error=str(e)[:200])
            return None
        if why:
            self.j.write("account", text=f"계좌 · {why}", **snap)
        if self.last_fee is None:
            self.last_fee = snap["fee"]
        return snap

    def _fee_after_fill(self) -> float | None:
        before = self.last_fee
        snap = self.account()
        if snap and before is not None and snap["fee"] == before:
            time.sleep(1.0)
            snap = self.account() or snap
        if not snap or snap["fee"] is None:
            return None
        self.last_fee = snap["fee"]
        return None if before is None else round(snap["fee"] - before, 2)

    def fill(self, side: int, why: str, q: dict) -> float | None:
        from nh_client import NHError, rows
        self.last_fill_fee = None
        s = "buy" if side > 0 else "sell"
        try:
            res = self.c.market_order(SYM, s, 1)
        except NHError as e:
            self.j.write("order", side=side, ok=False, text=f"주문 실패 · {why}", error=str(e)[:300])
            return None
        ord_no = str(res.get("ord_no") or "").strip()
        self.j.write("order", side=side, ord_no=ord_no, msg_cd=res.get("msg_cd"), msg_cntn=res.get("msg_cntn"),
                     ok=bool(ord_no), text=f"시장가 1계약 · {why}")
        if not ord_no:
            return None
        last = None
        for _ in range(30):                       # 최대 15초 전체체결(6) 기다림
            time.sleep(0.5)
            try:
                hist = rows(self.c.order_history())
            except NHError as e:
                self.j.write("event", text="주문내역 조회 실패", error=str(e)[:200])
                continue
            mine = [r for r in hist if str(r.get("ord_no", "")).strip().lstrip("0") == ord_no.lstrip("0")]
            if not mine:
                continue
            last = mine[0]
            sts = str(last.get("ord_sts", "")).strip()
            if sts == "6":
                px = (num(last.get("avg_exec_pric")) or num(last.get("exec_pric"))) or None
                self.last_fill_fee = self._fee_after_fill()
                self.j.write("fill", side=side, price=px, ord_no=ord_no, source="order-history",
                             exec_tm=last.get("exch_exec_tm"), fee=self.last_fill_fee, fee_total=self.last_fee,
                             text="모의계좌 체결")
                return px
            if sts in ("8", "9"):
                self.j.write("event", ord_no=ord_no, text=f"주문 {last.get('ord_sts_nm')}",
                             reject=last.get("ord_rejt_rsn"), raw=last)
                return None
        self.j.write("event", ord_no=ord_no, text="15초 안에 전체체결 확인 안 됨 — 잔고로 확인", raw=last)
        return None

    def position(self) -> int | None:
        from nh_client import NHError, rows
        try:
            oi = rows(self.c.open_interest())
        except NHError as e:
            self.j.write("event", text="잔고 조회 실패", error=str(e)[:200])
            return None
        qty = 0
        for r in oi:
            if str(r.get("sym", "")).strip() == SYM:
                n = int(num(r.get("opint_qty")) or 0)
                qty += n if str(r.get("trd_div")).strip() == "1" else -n
        return qty


# ---------------------------------------------------------------- 실행기
class Trader:
    def __init__(self, broker, rule: dict, journal: Journal, trades_csv: Path | None = None,
                 stop_pt: float = analyze.STOP_PT, daily_loss_krw: float = analyze.DAILY_LOSS_KRW):
        self.b, self.rule, self.j, self.trades_csv = broker, rule, journal, trades_csv
        self.h = int(rule["h"])
        self.stop_pt, self.daily_loss_krw = stop_pt, daily_loss_krw
        self.loss_halted = False          # 하루 손실 한도에 걸림 (그날은 새 진입 없음)
        self.side = 0                     # 지금 들고 있는 방향 (실행기 기준)
        self.exit_at: pd.Timestamp | None = None
        self.entry: dict | None = None
        self.trades: list[dict] = []
        self.halted = False

    def _check_pos(self, why: str) -> None:
        pos = self.b.position()
        if pos is not None and pos != self.side:
            self.j.write("event", text=f"잔고 불일치 ({why}) — 실행기 {self.side}, 잔고 {pos}. 새 진입 멈춤", pos=pos)
            self.side = pos
            self.halted = True

    def enter(self, t: pd.Timestamp, side: int, z: float) -> None:
        q = self.b.quote()
        px = self.b.fill(side, f"진입 z {z:+.2f}", q)
        if px is None:
            self._check_pos("진입 실패 뒤")
            return
        self.side, self.exit_at = side, t + pd.Timedelta(minutes=self.h)
        self.entry = {"t": t, "side": side, "z": z, "px": px, "quote": q, "fee": self.b.last_fill_fee,
                      "mid": mid_of(q, px)}
        self._check_pos("진입 뒤")

    def exit(self, t: pd.Timestamp, why: str) -> bool:
        if self.side == 0:
            return True
        q = self.b.quote()
        px = self.b.fill(-self.side, why, q)
        if px is None:
            self._check_pos("청산 실패 뒤")
            return self.side == 0
        e = self.entry or {}
        if e:
            pnl_pt = self.side * (px - e["px"])
            fees = [x for x in (e.get("fee"), self.b.last_fill_fee) if x is not None]
            fee_est = analyze.fee_krw(e["px"]) + analyze.fee_krw(px)     # 수수료를 못 잡았을 때 (수수료율로 계산)
            mid_in = ((e["quote"].get("bid") or 0) + (e["quote"].get("ask") or 0)) / 2 or e["px"]
            tr = {"entry_t": e["t"], "exit_t": t, "side": self.side, "z": round(e["z"], 3),
                  "entry_px": e["px"], "exit_px": px, "entry_bid": e["quote"].get("bid"),
                  "entry_ask": e["quote"].get("ask"), "exit_bid": q.get("bid"), "exit_ask": q.get("ask"),
                  "pnl_pt": round(pnl_pt, 4), "fee_krw": sum(fees) if len(fees) == 2 else None,
                  "pnl_krw": round(pnl_pt * analyze.PT_KRW - (sum(fees) if len(fees) == 2 else fee_est)),
                  "net_bp": round(pnl_pt / mid_in * 1e4 - (sum(fees) if len(fees) == 2 else fee_est)
                                  / analyze.PT_KRW / mid_in * 1e4, 2), "why": why}
            self.trades.append(tr)
            if self.trades_csv is not None:      # 거래마다 덧붙임 — 중간에 죽고 다시 떠도 앞 거래가 남음
                pd.DataFrame([tr]).to_csv(self.trades_csv, mode="a", index=False,
                                          header=not self.trades_csv.exists())
            self.j.write("trade", text=f"거래 {len(self.trades)} · {tr['pnl_krw']:+,}원", **tr)
        self.side, self.exit_at, self.entry = 0, None, None
        self._check_pos("청산 뒤")
        return self.side == 0

    def day_pnl(self) -> float:
        return float(sum(x["pnl_krw"] for x in self.trades))

    def step(self, t: pd.Timestamp, z: float, stale: bool) -> None:
        """분 경계 t 에서: 시간 청산 → 손절 → 하루 한도 → 진입 판단 (analyze.simulate_risk 와 같은 순서)."""
        if self.side and self.exit_at is not None and t >= self.exit_at:
            self.exit(t, f"{self.h}분 보유 끝")
        if self.side and self.stop_pt and self.entry and np.isfinite(self.entry.get("mid", np.nan)):
            m = mid_of(self.b.quote())
            if np.isfinite(m) and self.side * (m - self.entry["mid"]) <= -self.stop_pt:
                self.exit(t, f"손절 {self.stop_pt:g}pt (중간가 {m:.2f}, 진입 {self.entry['mid']:.2f})")
        if not self.loss_halted and self.daily_loss_krw and self.day_pnl() <= -self.daily_loss_krw:
            self.loss_halted = True
            self.j.write("event", text=f"하루 손실 한도 {self.daily_loss_krw:,.0f}원 — 오늘은 새 진입 없음", pnl=self.day_pnl())
        want = decide(z, stale, self.rule)
        self.j.write("minute", t=t, z=None if not np.isfinite(z) else round(z, 3), stale=stale,
                     side=self.side, want=want)
        if self.side == 0 and not self.halted and not self.loss_halted and want and entry_window(t, self.h):
            self.enter(t, want, z)


def mid_of(q: dict, fallback: float = np.nan) -> float:
    bid, ask = q.get("bid"), q.get("ask")
    return (bid + ask) / 2 if bid and ask else float(fallback)


def read_investor(day: str) -> pd.DataFrame:
    inv, _ = convert.read_rest(RAW_DIR / day)
    return inv


def wait_until(ts: datetime, stop) -> None:
    while not stop() and now_kst() < ts:
        time.sleep(min(1.0, max(0.0, (ts - now_kst()).total_seconds())))


def live(paper: bool) -> int:
    from nh_client import NHClient

    lock = single_instance(LOCK)
    if lock is None:
        print("실행기가 이미 돌고 있음", flush=True)
        return 0
    now = now_kst()
    day = now.strftime("%Y%m%d")
    end = datetime.strptime(day + analyze.LAST_EXIT.replace(":", ""), "%Y%m%d%H%M")
    if now.weekday() >= 5 or now >= end or now.strftime("%H:%M") < "08:55":
        print("장 시간이 아님", flush=True)
        return 0
    rule, shadow_rule = load_rule(ORDER_RULE), load_rule(SHADOW_RULE)
    client = NHClient()
    if client.env != "demo":
        raise SystemExit("실행기는 모의 서버(demo)에서만 돌립니다.")
    j = Journal(LIVE_DIR / f"{day}.jsonl")
    js = Journal(LIVE_DIR / f"{day}_shadow.jsonl")
    broker = PaperBroker(client, j) if paper else MockAccountBroker(client, j)
    tr = Trader(broker, rule, j, trades_csv=LIVE_DIR / f"{day}_trades.csv")
    sh = Trader(PaperBroker(client, js), shadow_rule, js, trades_csv=LIVE_DIR / f"{day}_shadow_trades.csv")
    j.write("event", text=f"시작 ({broker.mode})", rule=rule)
    js.write("event", text="시작 (기록만, 호가 종이 체결)", rule=shadow_rule)
    if rule.get("for_day") != day:
        j.write("event", text=f"주문 규칙이 오늘용이 아님 (for_day {rule.get('for_day')}) — 그대로 씀. 밤 갱신 확인할 것")

    stopping = {"v": False}

    def on_sig(*_):
        stopping["v"] = True
    signal.signal(signal.SIGTERM, on_sig)
    signal.signal(signal.SIGINT, on_sig)

    pos = broker.position()
    if pos:
        j.write("event", text=f"시작할 때 미니 잔고 {pos} — 먼저 청산", pos=pos)
        tr.side = int(np.sign(pos))
        tr.entry = None
        tr.exit(pd.Timestamp(now_kst()).floor("1min"), "시작 때 남은 잔고 청산")

    while not stopping["v"]:
        nxt = (now_kst() + timedelta(minutes=1)).replace(second=0, microsecond=0)
        wait_until(nxt + timedelta(seconds=LAG_S), lambda: stopping["v"])
        if stopping["v"]:
            break
        t = pd.Timestamp(nxt)
        if nxt >= end:
            break
        try:
            f = investor_features(read_investor(day), day, t)
        except Exception as e:  # noqa: BLE001 — 파일이 아직 없거나 깨진 줄: 이 분은 건너뜀
            j.write("event", text="신호 계산 실패", error=f"{type(e).__name__}: {str(e)[:200]}")
            f = None
        for trader, r in ((tr, rule), (sh, shadow_rule)):
            z, stale = np.nan, True
            if f is not None:
                try:
                    z, stale = signal_at(analyze.add_signal(f, r), t, r)
                except Exception as e:  # noqa: BLE001
                    trader.j.write("event", text="신호 계산 실패", error=f"{type(e).__name__}: {str(e)[:200]}")
            trader.step(t, z, stale)

    # 끝: 무조건 청산 (실패하면 몇 번 더)
    for _ in range(5):
        if tr.exit(pd.Timestamp(now_kst()).floor("1min"), "장 마감/중단 청산"):
            break
        time.sleep(2)
    sh.exit(pd.Timestamp(now_kst()).floor("1min"), "장 마감/중단 청산")
    for trader in (tr, sh):
        net = sum(x["pnl_krw"] for x in trader.trades)
        trader.j.write("event", text=f"끝 · 거래 {len(trader.trades)}건 · {net:+,}원", side=trader.side)
    if isinstance(broker, MockAccountBroker):
        broker.account("끝")
    return 0


# ---------------------------------------------------------------- 다시 돌려 보기
class ReplayBroker(PaperBroker):
    """저장된 1분 표의 중간가로 체결 (판단 순서·시각이 analyze.simulate 와 같은지 보는 용도)."""
    mode = "replay"

    def __init__(self, mids: pd.Series, journal: Journal, spreads: pd.Series | None = None):
        super().__init__(None, journal)
        self.mids, self.spreads, self.now = mids, spreads, None

    def quote(self) -> dict:
        m = float(self.mids.get(self.now, np.nan))
        sp = float(self.spreads.get(self.now, np.nan)) if self.spreads is not None else 0.0
        sp = sp if np.isfinite(sp) else analyze.TICK_PT
        return {"bid": m - sp / 2, "ask": m + sp / 2, "last": m}


def replay(day: str, rule: dict, out: Path) -> list[dict]:
    p = pd.read_csv(convert.PANEL_DIR / f"{day}.csv", index_col="t", parse_dates=True)
    f = analyze.add_signal(analyze.features(p), rule)
    j = Journal(out)
    b = ReplayBroker(f["mid"], j, f["spread"])
    tr = Trader(b, rule, j)
    for t in f.index:
        if t.strftime("%H:%M") >= analyze.LAST_EXIT:
            break
        b.now = t
        z, stale = signal_at(f, t, rule)
        tr.step(t, z, stale)
    b.now = f.index[f.index.strftime("%H:%M") >= analyze.LAST_EXIT][0]
    tr.exit(b.now, "마감")
    return tr.trades


def simulate_rule(day: str, rule: dict) -> list[dict]:
    p = pd.read_csv(convert.PANEL_DIR / f"{day}.csv", index_col="t", parse_dates=True)
    f = analyze.add_signal(analyze.features(p), rule)
    return analyze.simulate_risk(f, rule["signal"], int(rule["h"]), rule["theta"], rule["sign"], rule["sd"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--paper", action="store_true", help="주문 없이 호가로 종이 체결")
    ap.add_argument("--replay", metavar="YYYYMMDD", help="저장된 1분 표로 판단만 다시 돌림 (두 규칙 다)")
    a = ap.parse_args(argv)
    if a.replay:
        for name, path in (("주문(국면 필터)", ORDER_RULE), ("기록(개인)", SHADOW_RULE)):
            r = load_rule(path)
            tr = replay(a.replay, r, LIVE_DIR / f"replay_{a.replay}.jsonl")
            sim = simulate_rule(a.replay, r)
            same = [(x["entry_t"], x["exit_t"]) for x in tr] == [(x["t"], x["exit_t"]) for x in sim]
            print(f"{name}: replay {len(tr)}건 {sum(x['pnl_krw'] for x in tr):+,.0f}원, "
                  f"analyze.simulate_risk {len(sim)}건, 진입·청산 시각 일치 {same}")
        (LIVE_DIR / f"replay_{a.replay}.jsonl").unlink(missing_ok=True)
        return 0
    return live(a.paper)


if __name__ == "__main__":
    sys.exit(main())
