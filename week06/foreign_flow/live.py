"""미니코스피200 모의매매 실행기 (6주차, 11/23~).

  규칙 파일 (analyze.py --freeze 가 씀, results/rules/):
    order.json         모의계좌 주문 (위험관리 켬)
    order_norisk.json  같은 규칙, 위험관리 끔 — 호가 종이 체결 (위험관리 전·후 비교용)
    paper.json         둘째 후보 — 호가 종이 체결

  .venv/bin/python live.py                     # 모의계좌 주문 (기본)
  .venv/bin/python live.py --paper             # 주문 없이 셋 다 종이 체결
  .venv/bin/python live.py --replay 20261120   # 저장된 1분 표로 판단만 다시 돌려 analyze.simulate_rule 과 같은지 확인
  ./run_live.sh                                # cron 용 (이미 돌고 있으면 바로 끝남)

어떻게 도나:
  - API 로 시세·투자자 동향을 따로 받지 않음. 같은 서버의 수집기(record.py)가 쓰는 파일을 읽음:
      투자자 동향  data/raw/오늘/rest.jsonl
      웹소켓 체결  data/raw/오늘/HH.jsonl (지금 시간, 새로 붙은 줄만) + HH.jsonl.gz (지난 시간)
    → API 호출·웹소켓 접속이 늘지 않음. 수집기가 죽으면 값이 늦어져서 진입이 저절로 멈춤.
  - 매 분 t+3초에 두 파일을 읽어 convert.investor_cols·trade_cols → analyze.features 로 t 행 신호 계산 (분석과 같은 코드).
  - 투자자 스냅샷이 120초 넘게 늦으면(analyze.STALE_S) 진입 안 함. 체결 신호(aggr) 규칙은 마지막 체결이
    WS_STALE_S 초 넘게 지났을 때도 진입 안 함 (웹소켓 끊김).
  - |z| > θ 이고 포지션이 없으면 sign(z)·rule.sign 방향 시장가 주문, h 분 뒤 분 경계에서 한꺼번에 시장가 청산.
  - 위험관리는 규칙 파일의 risk. 0 이면 끔.
      daily_loss_krw   그날 실현 손익이 이만큼 지면 새 진입 없음
      stop_pt          고정 손절 (중간가가 진입 중간가보다 이만큼 불리하면 청산)
      stop_vol_k       변동폭 손절: 진입 때 analyze.move_sd × k 를 그 포지션의 손절 폭으로 고정 (stop_pt 대신)
      vol_fallback_pt  장 초반 변동폭을 아직 못 잴 때 쓰는 값
      size_theta2/n2   |z| ≥ size_theta2 면 size_n2 계약, 아니면 1계약
  - 08:55 전·15:35 뒤·주말엔 바로 끝남. 진입은 09:05~(15:35 − h), 15:35 에는 무조건 청산. 끝나거나 SIGTERM 이면 청산하고 끝냄.
  - 시작할 때 미니 잔고가 남아 있으면(전에 죽은 경우) 먼저 청산.
기록: data/live/YYYYMMDD_<규칙파일 이름>.jsonl·_trades.csv. 계좌번호 등은 지움.
"""
from __future__ import annotations

import argparse
import gzip
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
RULE_DIR = analyze.RULE_DIR
ORDER_RULE = RULE_DIR / "order.json"
PAPER_RULES = (RULE_DIR / "order_norisk.json", RULE_DIR / "paper.json")
LOCK = HERE / "data" / "live.lock"
LAG_S = 3                     # 분 경계 뒤 이만큼 기다렸다 판단 (수집기의 :00 조회가 파일에 들어올 시간)
WS_STALE_S = 120              # 마지막 체결이 이만큼 넘게 지났으면 체결 신호는 못 믿음
PRIVATE_KEYS = {"ac_no", "ac_nm", "user_ip_addr", "ord_id"}
SIGNALS = analyze.INVESTOR + ("aggr_",)


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
def load_rule(path: Path) -> dict:
    rule = json.loads(path.read_text())
    if not rule["signal"].startswith(SIGNALS):
        raise SystemExit(f"실행기는 투자자 동향·체결 신호만 계산합니다: {rule['signal']}")
    rule.setdefault("name", path.stem)
    rule.setdefault("risk", analyze.NO_RISK)
    return rule


