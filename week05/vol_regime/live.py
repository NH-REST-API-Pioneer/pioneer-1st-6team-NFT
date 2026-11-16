"""변동성 국면 전환 실행기 — 해외 모의계좌, MES·MGC 1계약, 한 번 켜면 매일 알아서 계속 돕니다.

판단은 백테스트와 같은 코드입니다: features.build → 저장된 LightGBM(models/vol_lgbm.txt, 실행 중 재학습 없음)
→ regime(직전 20거래일 백분위, 상위 30% high / 하위 30% low) → engine.Engine("switch").
신호는 일반 계약(ES·GC) 연결선물 5분봉(cidx_yn=1, 학습 데이터와 같은 시세), 주문은 마이크로(MES·MGC).

  ./run.sh                 # 종이 체결 (주문 없음). 체결가 = 신호 봉 종가
  ./run.sh --orders        # 모의 주문

하루 흐름 (KST)
  00:00~06:44 대기 — 시세 조회·주문 안 함 (해외 모의계좌는 0~7시 체결 안 됨)
  06:45~06:59 준비 — 토큰 갱신, 과거 75일 봉으로 특징·백분위 채움
  07:00~23:59 거래 — 5분봉 마감 8초 뒤 판단. CME 휴장·주말은 건너뜀. 진입 체결 ~23:30, 23:45 봉에서 정리(23:50 체결)
  날짜가 바뀌었는데 포지션이 남아 있으면(재시작 등) 첫 봉에서 먼저 정리 ("overnight").

주문·잔고
  시장가, 주문내역에서 체결가가 확인된 것만 체결로 셈. 시장가는 다시 보내지 않음 (중복 위험).
  체결 확인 시간 초과 → 그 종목 결과 모름: 신규 진입 중단, 다음 봉 잔고로 확인 후 이어감.
  청산 거부·오류 → 다음 봉에 다시 (그동안 그 종목 진입 없음).
  봉마다 잔고 대조. 실행기가 아는 수량과 다르면 기록하고 신규 진입을 멈춤 (사람이 확인 후 state.json 정리).
  NH 오류가 10번 연속이면 신규 진입을 멈추고 기록 (루프는 계속, 성공하면 풀림).

기록: results/live/<YYYYMMDD>/events.csv · trades.csv · api.jsonl, 상태 results/live/state.json
"""
from __future__ import annotations

import argparse
import csv
import fcntl
import json
import math
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import config
import data
import engine
import features
import model
import regime
import session
from nh_client import NHClient, NHError, rows

KST = ZoneInfo("Asia/Seoul")
LIVE_DIR = config.RESULTS / "live"
WARM_DAYS = 75
FILL_WAIT_S = 20
FAIL_HALT = 10
UNKNOWN_MAX = 6        # 결과 모르는 주문을 이만큼 봉 동안 못 풀면 멈춤
ORDER_TOD = ("07:00:00", "23:59:30")   # 해외 모의계좌 체결 시간 안에서만 주문
_BAR = pd.Timedelta("5min")
_KEEP = ["ref_dt", "exch_tm", "open_pric", "high_pric", "low_pric", "close_pric", "cum_trd_qty"]


def now_kst() -> pd.Timestamp:
    return pd.Timestamp(datetime.now(KST).replace(tzinfo=None))


def phase(now: pd.Timestamp) -> str:
    tod = now.strftime("%H:%M")
    if tod < "06:45":
        return "sleep"
    return "warmup" if tod < "07:00" else "trade"


def root_for(orders: bool) -> Path:
    """종이·모의 기록과 state.json 을 따로 (종이로 시험한 뒤 모의로 켜도 상태가 섞이지 않게)."""
    return LIVE_DIR / ("mock" if orders else "paper")


def load_booster():
    return model.load(config.MODELS_DIR / "vol_lgbm.txt")


