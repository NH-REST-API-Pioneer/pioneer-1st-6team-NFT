"""금·은 롤링 β 상대가치 실행기 — 해외 모의계좌, 5분봉 REST, 두 레그(MGC·MSI) 시장가.

판단은 rv_engine.RVEngine (백테스트와 거래가 똑같은 것을 test_rv_engine 으로 확인). 신호는 GC·SI(일반 계약)
5분봉으로 내고 — 백테스트와 같은 시세 — 주문은 마이크로 MGC·MSI 로 냅니다.

  python3 rv_runner.py                    # 종이 체결 (주문 없음). 체결가 = 신호 봉 종가
  python3 rv_runner.py --orders           # 모의 주문
  python3 rv_runner.py --orders --y-qty 3 --until 202611122350

돌아가는 방식
  5분마다 봉이 끝나고 8초 뒤 GC·SI 5분봉을 받아 엔진에 넣습니다. 진행 중인 봉은 버립니다.
  휴장: CME 금속은 뉴욕 17:00~18:00 쉬고 금요일 17:00 부터 주말. 실행기는 휴장 5분 전 봉에서 정리합니다
       (백테스트는 휴장 직전 봉 종가에 정리 — 그 시각엔 이미 장이 닫혀 실제로는 못 하므로 한 봉 일찍).
  수량: 금 y_qty 계약, 은 = round(|β| × 금 명목 / 은 1계약 명목). 은이 0 계약이면 진입하지 않음.

두 레그 처리 (과제 핵심)
  진입: 두 주문을 바로 이어서 내고 각각 체결을 기다림(20초). 한쪽이 거부·미체결이면 그 쪽만 한 번 더 내고,
        그래도 안 되면 체결된 쪽을 반대 주문으로 되돌려 포지션 없음으로 돌아감 ("레그 실패").
  청산: 안 된 레그만 3번까지 다시 냄. 그래도 안 되면 「청산 대기」로 두고 다음 봉에 또 시도. 그동안 진입 안 함.
  잔고 대조: 봉마다 해외 잔고의 MGC·MSI 수량을 실행기가 아는 수량과 비교. 다르면 기록하고 신규 진입을 멈춤
        (다른 사람이 같은 계좌로 같은 종목을 거래할 수도 있어서 남의 포지션을 마음대로 정리하지 않음).
  재시작: 포지션이 있으면 results/live/state.json 에 저장 → 다시 켜면 이어서 관리.

위험 관리: 손절 1.5σ(엔진), 하루(KST) 손절 3번이면 그날 진입 중단, --until 시각에 포지션 정리 후 종료,
  11/13(대회 날, 해외 모의계좌는 대회 전략만)에는 켜지지 않음.
기록: results/live/<YYYYMMDD>/events.csv (봉마다 z·β 와 모든 주문), trades.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from nh_client import NHClient, NHError, rows
from rv_engine import RVEngine, RVParams

HERE = Path(__file__).parent
LIVE = HERE / "results" / "live"
STATE = LIVE / "state.json"
KST, NY = ZoneInfo("Asia/Seoul"), ZoneInfo("America/New_York")
SIG = {"y": ("GCZ26", "FCME"), "x": ("SIZ26", "FCME")}          # 신호용 시세
TRD = {"y": ("MGCZ26", "MGC", 10), "x": ("MSIZ26", "MSI", 1000)}  # 주문 종목, 품목, 승수
FEE = 2.0  # 마이크로 편도 $ (NH 표 가정, 실측은 체결 뒤 예수금으로 확인)
CONTEST_DAY = "20261113"
FILL_WAIT_S = 20


# ---------------------------------------------------------------- 기록
class Recorder:
    EV = ["time", "kind", "bar", "z", "beta", "sd", "adverse_sd", "act", "reason", "side",
          "sym", "order_side", "qty", "ord_no", "price", "note"]
    TR = ["entry_time", "exit_time", "side", "beta", "y_qty", "x_qty", "y_in", "x_in", "y_out", "x_out",
          "pnl_usd", "fee_usd", "net_usd", "reason", "mode"]

    def __init__(self):
        self.day = None

    def _file(self, name: str, fields: list[str]):
        day = datetime.now(KST).strftime("%Y%m%d")
        d = LIVE / day
        d.mkdir(parents=True, exist_ok=True)
        path = d / name
        new = not path.exists()
        f = path.open("a", newline="", encoding="utf-8")
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        return f, w

    def event(self, kind: str, **kw) -> None:
        f, w = self._file("events.csv", self.EV)
        row = {"time": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"), "kind": kind, **kw}
        for k in ("z", "beta", "sd", "adverse_sd"):
            if isinstance(row.get(k), float):
                row[k] = "" if math.isnan(row[k]) else round(row[k], 4)
        w.writerow(row)
        f.close()
        print(f"[{row['time']}] {kind} " + " ".join(f"{k}={v}" for k, v in kw.items() if v not in (None, "")),
              flush=True)

    def trade(self, **kw) -> None:
        f, w = self._file("trades.csv", self.TR)
        w.writerow(kw)
        f.close()


# ---------------------------------------------------------------- 시세
def fetch_5m(client: NHClient, sym: str, exch: str, n: int = 120) -> pd.DataFrame:
    """최근 5분봉 n 개 (최신부터 옴 → 시간순으로). 시각 = 봉 시작 KST."""
    body = client.chart_minute(sym, exch, start_dt="20000101", end_dt="99991231", dcnt=5, req_qty=n)
    r = rows(body)
    df = pd.DataFrame(r)
    ts = pd.to_datetime(df["ref_dt"] + df["exch_tm"].str.zfill(6), format="%Y%m%d%H%M%S")
    out = pd.DataFrame({"o": np.log(df["open_pric"].astype(float).values),
                        "c": np.log(df["close_pric"].astype(float).values)}, index=ts)
    return out[~out.index.duplicated(keep="last")].sort_index()


def aligned(client: NHClient, now: pd.Timestamp) -> pd.DataFrame:
    y = fetch_5m(client, *SIG["y"])
    x = fetch_5m(client, *SIG["x"])
    j = pd.concat({"y": y, "x": x}, axis=1, sort=True).ffill(limit=2).dropna()
    return j[j.index <= now - pd.Timedelta("5min")]  # 끝난 봉만


def pre_break(bar: pd.Timestamp) -> bool:
    """이 봉 다음 봉이 휴장 직전 마지막 봉이면 True (= 휴장 5분 전에 정리). 뉴욕 17:00 휴장, 금요일 = 주말."""
    close_time = (bar + pd.Timedelta("10min")).tz_localize(KST).tz_convert(NY)
    return close_time.hour == 17 and close_time.minute == 0


def market_open(now: pd.Timestamp) -> bool:
    ny = now.tz_localize(KST).tz_convert(NY)
    if ny.weekday() == 5 or (ny.weekday() == 4 and ny.hour >= 17) or (ny.weekday() == 6 and ny.hour < 18):
        return False
    return not (ny.hour == 17)


# ---------------------------------------------------------------- 주문
class Broker:
    """모의 주문 또는 종이 체결. 레그 하나 = (키 y/x, 매수/매도, 수량)."""

    def __init__(self, client: NHClient, rec: Recorder, orders: bool):
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

    def leg(self, key: str, side: str, qty: int, ref_px: float, why: str) -> tuple[bool, float | None, str]:
        sym, prd, _ = TRD[key]
        if not self.orders:
            self.rec.event("종이체결", sym=sym, order_side=side, qty=qty, price=round(ref_px, 4), note=why)
            return True, ref_px, ""
        try:
            res = self.c.ovs_market_order(sym, side, qty)
        except NHError as e:
            self.rec.event("주문오류", sym=sym, order_side=side, qty=qty, note=str(e)[:200])
            return False, None, ""
        ord_no = str(res.get("ord_no", "")).strip()
        if res.get("err_cd") and str(res.get("err_cd")).strip() not in ("", "0", "00000"):
            self.rec.event("주문거부", sym=sym, order_side=side, qty=qty, ord_no=ord_no,
                           note=f"{res.get('err_cd')} {res.get('err_msg')}")
            return False, None, ord_no
        st, px, note = self._wait(prd, ord_no)
        self.rec.event("체결" if st == "filled" else "미체결", sym=sym, order_side=side, qty=qty, ord_no=ord_no,
                       price=px, note=f"{why} {note}".strip())
        return st == "filled", px, ord_no

    def positions(self) -> dict[str, int]:
        """해외 잔고에서 MGC·MSI 순수량 (+매수 −매도)."""
        out = {"y": 0, "x": 0}
        for r in rows(self.c.ovs_open_interest()):
            sym = r.get("sym", "").strip()
            for k, (s, _, _) in TRD.items():
                if sym == s:
                    q = int(float(r.get("opint_qty") or 0))
                    out[k] += q if r.get("trd_tp") == "1" else -q
        return out


# ---------------------------------------------------------------- 실행기
class Runner:
    def __init__(self, args):
        self.args = args
        self.c = NHClient()
        self.c.token()
        self.rec = Recorder()
        self.bk = Broker(self.c, self.rec, args.orders)
        self.eng = RVEngine(RVParams())
        self.held = {"y": 0, "x": 0}      # 실행기가 아는 순수량
        self.exit_pending = None          # 청산 못 끝낸 레그들
        self.halt = None                  # 신규 진입 중단 사유
        self.stops_today: dict[str, int] = {}
        self.last_bar = None
        self._load_state()

    # -- 상태 저장·복구
    def _save_state(self) -> None:
        LIVE.mkdir(parents=True, exist_ok=True)
        if self.eng.pos is None and not any(self.held.values()):
            STATE.unlink(missing_ok=True)
            return
        q = self.eng.pos
        STATE.write_text(json.dumps({
            "held": self.held, "exit_pending": self.exit_pending, "orders": self.args.orders,
            "pos": None if q is None else {"side": q.side, "beta": q.beta, "s0": q.s0, "sd0": q.sd0,
                                           "entry_bar": str(q.entry_bar), "extra": q.extra},
        }, ensure_ascii=False, indent=1), encoding="utf-8")

    def _load_state(self) -> None:
        if not STATE.exists():
            return
        d = json.loads(STATE.read_text(encoding="utf-8"))
        if d.get("orders") != self.args.orders:
            raise SystemExit(f"state.json 이 {'모의 주문' if d.get('orders') else '종이'} 모드에서 남은 것입니다. "
                             "같은 모드로 켜거나 확인 후 지우세요.")
        self.held, self.exit_pending = d["held"], d.get("exit_pending")
        if d.get("pos"):
            from rv_engine import Position
            p = d["pos"]
            self.eng.pos = Position(p["side"], p["beta"], p["s0"], p["sd0"], pd.Timestamp(p["entry_bar"]), p["extra"])
        self.rec.event("상태복구", note=json.dumps(d, ensure_ascii=False)[:300])

    # -- 한 봉
    def on_bar(self, now: pd.Timestamp) -> None:
        expected = now.floor("5min") - pd.Timedelta("5min")
        for _ in range(5):  # 차트가 봉 끝나고 바로 안 바뀔 때가 있어 최대 25초 기다림
            j = aligned(self.c, now)
            if len(j) and j.index[-1] >= expected:
                break
            time.sleep(5)
        if len(j) < self.eng.p.L:
            self.rec.event("시세부족", note=f"{len(j)}봉")
            return
        bar = j.index[-1]
        if self.last_bar is not None and bar <= self.last_bar:
            return
        self.last_bar = bar
        if now - bar > pd.Timedelta("15min"):
            self.rec.event("시세지연", bar=str(bar), note="마지막 봉이 15분 넘게 지남 — 판단 건너뜀")
            return
        if self.args.orders:
            self.reconcile()
        tail = j.iloc[-self.eng.p.L:]
        yc, xc = tail[("y", "c")].values, tail[("x", "c")].values
        info = self.eng.step(tail.index, yc, xc, pre_break(bar))
        self.rec.event("봉", bar=str(bar), z=info["z"], beta=info["beta"], sd=info["sd"],
                       adverse_sd=info.get("adverse_sd"), act=info["act"] or "", reason=info.get("reason", ""),
                       note=("발표구간 " if info["blk"] else "") + ("휴장전 " if info["brk"] else "")
                       + ("" if info["ok"] else "창이 휴장을 건넘"))
        py, px = math.exp(yc[-1]), math.exp(xc[-1])
        if self.exit_pending:
            self.finish_exit(py, px, retry_only=True)
            return
        if info["act"] == "exit":
            self.exit(info, py, px)
        elif info["act"] == "enter":
            self.enter(info, py, px)

    def enter(self, info: dict, py: float, px: float) -> None:
        day = datetime.now(KST).strftime("%Y%m%d")
        if self.halt:
            self.rec.event("진입보류", note=self.halt)
            return
        if self.stops_today.get(day, 0) >= 3:
            self.rec.event("진입보류", note="오늘 손절 3번")
            return
        side, beta = info["side"], info["beta"]
        y_qty = self.args.y_qty
        x_qty = round(abs(beta) * y_qty * py * TRD["y"][2] / (px * TRD["x"][2]))
        if x_qty < 1:
            self.rec.event("진입보류", note=f"은 수량 0 (β {beta:.3f})")
            return
        y_side = "buy" if side == 1 else "sell"
        x_side = ("sell" if side == 1 else "buy") if beta > 0 else y_side
        legs = [("y", y_side, y_qty, py), ("x", x_side, x_qty, px)]
        fills = {}
        for key, s, q, ref in legs:
            ok, fpx, _ = self.bk.leg(key, s, q, ref, "진입")
            if not ok:
                ok, fpx, _ = self.bk.leg(key, s, q, ref, "진입 재시도")
            if ok:
                fills[key] = (s, q, fpx)
                self.held[key] += q if s == "buy" else -q
        if len(fills) < 2:
            # 레그 실패 — 체결된 쪽 되돌리기
            for key, (s, q, _) in fills.items():
                back = "sell" if s == "buy" else "buy"
                ok, _, _ = self.bk.leg(key, back, q, py if key == "y" else px, "레그 실패 되돌림")
                if ok:
                    self.held[key] -= q if s == "buy" else -q
            self.rec.event("레그실패", note=f"체결된 레그 {list(fills)} 되돌림, 잔량 {self.held}")
            if any(self.held.values()):
                self.halt = "레그 실패 뒤 잔량 남음 — 직접 확인 필요"
            self._save_state()
            return
        self.eng.filled_entry(info, y_qty=y_qty, x_qty=x_qty, y_side=y_side, x_side=x_side,
                              y_in=fills["y"][2], x_in=fills["x"][2],
                              entry_time=datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"))
        self.rec.event("진입", side=side, beta=beta, z=info["z"],
                       note=f"금 {y_side} {y_qty} @{fills['y'][2]} · 은 {x_side} {x_qty} @{fills['x'][2]}")
        self._save_state()

    def exit(self, info: dict, py: float, px: float) -> None:
        q = self.eng.pos
        e = q.extra
        self.exit_pending = {
            "reason": info["reason"],
            "legs": [["y", "sell" if e["y_side"] == "buy" else "buy", e["y_qty"]],
                     ["x", "sell" if e["x_side"] == "buy" else "buy", e["x_qty"]]],
            "fills": {},
        }
        self.finish_exit(py, px)

    def finish_exit(self, py: float, px: float, retry_only: bool = False) -> None:
        ep = self.exit_pending
        for key, s, qty in ep["legs"]:
            if key in ep["fills"]:
                continue
            for k in range(3):
                ok, fpx, _ = self.bk.leg(key, s, qty, py if key == "y" else px,
                                         f"청산({ep['reason']})" + (f" 재시도{k}" if k or retry_only else ""))
                if ok:
                    ep["fills"][key] = fpx
                    self.held[key] += qty if s == "buy" else -qty
                    break
                time.sleep(3)
        if len(ep["fills"]) < 2:
            self.rec.event("청산대기", note=f"안 된 레그 {[l[0] for l in ep['legs'] if l[0] not in ep['fills']]} — 다음 봉에 다시")
            self._save_state()
            return
        q = self.eng.filled_exit()
        e = q.extra
        sgn = {"buy": 1, "sell": -1}
        pnl = (sgn[e["y_side"]] * e["y_qty"] * (ep["fills"]["y"] - e["y_in"]) * TRD["y"][2]
               + sgn[e["x_side"]] * e["x_qty"] * (ep["fills"]["x"] - e["x_in"]) * TRD["x"][2])
        fee = FEE * 2 * (e["y_qty"] + e["x_qty"])
        self.rec.trade(entry_time=e["entry_time"], exit_time=datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"),
                       side=q.side, beta=round(q.beta, 4), y_qty=e["y_qty"], x_qty=e["x_qty"],
                       y_in=e["y_in"], x_in=e["x_in"], y_out=ep["fills"]["y"], x_out=ep["fills"]["x"],
                       pnl_usd=round(pnl, 2), fee_usd=fee, net_usd=round(pnl - fee, 2), reason=ep["reason"],
                       mode="모의주문" if self.args.orders else "종이")
        self.rec.event("청산", side=q.side, reason=ep["reason"], note=f"손익 ${pnl:.2f} 수수료 ${fee:.0f} 순 ${pnl - fee:.2f}")
        if ep["reason"] == "손절":
            day = datetime.now(KST).strftime("%Y%m%d")
            self.stops_today[day] = self.stops_today.get(day, 0) + 1
        self.exit_pending = None
        self._save_state()

    def reconcile(self) -> None:
        try:
            got = self.bk.positions()
        except NHError as e:
            self.rec.event("잔고조회오류", note=str(e)[:200])
            return
        if got != self.held:
            self.rec.event("잔고불일치", note=f"계좌 {got} · 실행기 {self.held}")
            self.halt = f"잔고 불일치 계좌 {got} 실행기 {self.held}"
        elif self.halt and self.halt.startswith("잔고 불일치"):
            self.rec.event("잔고일치", note="다시 진입 허용")
            self.halt = None

    def flatten(self, why: str) -> None:
        if self.eng.pos is None and not self.exit_pending:
            return
        j = aligned(self.c, pd.Timestamp(datetime.now(KST).replace(tzinfo=None)))
        py, px = math.exp(j[("y", "c")].iloc[-1]), math.exp(j[("x", "c")].iloc[-1])
        if not self.exit_pending:
            self.exit({"reason": why}, py, px)
        else:
            self.finish_exit(py, px, retry_only=True)

    def run(self) -> None:
        until = pd.Timestamp(datetime.strptime(self.args.until, "%Y%m%d%H%M"))
        self.rec.event("시작", note=f"{'모의 주문' if self.args.orders else '종이'} · 금 {self.args.y_qty}계약 · "
                                    f"{self.eng.p} · 종료 {until}")
        if self.args.orders:
            got = self.bk.positions()
            if got != self.held:
                self.rec.event("잔고불일치", note=f"시작 때 계좌 {got} · 실행기 {self.held}")
                self.halt = f"잔고 불일치 계좌 {got} 실행기 {self.held}"
        fails = 0
        while True:
            now = pd.Timestamp(datetime.now(KST).replace(tzinfo=None))
            if now >= until or now.strftime("%Y%m%d") == CONTEST_DAY:
                try:
                    self.flatten("종료 시각")
                except Exception as e:
                    self.rec.event("정리오류", note=f"{type(e).__name__}: {e} — 잔고 직접 확인"[:300])
                self.rec.event("종료", note=f"포지션 {self.held}")
                break
            nxt = now.floor("5min") + pd.Timedelta("5min") + pd.Timedelta(seconds=8)
            time.sleep(max(1.0, (nxt - now).total_seconds()))
            now = pd.Timestamp(datetime.now(KST).replace(tzinfo=None))
            if not market_open(now - pd.Timedelta("5min")):
                continue
            try:
                self.on_bar(now)
                fails = 0
            except NHError as e:
                fails += 1
                self.rec.event("API오류", note=f"{fails}번째 {str(e)[:200]}")
                time.sleep(min(60, 5 * fails))
            except Exception as e:  # 실행기는 죽지 않게 — 기록하고 다음 봉
                fails += 1
                self.rec.event("예외", note=f"{type(e).__name__}: {e}"[:300])
                time.sleep(min(60, 5 * fails))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--orders", action="store_true", help="모의 주문을 냅니다 (없으면 종이 체결)")
    ap.add_argument("--y-qty", type=int, default=3, help="금(MGC) 계약 수, 은은 β 로 계산 (기본 3)")
    ap.add_argument("--until", default="202611122350", help="이 시각(KST, YYYYMMDDHHMM)에 정리하고 종료")
    args = ap.parse_args()
    if time.strftime("%z") != "+0900":
        raise SystemExit("컴퓨터 시간대가 KST 가 아닙니다. TZ=Asia/Seoul 로 실행하세요.")
    if datetime.now(KST).strftime("%Y%m%d") == CONTEST_DAY:
        raise SystemExit("11/13 은 대회 날이라 해외 모의계좌는 대회 전략만 돌립니다. 켜지 않습니다.")
    Runner(args).run()


if __name__ == "__main__":
    main()
