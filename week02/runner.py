"""2주차 실행기 — 채택 전략을 모의 서버에서 실제로 돌립니다 (VWAP · 볼린저+RSI 공용).

판정은 백테스트와 **같은 전략 객체**(strategy.py · strategy_bb.py)가 합니다. 실행기가 하는 일은
시세를 넣고, 신호대로 주문하고, 손절·익절·시간 청산·장 마감 정리를 감시하고, 모두 기록하는 것입니다.

## 시세를 받는 방법 — REST 폴링
- 1분봉: 분이 바뀌면 차트(분)로 **막 끝난 1분봉**을 받아 전략에 넣습니다. 백테스트와 같은 데이터
  출처라, 실행 때 본 봉과 백테스트의 봉이 같습니다.
- 현재가: 1초마다 현재가를 조회해 손절·익절을 봅니다 (국내 주문에는 예약 손절 주문이 없음).

## 주문 — 세 가지 모드
- `--orders` : 모의계좌에 **실제 시장가 주문**을 넣고, 주문내역으로 체결가를 확인합니다 (모의 서버만).
- 기본(종이) : 주문을 내지 않고 현재가 ± 1틱으로 체결된 것으로 기록합니다. **모의계좌 체결이 아닙니다.**
- `--replay YYYYMMDD` : 받아 둔 1분봉으로 그날을 다시 돌립니다 (서버 접속 없음, 종이 체결) — 코드 점검용.

## 안전장치
- 시작할 때 이 종목 잔고가 있으면 먼저 시장가로 정리합니다 (지난 실행이 비정상 종료된 경우).
- Ctrl+C 나 오류로 멈춰도 보유가 있으면 시장가로 정리하고 끝냅니다.
- 주문은 재시도하지 않습니다. 체결 확인이 안 되면 주문내역·잔고를 조회해 실제 상태를 따릅니다.

사용법 (week02/ 에서, 맥이 잠들지 않게 caffeinate 로):
  caffeinate -i -s python3 runner.py --strategy vwap --orders     # 모의계좌 주문
  caffeinate -i -s python3 runner.py --strategy bb                 # 종이 체결
  python3 runner.py --strategy vwap --replay 20261027              # 지난 하루 다시 돌리기
  python3 runner.py --strategy vwap --replay 20261030 --data results/data/bars_1m_A756B000_20261030.csv
  python3 runner.py --order-test                                   # 장중에 주문 경로만 점검 (매수 1 → 매도 1)
"""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import strategy as vwap_mod
import strategy_bb as bb_mod
from backtest import load_minutes, summarize
from strategy import TICK, TICK_VALUE, Minute, minutes_of, pnl_ticks, stop_price, take_profit_price

HERE = Path(__file__).parent
SLIP = TICK                      # 종이 체결 슬리피지 1틱 (백테스트와 같음)
PAPER_FEE = 690.0                # 종이 체결 수수료 (1계약 1번 체결) — 10/30 모의계좌 체결마다 690원
END_TIME = "153100"              # 15:30 정리 뒤 이 시각에 끝냄


def make_strategy(name: str):
    """(전략 객체, 익절 목표를 읽는 함수, 이름). 값은 각 Params 기본값 = 채택 값."""
    if name == "vwap":
        s = vwap_mod.VWAPReversion(vwap_mod.Params())
        return s, lambda: s.current_vwap, "VWAP 밴드 평균회귀"
    s = bb_mod.BBRSIReversion(bb_mod.Params())
    return s, lambda: s.target, "볼린저 밴드 + RSI 평균회귀"


def to_minute(r: dict) -> Minute:
    return Minute(r["ref_dt"], r["exch_tm"], float(r["open_pric"]), float(r["high_pric"]),
                  float(r["low_pric"]), float(r["close_pric"]), float(r.get("cum_trd_qty") or 0))


#: 기록·결과 파일에 남기지 않는 응답 칸 — 계좌 식별정보·개인정보 (주문내역·잔고 응답에 들어 있음)
PRIVATE_KEYS = {"ac_no", "ac_nm", "user_ip_addr", "ord_id"}


def num(v) -> float | None:
    """응답 숫자 문자열("-00000000000044000.000" 처럼 0 채움·부호 포함)을 float 로. 못 읽으면 None."""
    try:
        s = str(v).strip()
        return float(s) if s else None
    except (TypeError, ValueError):
        return None