# ---------------------------------------------------------------- 기록
class Recorder:
    EV = ["time", "kind", "sym", "bar", "pred", "pct", "regime", "act", "strat", "side", "reason",
          "order_side", "qty", "ord_no", "price", "note"]
    TR = ["sym", "strat", "side", "regime", "entry_bar", "entry_px", "exit_bar", "exit_px", "reason",
          "gross_bp", "pnl_usd", "fee_usd", "net_usd", "mode"]

    def __init__(self, root: Path, clock):
        self.root, self.clock = Path(root), clock
        self.events: list[dict] = []

    def _write(self, name: str, fields: list[str], row: dict) -> None:
        d = self.root / self.clock().strftime("%Y%m%d")
        d.mkdir(parents=True, exist_ok=True)
        path = d / name
        new = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow(row)

    def event(self, kind: str, **kw) -> None:
        row = {"time": self.clock().strftime("%Y-%m-%d %H:%M:%S"), "kind": kind, **kw}
        for k, v in row.items():
            if isinstance(v, float):
                row[k] = "" if math.isnan(v) else round(v, 5)
        self.events.append(row)
        self._write("events.csv", self.EV, row)
        print(f"[{row['time']}] {kind} " + " ".join(f"{k}={v}" for k, v in kw.items() if v not in (None, "")),
              flush=True)

    def trade(self, **kw) -> None:
        self._write("trades.csv", self.TR, kw)

    def api(self, **kw) -> None:
        d = self.root / self.clock().strftime("%Y%m%d")
        d.mkdir(parents=True, exist_ok=True)
        with (d / "api.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"time": self.clock().strftime("%H:%M:%S"), **kw}, ensure_ascii=False) + "\n")


class LoggedClient(NHClient):
    """요청 경로·결과 코드·지연만 api.jsonl 에 (본문·계좌값은 남기지 않음)."""

    rec: Recorder | None = None

    def post(self, path, body, kind="market", retries=3):
        t0 = time.monotonic()
        try:
            out = super().post(path, body, kind, retries)
            ok, code = True, str(out.get("err_cd") or out.get("rsp_cd") or "")[:12]
            return out
        except NHError as e:
            ok, code = False, str(e.status)
            raise
        finally:
            if self.rec:
                self.rec.api(path=path, ok=ok, code=code, ms=round((time.monotonic() - t0) * 1000))


# ---------------------------------------------------------------- 시세
class Feed:
    def __init__(self, client, days: int = WARM_DAYS):
        self.c, self.days = client, days
        self.raw: dict[str, pd.DataFrame] = {}

    def warmup(self, now: pd.Timestamp) -> None:
        start = (now - pd.Timedelta(days=self.days)).strftime("%Y%m%d")
        for sym, spec in config.LIVE.items():
            self.raw[sym] = data.fetch_cont(self.c, *spec["sig"], start)

    def update(self, now: pd.Timestamp) -> None:
        for sym, spec in config.LIVE.items():
            body = self.c.chart_minute(*spec["sig"], start_dt="20000101", end_dt="99991231", dcnt=5,
                                       req_qty=120, cidx_yn="1")
            new = pd.DataFrame(rows(body))
            if not len(new):
                continue
            both = pd.concat([self.raw.get(sym), new[_KEEP]], ignore_index=True)
            both = both.drop_duplicates(["ref_dt", "exch_tm"], keep="last").sort_values(["ref_dt", "exch_tm"])
            cut = (now - pd.Timedelta(days=self.days)).strftime("%Y%m%d")
            self.raw[sym] = both[both["ref_dt"] >= cut].reset_index(drop=True)

    def bars(self, now: pd.Timestamp) -> dict[str, pd.DataFrame]:
        """끝난 봉만 (봉 시작 + 5분 ≤ now)."""
        out = {}
        for sym, raw in self.raw.items():
            df = data.clean(raw)
            out[sym] = df[df.index + _BAR <= now]
        return out


# ---------------------------------------------------------------- 주문
class Broker:
    """모의 주문 또는 종이 체결. 상태: filled / rejected / error(확실히 안 됨) / timeout·unknown(결과 모름)."""

    def __init__(self, client, rec: Recorder, orders: bool):
        self.c, self.rec, self.orders = client, rec, orders

    def _wait(self, prd: str, ord_no: str) -> tuple[str, float | None, str]:
        end = time.monotonic() + FILL_WAIT_S
        want = ord_no.strip().lstrip("0")
        while time.monotonic() < end:
            try:
                for r in rows(self.c.ovs_order_history(prd)):
                    if r.get("ord_no", "").strip().lstrip("0") != want:
                        continue
                    sts = r.get("ord_sts")
                    if sts == "6":
                        return "filled", float(r.get("exec_pric") or r.get("ord_avg_pric")), ""
                    if sts in ("8", "9"):
                        return "rejected", None, f"{r.get('err_cd')} {r.get('rejt_rsn') or r.get('err_msg')}".strip()
            except NHError as e:
                self.rec.event("조회오류", note=str(e)[:200])
            time.sleep(1)
        return "timeout", None, "체결 확인 시간 초과"

    def order(self, sym: str, side: str, qty: int, ref_px: float, why: str) -> tuple[str, float | None, str]:
        code, prd = config.LIVE[sym]["trd"]
        if not self.orders:
            self.rec.event("종이체결", sym=sym, order_side=side, qty=qty, price=ref_px, note=why)
            return "filled", ref_px, ""
        try:
            res = self.c.ovs_market_order(code, side, qty)
        except NHError as e:
            # 연결 끊김·시간 초과(0)·서버 오류(5xx)는 서버가 이미 받았을 수 있음 → 결과 모름
            st = "unknown" if e.status == 0 or e.status >= 500 else "error"
            self.rec.event("주문오류", sym=sym, order_side=side, qty=qty, note=f"{st} {str(e)[:200]}")
            return st, None, ""
        ord_no = str(res.get("ord_no", "")).strip()
        if res.get("err_cd") and str(res.get("err_cd")).strip() not in ("", "0", "00000"):
            self.rec.event("주문거부", sym=sym, order_side=side, qty=qty, ord_no=ord_no,
                           note=f"{res.get('err_cd')} {res.get('err_msg')}")
            return "rejected", None, ord_no
        st, px, note = self._wait(prd, ord_no)
        self.rec.event("체결" if st == "filled" else "미체결", sym=sym, order_side=side, qty=qty, ord_no=ord_no,
                       price=px, note=f"{why} {note}".strip())
        return st, px, ord_no

    def check(self, sym: str, ord_no: str) -> tuple[str, float | None]:
        """주문번호 하나의 지금 상태: filled / rejected / working / none."""
        want = ord_no.strip().lstrip("0")
        for r in rows(self.c.ovs_order_history(config.LIVE[sym]["trd"][1])):
            if r.get("ord_no", "").strip().lstrip("0") != want:
                continue
            sts = r.get("ord_sts")
            if sts == "6":
                return "filled", float(r.get("exec_pric") or r.get("ord_avg_pric") or "nan")
            return ("rejected", None) if sts in ("8", "9") else ("working", None)
        return "none", None

    def positions(self) -> dict[str, int]:
        out = {s: 0 for s in config.LIVE}
        codes = {spec["trd"][0]: s for s, spec in config.LIVE.items()}
        for r in rows(self.c.ovs_open_interest()):
            s = codes.get(r.get("sym", "").strip())
            if s:
                q = int(float(r.get("opint_qty") or 0))
                out[s] += q if r.get("trd_tp") == "1" else -q
        return out


# ---------------------------------------------------------------- 실행기
class Runner:
    def __init__(self, orders: bool, client=None, broker=None, clock=now_kst, sleep=time.sleep,
                 root: Path | None = None, booster=None):
        self.orders, self.clock, self.sleep = orders, clock, sleep
        self.root = Path(root) if root is not None else root_for(orders)
        self.state_path = self.root / "state.json"
        self.rec = Recorder(self.root, clock)
        if client is None:
            client = LoggedClient()
            client.rec = self.rec
        self.c = client
        self.bk = broker or Broker(client, self.rec, orders)
        self.booster = booster or load_booster()
        self.feed = Feed(client)
        self.eng = {s: engine.Engine("switch") for s in config.LIVE}
        self.held = {s: 0 for s in config.LIVE}
        self.open: dict[str, dict] = {}           # 체결된 진입 (손익 기록용)
        self.exit_pending: dict[str, dict] = {}   # 청산 못 끝낸 종목
        self.unknown: dict[str, dict] = {}        # 체결 여부 모르는 주문
        self.halt: str | None = None              # 잔고 불일치 — 사람 확인 필요
        self.api_fails = 0
        self.last_bar: dict[str, pd.Timestamp] = {}
        self.recon_failed = False
        self.last_px: dict[str, float] = {}
        self._load_state()

    # -- 상태
    def _save_state(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name("state.json.tmp")
        tmp.write_text(json.dumps({
            "orders": self.orders, "held": self.held, "engines": {s: e.state() for s, e in self.eng.items()},
            "exit_pending": self.exit_pending, "unknown": self.unknown, "halt": self.halt,
            "last_bar": {s: str(t) for s, t in self.last_bar.items()}, "open": self.open,
        }, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        os.replace(tmp, self.state_path)   # 쓰다가 죽어도 state.json 이 반쯤 남지 않게

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            d = json.loads(self.state_path.read_text(encoding="utf-8"))
            d["held"]
        except (ValueError, KeyError, TypeError) as e:
            bad = self.state_path.with_name(f"state.json.bad-{self.clock().strftime('%Y%m%d%H%M%S')}")
            os.replace(self.state_path, bad)
            self.halt = f"state.json 을 읽지 못함 ({type(e).__name__}) — {bad.name} 로 옮김, 잔고 확인 후 정리"
            self.rec.event("상태오류", note=self.halt)
            return
        if d.get("orders") != self.orders:
            raise SystemExit(f"state.json 이 {'모의 주문' if d.get('orders') else '종이'} 모드에서 남은 것입니다. "
                             "같은 모드로 켜거나 확인 후 지우세요.")
        self.held = {s: int(d["held"].get(s, 0)) for s in config.LIVE}
        for s, st in d.get("engines", {}).items():
            if s in self.eng:
                self.eng[s].load_state(st)
        self.exit_pending, self.unknown = d.get("exit_pending", {}), d.get("unknown", {})
        self.halt, self.open = d.get("halt"), d.get("open", {})
        self.last_bar = {s: pd.Timestamp(t) for s, t in d.get("last_bar", {}).items()}
        self.rec.event("상태복구", note=json.dumps(d, ensure_ascii=False, default=str)[:300])

    # -- 막힘
    def blocked(self, sym: str | None = None) -> bool:
        if self.halt or self.unknown or self.api_fails >= FAIL_HALT or self.recon_failed:
            return True
        return sym is not None and sym in self.exit_pending

    def order_window(self) -> bool:
        """지금 주문을 내도 되나: CME 가 열려 있고 KST 07:00:00~23:59:30."""
        now = self.clock()
        return session.market_open(now) and ORDER_TOD[0] <= now.strftime("%H:%M:%S") <= ORDER_TOD[1]

    # -- 잔고 대조
    def reconcile(self) -> bool:
        """잔고 대조. 조회가 실패하면 False (이 봉은 신규 진입 없이 청산만)."""
        if not self.orders:
            return True
        try:
            got = self.bk.positions()
        except NHError as e:
            self.rec.event("잔고조회오류", note=str(e)[:200])
            return False
        for s in config.LIVE:
            u = self.unknown.get(s)
            if u:
                self._resolve(s, u, got[s])
                continue
            if s in self.exit_pending and got[s] == 0 and self.held[s] != 0:
                self.held[s] = 0
                self.exit_pending.pop(s)
                self._close_trade(s, self.last_px.get(s, float("nan")), "청산(잔고로 확인)", self.clock())
                self.rec.event("체결확인", sym=s, note="잔고 0 — 앞선 청산이 체결돼 있었음")
                continue
            if got[s] != self.held[s]:
                self.halt = f"{s} 계좌 {got[s]} · 실행기 {self.held[s]}"
                self.rec.event("잔고불일치", sym=s, note=self.halt)
        return True

    def _resolve(self, s: str, u: dict, acct: int) -> None:
        st, px = "none", None
        if u.get("ord_no"):
            try:
                st, px = self.bk.check(s, u["ord_no"])
            except NHError as e:
                self.rec.event("조회오류", sym=s, note=str(e)[:200])
        if px is not None and math.isnan(px):
            px = None
        filled = st == "filled" or (st != "rejected" and acct == u["expect"])
        not_filled = st == "rejected" or (not u.get("ord_no") and acct == self.held[s] and u["checks"] >= 2)
        if filled:
            self.held[s] = u["expect"]
            if u["kind"] == "enter":
                self.eng[s].load_state(u["pos"])
                self.open[s] = {**u["open"], "entry_px": px or u["open"]["entry_px"]}
            else:
                self._close_trade(s, px or u["ref_px"], u["reason"], u["bar"])
            self.rec.event("체결확인", sym=s, ord_no=u.get("ord_no", ""), price=px,
                           note=f"{u['kind']} 체결 (주문내역 {st}, 잔고 {acct})")
        elif not_filled:
            if u["kind"] == "exit":
                self.exit_pending[s] = {"reason": u["reason"]}
            self.rec.event("체결확인", sym=s, ord_no=u.get("ord_no", ""), note=f"{u['kind']} 체결 안 됨 ({st})")
        elif acct not in (self.held[s], u["expect"]):
            self.halt = f"{s} 결과 모르는 주문 뒤 잔고 {acct} (실행기 {self.held[s]}, 기대 {u['expect']})"
            self.rec.event("잔고불일치", sym=s, note=self.halt)
        else:
            u["checks"] += 1
            if u["checks"] >= UNKNOWN_MAX:
                self.halt = f"{s} 주문 {u.get('ord_no') or '(번호 없음)'} 결과를 {UNKNOWN_MAX}봉 동안 모름"
                self.rec.event("진입중단", sym=s, note=self.halt)
            return
        del self.unknown[s]

    # -- 주문 처리
    def _side(self, pos_side: int, closing: bool) -> str:
        return ("sell" if pos_side > 0 else "buy") if closing else ("buy" if pos_side > 0 else "sell")

    def _close_trade(self, sym: str, px: float, reason: str, bar) -> None:
        o = self.open.pop(sym, None)
        if not o:
            return
        q, mult = config.LIVE[sym]["qty"], config.LIVE[sym]["mult"]
        gross_bp = o["side"] * (px / o["entry_px"] - 1) * 1e4
        pnl = o["side"] * (px - o["entry_px"]) * mult * q
        fee = 2 * config.FEE_SIDE * q
        self.rec.trade(sym=sym, strat=o["strat"], side=o["side"], regime=o["regime"], entry_bar=o["entry_bar"],
                       entry_px=o["entry_px"], exit_bar=str(bar), exit_px=px, reason=reason,
                       gross_bp=round(gross_bp, 3), pnl_usd=round(pnl, 2), fee_usd=fee, net_usd=round(pnl - fee, 2),
                       mode="모의" if self.orders else "종이")

    def handle(self, sym: str, res: dict, px: float) -> None:
        q = config.LIVE[sym]["qty"]
        if res["act"] == "enter":
            if self.blocked(sym) or not self.order_window():
                pos, self.eng[sym].pos = self.eng[sym].state(), None
                self.rec.event("진입보류", sym=sym, note=f"{self.halt or ''} {list(self.unknown)} "
                                                       f"api실패{self.api_fails} {list(self.exit_pending)}".strip())
                return
            side = res["side"]
            st, fill, ord_no = self.bk.order(sym, self._side(side, False), q, px, f"진입 {res['strat']}")
            pos = self.eng[sym].state()
            info = {"entry_bar": str(res["bar"]), "entry_px": fill, "side": side, "strat": res["strat"],
                    "regime": res["regime"]}
            if st == "filled":
                self.held[sym] = side * q
                self.open[sym] = info
            elif st in ("timeout", "unknown"):
                self.eng[sym].pos = None
                self.unknown[sym] = {"kind": "enter", "expect": side * q, "pos": pos, "ord_no": ord_no,
                                     "checks": 0, "open": {**info, "entry_px": px}}
            else:
                self.eng[sym].pos = None
        elif res["act"] == "exit":
            self._exit(sym, res["side"], res["reason"], px, res["bar"])

    def _exit(self, sym: str, side: int, reason: str, px: float, bar) -> None:
        if self.halt or not self.order_window():
            self.exit_pending[sym] = {"reason": reason}
            self.rec.event("정리대기", sym=sym, reason=reason,
                           note=f"멈춤: {self.halt}" if self.halt else "주문 시간 밖(CME 휴장 또는 0~7시) — 열리면 정리")
            return
        q = abs(self.held[sym]) or config.LIVE[sym]["qty"]
        st, fill, ord_no = self.bk.order(sym, self._side(side, True), q, px, f"청산 {reason}")
        if st == "filled":
            self.held[sym] = 0
            self.exit_pending.pop(sym, None)
            self._close_trade(sym, fill, reason, bar)
        elif st in ("timeout", "unknown"):
            self.exit_pending.pop(sym, None)
            self.unknown[sym] = {"kind": "exit", "expect": 0, "reason": reason, "ref_px": px, "bar": str(bar),
                                 "ord_no": ord_no, "checks": 0}
        else:
            self.exit_pending[sym] = {"reason": reason}

    def retry_exits(self, px: dict[str, float]) -> None:
        if self.halt or not self.order_window():
            return
        for sym, p in list(self.exit_pending.items()):
            if sym in self.unknown:
                continue
            if self.held[sym] == 0:
                self.exit_pending.pop(sym)
                continue
            side = 1 if self.held[sym] > 0 else -1
            self._exit(sym, side, f"{p['reason']} 재시도", px[sym], self.clock())

    # -- 한 봉
    def on_bar(self, now: pd.Timestamp) -> None:
        if hasattr(self.c, "token"):
            self.c.token()
        expected = now.floor("5min") - _BAR
        for _ in range(5):   # 차트가 봉 끝나고 바로 안 바뀔 때가 있어 최대 25초
            self.feed.update(now)
            bars = self.feed.bars(now)
            if all(len(b) and b.index[-1] >= expected for b in bars.values()):
                break
            self.sleep(5)
        self.recon_failed = not self.reconcile()
        f = features.build(bars)
        pred = model.predict(self.booster, f)
        pct = regime.for_all(pred)
        lab = regime.label(pct)
        last_px = {s: float(b["c"].iloc[-1]) for s, b in bars.items() if len(b)}
        self.last_px.update(last_px)
        if self.exit_pending:
            self.retry_exits(last_px)
        for sym in config.LIVE:
            b = bars[sym]
            if len(b) < engine.WARM:
                self.rec.event("시세부족", sym=sym, note=f"{len(b)}봉")
                continue
            bar = b.index[-1]
            e = self.eng[sym]
            if now - (bar + _BAR) > config.GAP_MAX:
                self.rec.event("시세지연", sym=sym, bar=str(bar), note="마지막 봉이 15분 넘게 지남 — 판단 건너뜀")
                if e.pos is not None and (session.must_flatten(expected) or expected.normalize() > e.pos.entry_bar.normalize()):
                    p, e.pos = e.pos, None   # 시세가 멈춰도 시간 정리는 함 (마지막 가격 기준)
                    self.handle(sym, {"act": "exit", "side": p.side, "strat": p.strat, "reason": "flat 시세지연",
                                      "regime": p.regime, "bar": expected}, last_px[sym])
                continue
            if sym in self.last_bar and bar <= self.last_bar[sym]:
                continue
            self.last_bar[sym] = bar
            if sym in self.exit_pending:
                self.rec.event("청산대기", sym=sym, bar=str(bar))
                continue
            if e.pos is not None and bar.normalize() > e.pos.entry_bar.normalize():
                p, e.pos = e.pos, None
                res = {"act": "exit", "side": p.side, "strat": p.strat, "reason": "overnight", "regime": p.regime,
                       "bar": bar}
            else:
                res = e.step(b, lab.loc[(sym, bar)])
            self.rec.event("봉", sym=sym, bar=str(bar), pred=float(pred.loc[(sym, bar)]),
                           pct=float(pct.loc[(sym, bar)]), regime=lab.loc[(sym, bar)], act=res["act"] or "",
                           strat=res["strat"], side=res["side"] or "", reason=res["reason"])
            self.handle(sym, res, last_px[sym])
        self._save_state()

    # -- 상주 루프
    def run(self) -> None:
        self.rec.event("시작", note=f"{'모의 주문' if self.orders else '종이'} · {list(config.LIVE)} · 상주")
        warmed, slept = False, False
        while True:
            now = self.clock()
            ph = phase(now)
            if ph == "sleep":
                if not slept:
                    self.rec.event("대기", note="00:00~06:44 시세 조회·주문 안 함")
                    slept = True
                warmed = False
                self.sleep(60)
                continue
            slept = False
            if not warmed:
                try:
                    self.c.token()
                    self.feed.warmup(now)
                    warmed = True
                    self.rec.event("준비", note={s: len(r) for s, r in self.feed.raw.items()})
                except Exception as e:  # 준비 실패: 간격을 늘려 다시
                    self.api_fails += 1
                    self.rec.event("준비오류", note=f"{self.api_fails}번째 {type(e).__name__}: {e}"[:300])
                    self.sleep(min(60, 2 ** min(self.api_fails, 6)))
                continue
            if ph != "trade":
                self.sleep(20)
                continue
            nxt = now.floor("5min") + _BAR + pd.Timedelta(seconds=8)
            self.sleep(max(1.0, (nxt - now).total_seconds()))
            now = self.clock()
            if phase(now) != "trade" or not session.market_open(now - _BAR):
                continue
            try:
                self.on_bar(now)
                if self.api_fails:
                    self.rec.event("복구", note=f"API 실패 {self.api_fails}번 뒤 성공")
                self.api_fails = 0
            except Exception as e:  # 실행기는 죽지 않게 — 기록하고 다음 봉
                self.api_fails += 1
                self.rec.event("API오류" if isinstance(e, NHError) else "예외",
                               note=f"{self.api_fails}번째 {type(e).__name__}: {e}"[:300])
                if self.api_fails == FAIL_HALT:
                    self.rec.event("진입중단", note=f"오류 {FAIL_HALT}번 연속 — 성공할 때까지 신규 진입 없음")
                self.sleep(min(60, 5 * self.api_fails))


def single_instance(lock: Path):
    """같은 실행기가 이미 돌고 있으면 바로 끝냄 (cron 이 5분마다 켜도 하나만 돎). 돌려준 파일을 열어 둬야 잠금 유지."""
    lock.parent.mkdir(parents=True, exist_ok=True)
    f = lock.open("w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        raise SystemExit(0)
    f.write(str(datetime.now()))
    f.flush()
    return f


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--orders", action="store_true", help="모의 주문을 냅니다 (없으면 종이 체결)")
    args = ap.parse_args(argv)
    if time.strftime("%z") != "+0900":
        raise SystemExit("컴퓨터 시간대가 KST 가 아닙니다. TZ=Asia/Seoul 로 실행하세요.")
    _lock = single_instance(LIVE_DIR / "live.lock")   # 종이·모의 합쳐 하나만 (같은 키)  # noqa: F841 (끝날 때까지 열어 둠)
    Runner(orders=args.orders).run()


if __name__ == "__main__":
    main()