def uses_ws(rule: dict) -> bool:
    return rule["signal"].startswith("aggr_")


class WsTail:
    """수집기 웹소켓 파일에서 KA 체결(tick_div=1)만, 새로 붙은 만큼 읽음.

    수집기는 지금 시간을 HH.jsonl 에 이어 쓰다가 시간이 바뀌면 HH.jsonl.gz 뒤에 붙이고 평문은 지움
    (record.HourlyWriter). 그래서 평문은 읽은 바이트 위치부터, gz 는 크기가 바뀌었을 때 처음부터 다시 읽고
    같은 체결(종목·시각·누적 매수/매도·수량)은 한 번만 넣음. 줄 순서는 파일 순서 그대로 (convert.read_ws 와 같음).
    """

    def __init__(self, day_dir: Path):
        self.dir = day_dir
        self.off: dict[str, int] = {}
        self.gz_size: dict[str, int] = {}
        self.seen: set = set()
        self.rows: list[dict] = []

    def _add(self, line: str) -> None:
        if '"KA"' not in line:
            return
        try:
            m = json.loads(line)["msg"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return
        h = m.get("header") if isinstance(m, dict) else None
        if not h or h.get("action_code") != "KA":
            return
        b = m.get("body") or {}
        if str(b.get("tick_div")) != "1":
            return
        sym = b.get("sym") or h.get("action_sym")
        key = (sym, b.get("exch_tm"), b.get("cum_buy_exec_qty"), b.get("cum_sel_exec_qty"), b.get("exec_qty"))
        if key in self.seen:
            return
        self.seen.add(key)
        self.rows.append(convert.ka_row(sym, b))

    def poll(self) -> pd.DataFrame:
        for hh in sorted({p.name[:2] for p in self.dir.glob("[0-2][0-9].jsonl*")}):
            gz = self.dir / f"{hh}.jsonl.gz"
            if gz.exists() and self.gz_size.get(hh) != gz.stat().st_size:
                size = gz.stat().st_size
                try:
                    with gzip.open(gz, "rt", encoding="utf-8") as f:
                        for line in f:
                            self._add(line)
                    self.gz_size[hh] = size
                except (EOFError, OSError):
                    pass                                   # 붙이는 중인 gz — 다음 분에 다시 읽음
            plain = self.dir / f"{hh}.jsonl"
            try:
                with plain.open("rb") as f:
                    size = f.seek(0, 2)
                    start = self.off.get(hh, 0)
                    if size < start:                       # 지워지고 새로 생긴 평문 (같은 시간에 수집기 재시작)
                        start = 0
                    f.seek(start)
                    chunk = f.read()
            except FileNotFoundError:
                continue
            end = chunk.rfind(b"\n")
            if end < 0:
                continue
            self.off[hh] = start + end + 1
            for line in chunk[:end].decode("utf-8", "replace").splitlines():
                self._add(line)
        return pd.DataFrame(self.rows, columns=list(convert.ka_row("", {}).keys()))


def market_features(inv: pd.DataFrame, ka: pd.DataFrame, day: str, upto: pd.Timestamp) -> pd.DataFrame:
    """투자자 줄 + 체결 줄 → upto 까지 1분 신호 (analyze.features 그대로). 호가 열은 비워 둠."""
    idx = convert.minute_index(day)
    idx = idx[idx <= upto]
    p = pd.DataFrame(convert.investor_cols(inv, idx), index=idx)
    for c, v in convert.trade_cols(ka, day, idx).items():
        p[c] = v
    p["MINI_mid"], p["MINI_spread"] = np.nan, np.nan
    return analyze.features(p)


def ws_age_s(ka: pd.DataFrame, day: str, t: pd.Timestamp) -> float:
    """t 기준 마지막 체결(거래소 시각)이 몇 초 전인지. 체결이 없으면 inf."""
    if not len(ka):
        return float("inf")
    ts = convert._exch_ts(day, ka["exch_tm"].dropna())
    ts = ts[ts < t]
    return float((t - ts.max()).total_seconds()) if len(ts) else float("inf")


def signal_at(f: pd.DataFrame, t: pd.Timestamp, rule: dict, ws_stale: bool = False) -> tuple[float, bool]:
    """(z, 진입 막힘 여부). 값이 없으면 z = nan."""
    if t not in f.index:
        return np.nan, True
    z = f.at[t, rule["signal"]] / rule["sd"]
    blocked = bool(f.at[t, "stale"]) or (uses_ws(rule) and ws_stale)
    return (float(z) if np.isfinite(z) else np.nan), blocked


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

    def fill(self, side: int, why: str, q: dict, n: int = 1) -> float | None:
        px = q.get("ask") if side > 0 else q.get("bid")
        if not px:
            return None
        self.pos += side * n
        self.last_fill_fee = round(analyze.fee_krw(px) * n, 2)
        self.j.write("fill", side=side, n=n, price=px, source="paper", fee=self.last_fill_fee,
                     text=f"종이 체결 {n}계약 · {why}")
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

    def fill(self, side: int, why: str, q: dict, n: int = 1) -> float | None:
        from nh_client import NHError, rows
        self.last_fill_fee = None
        s = "buy" if side > 0 else "sell"
        try:
            res = self.c.market_order(SYM, s, n)
        except NHError as e:
            self.j.write("order", side=side, ok=False, text=f"주문 실패 · {why}", error=str(e)[:300])
            return None
        ord_no = str(res.get("ord_no") or "").strip()
        self.j.write("order", side=side, ord_no=ord_no, msg_cd=res.get("msg_cd"), msg_cntn=res.get("msg_cntn"),
                     ok=bool(ord_no), n=n, text=f"시장가 {n}계약 · {why}")
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
                self.j.write("fill", side=side, n=n, price=px, ord_no=ord_no, source="order-history",
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
                 stop_pt: float | None = None, daily_loss_krw: float | None = None):
        self.b, self.rule, self.j, self.trades_csv = broker, rule, journal, trades_csv
        self.h = int(rule["h"])
        risk = rule.get("risk", analyze.NO_RISK)
        self.stop_pt = risk.get("stop_pt", 0.0) if stop_pt is None else stop_pt
        self.daily_loss_krw = risk.get("daily_loss_krw", 0) if daily_loss_krw is None else daily_loss_krw
        self.stop_vol_k = float(risk.get("stop_vol_k", 0.0))
        self.vol_fallback_pt = float(risk.get("vol_fallback_pt", np.nan))
        self.size_theta2, self.size_n2 = float(risk.get("size_theta2", 0.0)), int(risk.get("size_n2", 1))
        self.n = 0                        # 지금 들고 있는 계약 수
        self.loss_halted = False          # 하루 손실 한도에 걸림 (그날은 새 진입 없음)
        self.side = 0                     # 지금 들고 있는 방향 (실행기 기준)
        self.exit_at: pd.Timestamp | None = None
        self.entry: dict | None = None
        self.trades: list[dict] = []
        self.halted = False

    def _check_pos(self, why: str) -> None:
        pos = self.b.position()
        if pos is not None and pos != self.side * self.n:
            self.j.write("event", text=f"잔고 불일치 ({why}) — 실행기 {self.side * self.n}, 잔고 {pos}. 새 진입 멈춤", pos=pos)
            self.side, self.n = int(np.sign(pos)), abs(int(pos))
            self.halted = True

    def size(self, z: float) -> int:
        return self.size_n2 if self.size_theta2 and abs(z) >= self.size_theta2 else 1

    def stop_for(self, msd: float) -> float:
        """이번 포지션의 손절 폭 (pt). 0 이면 손절 없음."""
        if self.stop_vol_k:
            return self.stop_vol_k * (msd if np.isfinite(msd) else self.vol_fallback_pt)
        return self.stop_pt

    def enter(self, t: pd.Timestamp, side: int, z: float, msd: float = np.nan) -> None:
        n, stop = self.size(z), self.stop_for(msd)
        q = self.b.quote()
        how = f"진입 z {z:+.2f} · {n}계약" + (f" · 손절 {stop:.2f}pt" if stop else "")
        px = self.b.fill(side, how, q, n)
        if px is None:
            self._check_pos("진입 실패 뒤")
            return
        self.side, self.n, self.exit_at = side, n, t + pd.Timedelta(minutes=self.h)
        self.entry = {"t": t, "side": side, "z": z, "n": n, "px": px, "quote": q, "fee": self.b.last_fill_fee,
                      "mid": mid_of(q, px), "stop_pt": stop, "move_sd": msd}
        self._check_pos("진입 뒤")

    def exit(self, t: pd.Timestamp, why: str) -> bool:
        if self.side == 0:
            return True
        q = self.b.quote()
        n = max(self.n, 1)
        px = self.b.fill(-self.side, why, q, n)
        if px is None:
            self._check_pos("청산 실패 뒤")
            return self.side == 0
        e = self.entry or {}
        if e:
            pnl_pt = self.side * (px - e["px"]) * n                        # 계약 수 곱한 pt
            fees = [x for x in (e.get("fee"), self.b.last_fill_fee) if x is not None]
            fee_est = (analyze.fee_krw(e["px"]) + analyze.fee_krw(px)) * n  # 수수료를 못 잡았을 때 (수수료율로 계산)
            mid_in = ((e["quote"].get("bid") or 0) + (e["quote"].get("ask") or 0)) / 2 or e["px"]
            tr = {"entry_t": e["t"], "exit_t": t, "side": self.side, "n": n, "z": round(e["z"], 3),
                  "stop_pt": round(e.get("stop_pt") or 0.0, 3),
                  "entry_px": e["px"], "exit_px": px, "entry_bid": e["quote"].get("bid"),
                  "entry_ask": e["quote"].get("ask"), "exit_bid": q.get("bid"), "exit_ask": q.get("ask"),
                  "pnl_pt": round(pnl_pt, 4), "fee_krw": sum(fees) if len(fees) == 2 else None,
                  "pnl_krw": round(pnl_pt * analyze.PT_KRW - (sum(fees) if len(fees) == 2 else fee_est)),
                  "net_bp": round((pnl_pt / mid_in * 1e4 - (sum(fees) if len(fees) == 2 else fee_est)
                                   / analyze.PT_KRW / mid_in * 1e4) / n, 2), "why": why}   # 1계약당 bp
            self.trades.append(tr)
            if self.trades_csv is not None:      # 거래마다 덧붙임 — 중간에 죽고 다시 떠도 앞 거래가 남음
                pd.DataFrame([tr]).to_csv(self.trades_csv, mode="a", index=False,
                                          header=not self.trades_csv.exists())
            self.j.write("trade", text=f"거래 {len(self.trades)} · {tr['pnl_krw']:+,}원", **tr)
        self.side, self.n, self.exit_at, self.entry = 0, 0, None, None
        self._check_pos("청산 뒤")
        return self.side == 0

    def day_pnl(self) -> float:
        return float(sum(x["pnl_krw"] for x in self.trades))

    def step(self, t: pd.Timestamp, z: float, stale: bool, msd: float = np.nan) -> None:
        """분 경계 t 에서: 시간 청산 → 손절 → 하루 한도 → 진입 판단 (analyze.simulate_risk 와 같은 순서).
        msd = t 행 analyze.move_sd (변동폭 손절용)."""
        if self.side and self.exit_at is not None and t >= self.exit_at:
            self.exit(t, f"{self.h}분 보유 끝")
        stop = (self.entry or {}).get("stop_pt") or 0.0
        if self.side and stop and np.isfinite(self.entry.get("mid", np.nan)):
            m = mid_of(self.b.quote())
            if np.isfinite(m) and self.side * (m - self.entry["mid"]) <= -stop:
                self.exit(t, f"손절 {stop:.2f}pt (중간가 {m:.2f}, 진입 {self.entry['mid']:.2f})")
        if not self.loss_halted and self.daily_loss_krw and self.day_pnl() <= -self.daily_loss_krw:
            self.loss_halted = True
            self.j.write("event", text=f"하루 손실 한도 {self.daily_loss_krw:,.0f}원 — 오늘은 새 진입 없음", pnl=self.day_pnl())
        want = decide(z, stale, self.rule)
        self.j.write("minute", t=t, z=None if not np.isfinite(z) else round(z, 3), stale=stale,
                     side=self.side, want=want)
        if self.side == 0 and not self.halted and not self.loss_halted and want and entry_window(t, self.h):
            self.enter(t, want, z, msd)


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
    rule = load_rule(ORDER_RULE)
    papers = [load_rule(p) for p in PAPER_RULES if p.exists()]
    client = NHClient()
    if client.env != "demo":
        raise SystemExit("실행기는 모의 서버(demo)에서만 돌립니다.")
    j = Journal(LIVE_DIR / f"{day}_order.jsonl")
    broker = PaperBroker(client, j) if paper else MockAccountBroker(client, j)
    tr = Trader(broker, rule, j, trades_csv=LIVE_DIR / f"{day}_order_trades.csv")
    traders = [(tr, rule)]
    j.write("event", text=f"시작 ({broker.mode})", rule=rule)
    for r, path in zip(papers, [p for p in PAPER_RULES if p.exists()]):
        js = Journal(LIVE_DIR / f"{day}_{path.stem}.jsonl")
        traders.append((Trader(PaperBroker(client, js), r, js, trades_csv=LIVE_DIR / f"{day}_{path.stem}_trades.csv"), r))
        js.write("event", text="시작 (기록만, 호가 종이 체결)", rule=r)
    if day < rule.get("valid_from", day):
        j.write("event", text=f"규칙 시작일({rule['valid_from']}) 전 — 그대로 씀")

    stopping = {"v": False}

    def on_sig(*_):
        stopping["v"] = True
    signal.signal(signal.SIGTERM, on_sig)
    signal.signal(signal.SIGINT, on_sig)

    pos = broker.position()
    if pos:
        j.write("event", text=f"시작할 때 미니 잔고 {pos} — 먼저 청산", pos=pos)
        tr.side, tr.n = int(np.sign(pos)), abs(int(pos))
        tr.entry = None
        tr.exit(pd.Timestamp(now_kst()).floor("1min"), "시작 때 남은 잔고 청산")

    ws = WsTail(RAW_DIR / day)
    ws_was_stale = None
    while not stopping["v"]:
        nxt = (now_kst() + timedelta(minutes=1)).replace(second=0, microsecond=0)
        wait_until(nxt + timedelta(seconds=LAG_S), lambda: stopping["v"])
        if stopping["v"]:
            break
        t = pd.Timestamp(nxt)
        if nxt >= end:
            break
        try:
            ka = ws.poll()
            age = ws_age_s(ka, day, t)
            f = market_features(read_investor(day), ka, day, t)
        except Exception as e:  # noqa: BLE001 — 파일이 아직 없거나 깨진 줄: 이 분은 건너뜀
            j.write("event", text="신호 계산 실패", error=f"{type(e).__name__}: {str(e)[:200]}")
            f, age = None, float("inf")
        ws_stale = age > WS_STALE_S and t.strftime("%H:%M") >= analyze.START
        if ws_stale != ws_was_stale and ws_was_stale is not None:
            j.write("event", text="웹소켓 체결 끊김 — 체결 신호 진입 멈춤" if ws_stale else "웹소켓 체결 다시 들어옴",
                    age_s=None if not np.isfinite(age) else round(age))
        ws_was_stale = ws_stale
        for trader, r in traders:
            z, stale, msd = np.nan, True, np.nan
            if f is not None:
                try:
                    z, stale = signal_at(f, t, r, ws_stale)
                    if trader.stop_vol_k and t in f.index:
                        msd = float(analyze.move_sd(f, trader.h).at[t])
                except Exception as e:  # noqa: BLE001
                    trader.j.write("event", text="신호 계산 실패", error=f"{type(e).__name__}: {str(e)[:200]}")
            trader.step(t, z, stale, msd)

    # 끝: 무조건 청산 (실패하면 몇 번 더)
    for _ in range(5):
        if tr.exit(pd.Timestamp(now_kst()).floor("1min"), "장 마감/중단 청산"):
            break
        time.sleep(2)
    for trader, _ in traders[1:]:
        trader.exit(pd.Timestamp(now_kst()).floor("1min"), "장 마감/중단 청산")
    for trader, _ in traders:
        net = sum(x["pnl_krw"] for x in trader.trades)
        trader.j.write("event", text=f"끝 · 거래 {len(trader.trades)}건 · {net:+,}원", side=trader.side)
    if isinstance(broker, MockAccountBroker):
        broker.account("끝")
    return 0


# ---------------------------------------------------------------- 다시 돌려 보기
class ReplayBroker(PaperBroker):
    """저장된 1분 표의 중간가로 체결 (판단 순서·시각이 analyze.simulate_rule 과 같은지 보는 용도)."""
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
    f = analyze.features(p)
    j = Journal(out)
    b = ReplayBroker(f["mid"], j, f["spread"])
    tr = Trader(b, rule, j)
    msd = analyze.move_sd(f, tr.h)
    for t in f.index:
        if t.strftime("%H:%M") >= analyze.LAST_EXIT:
            break
        b.now = t
        z, stale = signal_at(f, t, rule)
        tr.step(t, z, stale, float(msd.at[t]))
    b.now = f.index[f.index.strftime("%H:%M") >= analyze.LAST_EXIT][0]
    tr.exit(b.now, "마감")
    return tr.trades


def simulate_rule(day: str, rule: dict) -> list[dict]:
    p = pd.read_csv(convert.PANEL_DIR / f"{day}.csv", index_col="t", parse_dates=True)
    return analyze.simulate_rule(analyze.features(p), rule)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--paper", action="store_true", help="주문 없이 호가로 종이 체결")
    ap.add_argument("--replay", metavar="YYYYMMDD", help="저장된 1분 표로 판단만 다시 돌림 (규칙 파일 전부)")
    a = ap.parse_args(argv)
    if a.replay:
        out = LIVE_DIR / f"replay_{a.replay}.jsonl"
        for path in (ORDER_RULE, *PAPER_RULES):
            if not path.exists():
                continue
            r = load_rule(path)
            tr = replay(a.replay, r, out)
            sim = simulate_rule(a.replay, r)
            same = [(x["entry_t"], x["exit_t"]) for x in tr] == [(x["t"], x["exit_t"]) for x in sim]
            print(f"{path.name} ({r['name']}): replay {len(tr)}건 {sum(x['pnl_krw'] for x in tr):+,.0f}원, "
                  f"analyze.simulate_rule {len(sim)}건 {sum(x['krw'] for x in sim):+,.0f}원, 진입·청산 시각 일치 {same}")
        out.unlink(missing_ok=True)
        return 0
    return live(a.paper)


if __name__ == "__main__":
    sys.exit(main())