#: 예수금 응답에서 기록하는 칸 — 하루 누적 값 (수수료·확정손익)
ACCOUNT_KEYS = ("fee", "fut_cls_pl_amt", "pl_tot_pric", "ord_psb_tot_amt")


def scrub(obj):
    """dict·list 안의 PRIVATE_KEYS 를 모두 지운 사본."""
    if isinstance(obj, dict):
        return {k: scrub(v) for k, v in obj.items() if k not in PRIVATE_KEYS}
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    return obj


class Journal:
    """한 줄 = 한 사건 (jsonl). 키·토큰과 계좌 식별정보(PRIVATE_KEYS)는 넣지 않습니다."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.f = path.open("a", encoding="utf-8")

    def write(self, kind: str, **data) -> None:
        data = scrub(data)
        rec = {"kind": kind, "at": datetime.now().isoformat(timespec="seconds"), **data}
        self.f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.f.flush()
        if kind in ("event", "order", "fill", "trade", "account") or (kind == "signal" and data.get("side")):
            msg = data.get("text") or data.get("reason") or ""
            c = str(data.get("clock", ""))
            hms = f"{c[:2]}:{c[2:4]}:{c[4:6]}" if len(c) == 6 and c.isdigit() else rec["at"][11:19]
            print(f"[{hms}] {kind:6s} {msg} "
                  f"{ {k: v for k, v in data.items() if k not in ('text', 'reason', 'clock', 'raw')} }")


# ---------------------------------------------------------------- 체결 담당
class PaperBroker:
    """주문을 내지 않고 기준가 ∓ 1틱(불리한 쪽)으로 체결된 것으로 봅니다. 수수료는 체결마다 fee 원."""
    mode = "paper"

    def __init__(self, journal: Journal, fee: float = PAPER_FEE):
        self.j = journal
        self.fee = fee
        self.last_fill_fee: float | None = fee

    def account(self, clock: str = "", why: str = "") -> dict | None:
        return None

    def fill(self, side: str, ref_price: float, clock: str, why: str) -> float | None:
        px = round(ref_price + SLIP if side == "buy" else ref_price - SLIP, 1)
        self.j.write("fill", clock=clock, side=side, price=px, source="paper", fee=self.fee,
                     text=f"종이 체결 · {why}")
        return px

    def position(self) -> int | None:
        return None     # 종이 체결은 실제 잔고가 없음


class MockAccountBroker:
    """모의계좌에 시장가 주문을 넣고 주문내역으로 체결가를 확인합니다."""
    mode = "mock-account"

    def __init__(self, client, sym: str, journal: Journal, exec_cnd: str):
        self.c, self.sym, self.j, self.exec_cnd = client, sym, journal, exec_cnd
        self.today = datetime.now().strftime("%Y%m%d")
        self.last_fee: float | None = None         # 마지막으로 본 예수금 fee (하루 누적)
        self.last_fill_fee: float | None = None    # 직전 체결의 수수료 (fee 차이) — 모르면 None

    def account(self, clock: str = "", why: str = "") -> dict | None:
        """예수금의 수수료·확정손익(하루 누적)을 읽어 기록합니다. 실패해도 매매는 그대로 — None 만 돌려줌."""
        try:
            dep = self.c.deposit(self.today)
            snap = {k: num(dep.get(k)) for k in ACCOUNT_KEYS}
        except Exception as e:      # 기록용 조회라 어떤 오류도 매매를 멈추지 않게
            self.j.write("event", clock=clock, text=f"예수금 조회 실패 ({why})", error=f"{type(e).__name__}: {str(e)[:200]}")
            return None
        if why:
            self.j.write("account", clock=clock, text=f"계좌 · {why}", **snap)
        return snap

    def _fee_after_fill(self, clock: str) -> float | None:
        """체결 직후 fee 차이 = 이 체결의 수수료. 반영이 늦으면 1초 뒤 한 번 더 봅니다."""
        before = self.last_fee
        snap = self.account(clock)
        if snap and before is not None and snap["fee"] == before:
            time.sleep(1.0)
            snap = self.account(clock) or snap
        if not snap or snap["fee"] is None:
            return None
        self.last_fee = snap["fee"]
        return None if before is None else round(snap["fee"] - before, 2)

    def fill(self, side: str, ref_price: float, clock: str, why: str) -> float | None:
        from nh_client import NHError, rows
        self.last_fill_fee = None
        try:
            res = self.c.market_order(self.sym, side, 1, self.exec_cnd)
        except NHError as e:
            self.j.write("order", clock=clock, side=side, ok=False, text=f"주문 실패 · {why}", error=str(e)[:300])
            return None
        ord_no = str(res.get("ord_no") or "").strip()
        self.j.write("order", clock=clock, side=side, ord_no=ord_no, msg_cd=res.get("msg_cd"),
                     msg_cntn=res.get("msg_cntn"), ok=bool(ord_no), text=f"시장가 1계약 · {why}")
        if not ord_no:
            return None
        # 체결 확인: 주문내역에서 이 주문번호가 전체체결(6)이 될 때까지 최대 15초
        last = None
        for _ in range(30):
            time.sleep(0.5)
            try:
                hist = rows(self.c.order_history())
            except NHError as e:
                self.j.write("event", clock=clock, text="주문내역 조회 실패", error=str(e)[:200])
                continue
            mine = [r for r in hist if str(r.get("ord_no", "")).strip().lstrip("0") == ord_no.lstrip("0")]
            if not mine:
                continue
            last = mine[0]
            sts = str(last.get("ord_sts", "")).strip()
            if sts == "6":
                # avg_exec_pric 가 "0000…0.000" 으로 올 때가 있어 0 이면 exec_pric 를 씀 (10/29 은석님 수정)
                px = (num(last.get("avg_exec_pric")) or num(last.get("exec_pric"))) or None
                self.last_fill_fee = self._fee_after_fill(clock)
                self.j.write("fill", clock=clock, side=side, price=px, ord_no=ord_no, source="order-history",
                             exec_qty=last.get("exec_qty"), exec_tm=last.get("exch_exec_tm"),
                             fee=self.last_fill_fee, fee_total=self.last_fee, text="모의계좌 체결")
                return px
            if sts in ("8", "9"):
                self.j.write("event", clock=clock, ord_no=ord_no, text=f"주문 {last.get('ord_sts_nm')}",
                             reject=last.get("ord_rejt_rsn"), raw=last)
                return None
        self.j.write("event", clock=clock, ord_no=ord_no, text="15초 안에 전체체결 확인 안 됨 — 잔고로 확인",
                     raw=last)
        return None

    def position(self) -> int | None:
        """이 종목 순보유 계약 수 (매수 +, 매도 −). 조회 실패면 None."""
        from nh_client import NHError, rows
        qty = 0
        try:
            oi = rows(self.c.open_interest())
        except NHError as e:
            self.j.write("event", text="잔고 조회 실패", error=str(e)[:200])
            return None
        for r in oi:
            if str(r.get("sym", "")).strip() == self.sym:
                n = int(num(r.get("opint_qty")) or 0)
                qty += n if str(r.get("trd_div")).strip() == "1" else -n
        return qty


# ---------------------------------------------------------------- 실행기
class Runner:
    def __init__(self, strategy_name: str, broker, journal: Journal):
        self.strat, self.target_of, self.title = make_strategy(strategy_name)
        self.p = self.strat.p
        self.broker, self.j = broker, journal
        self.pos: dict | None = None
        self.pending: dict | None = None      # 판정 봉 신호 → 다음 가격에서 진입
        self.trades: list[dict] = []
        self.live_date: str | None = None     # 이 날의 신호부터 진입 (그 전은 준비)
        self.last_price: float | None = None
        self.retry_after: str = ""            # 청산 체결이 확인 안 됐을 때 다시 시도할 시각 (HHMMSS)
        self.cur_date: str | None = None

    def _feed(self, m: Minute) -> list:
        """1분봉을 전략에 넣습니다. 날이 바뀌면 백테스트처럼 먼저 전날의 덜 닫힌 판정 봉을 닫습니다."""
        if self.cur_date is not None and m.date != self.cur_date:
            self.strat.end_of_day()
        self.cur_date = m.date
        return self.strat.on_minute(m)

    # 준비 봉: 신호는 기록만 하고 진입하지 않음
    def warmup(self, minutes: list[Minute]) -> None:
        for m in minutes:
            self._feed(m)
        if minutes:
            self.j.write("event", text=f"준비 봉 {len(minutes)}개 ({minutes[0].date} {minutes[0].time} ~ "
                                       f"{minutes[-1].date} {minutes[-1].time})")

    def on_minute_closed(self, m: Minute) -> None:
        for s in self._feed(m):
            b = s.bar
            if s.side or "신호" in s.reason:
                self.j.write("signal", clock=b.end, date=b.date, bar=b.start, close=b.close, side=s.side,
                             reason=s.reason, stop_ticks=s.stop_ticks,
                             **{k: round(v, 3) for k, v in asdict(b).items()
                                if k in ("vwap", "sigma", "upper", "lower", "middle", "rsi", "atr") and v is not None})
            if s.side and self.pos is None and self.pending is None and b.date == self.live_date:
                self.pending = {"side": s.side, "stop_ticks": s.stop_ticks, "signal": b.end}

    def on_price(self, clock: str, price: float, ref_low: float | None = None, ref_high: float | None = None) -> None:
        """가격 한 번. ref_low/high 는 다시 돌리기에서 그 사이 가격 범위를 줄 때 씁니다."""
        lo = price if ref_low is None else ref_low
        hi = price if ref_high is None else ref_high
        self.last_price = price
        if self.pending and self.pos is None and self.p.entry_start <= clock < self.p.entry_end:
            self._enter(clock, price)
        elif self.pending and not (self.p.entry_start <= clock < self.p.entry_end):
            self.j.write("event", clock=clock, text="진입 시간이 지나 신호를 버림")
            self.pending = None
        if self.pos is None or clock < self.retry_after:
            return
        side, pos = self.pos["side"], self.pos
        target = self.target_of()
        if clock >= self.p.flatten_at:
            self._exit(clock, price, "장 마감 정리")
        elif side == "buy" and lo <= pos["stop"]:
            self._exit(clock, min(price, pos["stop"]) if ref_low is None else pos["stop"], "손절")
        elif side == "sell" and hi >= pos["stop"]:
            self._exit(clock, max(price, pos["stop"]) if ref_high is None else pos["stop"], "손절")
        elif target is not None and side == "buy" and hi >= (tp := take_profit_price(side, target)):
            self._exit(clock, max(price, tp) if ref_high is None else tp, "익절")
        elif target is not None and side == "sell" and lo <= (tp := take_profit_price(side, target)):
            self._exit(clock, min(price, tp) if ref_low is None else tp, "익절")
        elif minutes_of(clock) - minutes_of(pos["entry_time"]) >= self.p.hold_min:
            self._exit(clock, price, "시간 청산")

    def _enter(self, clock: str, price: float) -> None:
        e, self.pending = self.pending, None
        px = self.broker.fill(e["side"], price, clock, f"진입 ({e['side']})")
        if px is None:
            held = self.broker.position()
            if not held:
                self.j.write("event", clock=clock, text="진입 체결 없음 (잔고 0) — 이 신호는 건너뜀")
                return
            px = price      # 체결은 됐는데 체결가를 못 받음 → 현재가로 기록하고 보유로 다룸
            self.j.write("event", clock=clock, text=f"체결가 미확인 · 잔고 {held:+d} → 현재가 {px} 로 기록")
        self.pos = {"side": e["side"], "entry": px, "entry_time": clock, "signal_time": e["signal"],
                    "stop_ticks": e["stop_ticks"], "stop": stop_price(e["side"], px, e["stop_ticks"]),
                    "entry_fee": getattr(self.broker, "last_fill_fee", None)}
        self.strat.on_entry()
        self.j.write("event", clock=clock, text=f"진입 {e['side']} {px} · 손절 {self.pos['stop']} ({e['stop_ticks']}틱)")

    def _exit(self, clock: str, ref: float, reason: str) -> None:
        side = "sell" if self.pos["side"] == "buy" else "buy"
        px = self.broker.fill(side, ref, clock, f"청산 · {reason}")
        if px is None:
            held = self.broker.position()
            if held == 0:   # 청산은 됐는데 체결가를 못 받음 → 현재가로 기록 (다시 주문하면 반대 포지션이 생김)
                px = ref
                self.j.write("event", clock=clock, text=f"청산 체결가 미확인 · 잔고 0 → {px} 로 기록")
            else:
                sec = int(clock[:2]) * 3600 + int(clock[2:4]) * 60 + int(clock[4:6]) + 10
                self.retry_after = f"{sec // 3600:02d}{sec % 3600 // 60:02d}{sec % 60:02d}"
                self.j.write("event", clock=clock, text=f"청산 안 됨 ({reason}) · 잔고 {held} — 10초쯤 뒤 다시 시도")
                return
        ticks = pnl_ticks(self.pos["side"], self.pos["entry"], px)
        exit_fee = getattr(self.broker, "last_fill_fee", None)
        entry_fee = self.pos.get("entry_fee")
        fee = None if entry_fee is None or exit_fee is None else entry_fee + exit_fee
        t = {**self.pos, "exit": px, "exit_time": clock, "reason": reason, "ticks": ticks,
             "pnl": ticks * TICK_VALUE, "exit_fee": exit_fee, "fee": fee,
             "pnl_after_fee": None if fee is None else ticks * TICK_VALUE - fee,
             "entry_date": self.live_date, "exit_date": self.live_date}
        self.trades.append(t)
        fee_txt = "" if fee is None else f" · 수수료 {fee:,.0f}원 → {t['pnl_after_fee']:+,.0f}원"
        self.j.write("trade", clock=clock, text=f"{reason} {ticks:+d}틱 ({t['pnl']:+,}원){fee_txt}",
                     **{k: t[k] for k in ("side", "entry", "exit", "entry_time", "exit_time", "ticks", "pnl",
                                          "fee", "pnl_after_fee")})
        self.strat.on_exit(t["pnl"], stopped=reason == "손절")
        self.pos = None

    def force_flat(self, clock: str) -> None:
        if self.pos is not None and self.last_price is not None:
            self._exit(clock, self.last_price, "실행 종료 정리")


# ---------------------------------------------------------------- 다시 돌리기
def replay(strategy_name: str, date: str, out: Path, data: Path | None = None) -> dict:
    j = Journal(out / "journal.jsonl")
    r = Runner(strategy_name, PaperBroker(j), j)
    ms = load_minutes(data) if data else load_minutes()
    r.warmup([m for m in ms if m.date < date])
    r.live_date = date
    today = [m for m in ms if m.date == date]
    for m in today:
        if not (r.p.session_start <= m.time < r.p.session_end):
            r._feed(m)
            continue
        # 그 분의 가격 범위를 한 번에 줌: 시가에서 진입·정리, 고저로 손절·익절 (백테스트와 같은 순서)
        r.on_price(m.time, m.open)
        r.on_price(m.time, m.close, ref_low=m.low, ref_high=m.high)
        r.on_minute_closed(m)
    r.force_flat("153000")
    return {"trades": r.trades, "summary": summarize(r.trades)}


# ---------------------------------------------------------------- 실제 실행
def count_stops(journal: Path, date: str) -> int:
    """기록 파일에서 그날(date) 손절로 끝난 거래 수. 파일이 없거나 못 읽으면 0."""
    n = 0
    try:
        for line in journal.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            if (rec.get("kind") == "trade" and str(rec.get("at", "")).replace("-", "")[:8] == date
                    and str(rec.get("text", "")).startswith("손절")):
                n += 1
    except (OSError, ValueError):
        return n
    return n


def live(strategy_name: str, orders: bool, exec_cnd: str, sym: str | None, out: Path) -> dict:
    from nh_client import NHClient, NHError, rows
    if datetime.now().astimezone().utcoffset().total_seconds() != 9 * 3600:
        raise SystemExit("컴퓨터 시간대가 한국(KST)이 아닙니다. 진입·정리 시각이 어긋나므로 중단합니다.")
    client = NHClient(env="demo")
    client.token()
    j = Journal(out / "journal.jsonl")
    j.write("event", text=f"서버 {client.env} · 토큰 scopes {client.scope} · 주문 {'모의계좌' if orders else '종이'}")

    if sym is None:
        fut = [f for f in rows(client.all_futures("75"))
               if str(f.get("sym_exch", "")).startswith("A75") and "플렉스" not in str(f.get("sym_nm", ""))]
        sym = min(fut, key=lambda f: f["exp_mon"])["sym_exch"]
    detail = [d for d in rows(client.product_detail()) if d.get("dtl_cd_ko_nm") == "미국달러선물(USD)"]
    if not detail or float(detail[0]["cd_val_inf_1"]) != TICK or float(detail[0]["tick_val"]) != TICK_VALUE:
        raise SystemExit(f"틱·틱가치가 전략 가정(0.1원·1,000원)과 다릅니다: {detail[:1]}")
    j.write("event", text=f"종목 {sym} · 틱 {detail[0]['cd_val_inf_1']} · 틱가치 {float(detail[0]['tick_val']):.0f}")

    broker = MockAccountBroker(client, sym, j, exec_cnd) if orders else PaperBroker(j)
    r = Runner(strategy_name, broker, j)
    today = datetime.now().strftime("%Y%m%d")

    if orders:
        held = broker.position()
        if held is None:
            raise SystemExit("시작 잔고를 확인하지 못해 중단합니다.")
        j.write("event", text=f"시작 잔고 {sym} {held:+d}계약")
        account_start = broker.account(datetime.now().strftime("%H%M%S"), "시작 (수수료·확정손익은 하루 누적)")
        if account_start:
            broker.last_fee = account_start["fee"]
        if held:
            side = "sell" if held > 0 else "buy"
            for _ in range(abs(held)):
                broker.fill(side, 0.0, datetime.now().strftime("%H%M%S"), "시작 전 남은 잔고 정리")
    else:
        account_start = None
    account_end = None

    def completed_minutes(req_qty: int) -> list[Minute]:
        now_min = datetime.now().strftime("%H%M") + "00"
        got = [to_minute(x) for x in rows(client.chart_minute(sym, dcnt=1, req_qty=req_qty))]
        return sorted([m for m in got if (m.date, m.time) < (today, now_min)], key=lambda m: (m.date, m.time))

    hist = completed_minutes(1500)
    r.warmup(hist)
    r.live_date = today          # 준비 봉에서 난 오늘 신호로는 진입하지 않음 — 이 뒤로 닫히는 봉부터
    # 같은 날 다시 켠 경우: 앞선 실행의 손절 횟수를 이어받아 「손절 2회」 한도가 지켜지게
    stops_today = count_stops(out / "journal.jsonl", today)
    if stops_today:
        r.strat.day.stops = stops_today
        j.write("event", text=f"오늘 앞선 실행의 손절 {stops_today}회를 이어받음 (한도 {r.p.max_stops_per_day}회)")
    last_fed = (hist[-1].date, hist[-1].time) if hist else ("", "")
    seen_minute = datetime.now().strftime("%H%M")

    try:
        while True:
            now = datetime.now()
            clock = now.strftime("%H%M%S")
            if clock >= END_TIME:
                break
            # 분이 바뀌고 3초 뒤: 막 끝난 1분봉(들)을 받아 전략에 넣음
            if now.strftime("%H%M") != seen_minute and now.second >= 3:
                try:
                    new = [m for m in completed_minutes(10) if (m.date, m.time) > last_fed]
                    for m in new:
                        r.on_minute_closed(m)
                        last_fed = (m.date, m.time)
                    seen_minute = now.strftime("%H%M")
                except Exception as e:      # 연결·응답 문제로 하루 실행이 멈추지 않게 — 기록하고 다음 초에 다시
                    j.write("event", clock=clock, text="차트(분) 처리 실패 — 다음 초에 다시",
                            error=f"{type(e).__name__}: {str(e)[:200]}")
            try:
                q = client.price(sym)
                px = num(q.get("last_pric")) or 0
                if px > 0:
                    r.on_price(clock, px)
            except Exception as e:
                j.write("event", clock=clock, text="현재가 처리 실패 — 다음 초에 다시",
                        error=f"{type(e).__name__}: {str(e)[:200]}")
            time.sleep(max(0.0, 1.0 - (datetime.now() - now).total_seconds()))
    except KeyboardInterrupt:
        j.write("event", text="Ctrl+C — 보유가 있으면 정리하고 끝냄")
    finally:
        r.force_flat(datetime.now().strftime("%H%M%S"))
        if orders:
            j.write("event", text=f"끝 잔고 {sym} {broker.position()}계약")
            account_end = broker.account(datetime.now().strftime("%H%M%S"), "끝 (수수료·확정손익은 하루 누적)")
    return {"sym": sym, "trades": r.trades, "summary": summarize(r.trades),
            "account_start": account_start, "account_end": account_end}


def order_test(exec_cnd: str, out: Path) -> dict:
    """모의계좌 주문 경로 점검: 시장가 매수 1계약 → 체결 확인 → 시장가 매도 1계약 → 체결 확인."""
    from nh_client import NHClient, rows
    client = NHClient(env="demo")
    client.token()
    j = Journal(out / "journal.jsonl")
    sym = min((f for f in rows(client.all_futures("75"))
               if str(f.get("sym_exch", "")).startswith("A75") and "플렉스" not in str(f.get("sym_nm", ""))),
              key=lambda f: f["exp_mon"])["sym_exch"]
    b = MockAccountBroker(client, sym, j, exec_cnd)
    j.write("event", text=f"주문 점검 시작 · {sym} · scopes {client.scope} · 잔고 {b.position()}")
    start = b.account(datetime.now().strftime("%H%M%S"), "주문 점검 시작")
    if start:
        b.last_fee = start["fee"]
    clock = lambda: datetime.now().strftime("%H%M%S")
    buy = b.fill("buy", 0.0, clock(), "주문 점검 매수")
    j.write("event", text=f"매수 뒤 잔고 {b.position()}")
    sell = b.fill("sell", 0.0, clock(), "주문 점검 매도") if b.position() else None
    j.write("event", text=f"매도 뒤 잔고 {b.position()}")
    trades = []
    if buy and sell:
        ticks = pnl_ticks("buy", buy, sell)
        trades.append({"side": "buy", "entry": buy, "exit": sell, "ticks": ticks, "pnl": ticks * TICK_VALUE,
                       "reason": "주문 점검", "exit_date": datetime.now().strftime("%Y%m%d")})
    return {"sym": sym, "trades": trades, "summary": summarize(trades),
            "order_history": scrub(rows(client.order_history()))}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", choices=["vwap", "bb"])
    ap.add_argument("--order-test", action="store_true", help="모의계좌 매수 1 → 매도 1 로 주문 경로만 점검")
    ap.add_argument("--orders", action="store_true", help="모의계좌에 실제 시장가 주문 (없으면 종이 체결)")
    ap.add_argument("--exec-cnd", default="1", choices=["1", "2", "3"], help="체결조건 1 FAS · 2 FOK · 3 FAK")
    ap.add_argument("--sym", help="종목코드 (안 주면 미국달러 최근월물)")
    ap.add_argument("--replay", metavar="YYYYMMDD", help="받아 둔 1분봉으로 그날 다시 돌리기")
    ap.add_argument("--data", type=Path, help="--replay 에 쓸 1분봉 CSV (안 주면 results/data/bars_1m_A756B000.csv)")
    args = ap.parse_args()

    if not args.order_test and not args.strategy:
        ap.error("--strategy vwap|bb 가 필요합니다 (--order-test 는 빼고)")
    stamp = args.replay or datetime.now().strftime("%Y%m%d")
    if args.order_test:
        out = HERE / "results" / "runs" / f"{stamp}_order-test_{datetime.now().strftime('%H%M%S')}"
        res = order_test(args.exec_cnd, out)
        (out / "summary.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n주문 점검 끝 → {out.relative_to(HERE)}")
        return
    mode = "replay" if args.replay else ("orders" if args.orders else "paper")
    out = HERE / "results" / "runs" / f"{stamp}_{args.strategy}_{mode}"
    if args.replay:
        res = replay(args.strategy, args.replay, out, args.data)
    else:
        res = live(args.strategy, args.orders, args.exec_cnd, args.sym, out)
    summary = out / "summary.json"
    if summary.exists():        # 같은 날 다시 켰으면 앞선 실행의 요약을 덮어쓰지 않음 (기록 파일은 이어 씀)
        summary = out / f"summary_{datetime.now().strftime('%H%M%S')}.json"
    summary.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    s = res["summary"]
    print(f"\n끝: 거래 {s['trades']} · 순손익 {s['net_pnl']:+,}원 · {s['by_reason']}  → {out.relative_to(HERE)}")


if __name__ == "__main__":
    main()
