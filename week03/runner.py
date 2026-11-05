"""3주차 실행기 — 후보 B(서울 개장 돌파)를 종이 체결로 돌립니다. 2주차 week02/runner.py 와 같은 방식입니다.

판정은 백테스트와 **같은 전략 객체**(strategy_orb.SessionBreakout)가 합니다. 실행기가 하는 일은
시세를 넣고, 신호대로 종이 체결하고, 손절·익절·시간 청산을 감시하고, 모두 기록하는 것입니다.
값은 설계 문서 10절 선택 값입니다 (서울 09:00만 · 범위 10분 · 대기 60분 · 여유 0틱 · 익절 2.0배 · 시간 청산 60분).

## 시세를 받는 방법 — REST 폴링 (2주차와 같음, 웹소켓 안 씀)
- 1분봉: 분이 바뀌고 3초 뒤 해외 차트(분)로 **막 끝난 1분봉**을 받아 전략에 넣습니다. 백테스트와 같은 데이터
  출처라, 실행 때 본 봉과 백테스트의 봉이 같습니다.
- 현재가: 1초마다 해외 현재가를 조회해 손절·익절·시간 청산을 봅니다.

## 체결 — 종이만
- 기본(종이): 주문을 내지 않고 현재가 ± 1틱(불리한 쪽)으로 체결된 것으로 기록합니다. 수수료는 체결마다 $2
  (11/02 모의 1계약 확인, 왕복 $4). **모의계좌 체결이 아닙니다.**
- `--replay YYYYMMDD`: 받아 둔 1분봉으로 그날을 다시 돌립니다 (서버 접속 없음). 백테스트와 숫자가 같아야 합니다.
- 모의 주문 모드는 없습니다. 연계 전략(leadlag/ws_trader.py)이 같은 해외 모의계좌·같은 MKUX26 으로 주문을 내서,
  여기서도 주문하면 포지션이 섞입니다 (설계 문서 10절, 2주차 VWAP 를 종이로만 돌린 것과 같은 이유).

사용법 (week03/ 에서):
  python3 runner.py                       # 오늘 종이 체결 (08:55 전에 켜 두면 09:00 범위부터 봄, 11:15 에 끝남)
  python3 runner.py --replay 20261102     # 지난 하루 다시 돌리기 (results/data/bars_1m_MKUX26.csv)
  python3 runner.py --replay 20261105 --data results/data/bars_1m_MKUX26_20261105.csv
"""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import strategy_orb
from backtest import SEOUL_ONLY, block_of, load_minutes, summarize
from common import (PRICE_DECIMALS, TICK, TICK_VALUE, Minute, hhmmss_of, in_trading_hours,
                    minutes_of, pnl_ticks, target_price, ticks_between)

HERE = Path(__file__).parent
EXCH = "FSGX"                    # 해외 시세·차트 거래소코드 (fetch_data.py 와 같음)
SLIP = TICK                      # 종이 체결 슬리피지 1틱 (백테스트 SLIP_TICKS 와 같음)
PAPER_FEE = 2.0                  # 체결 1번 수수료 US$ (왕복 $4 = 백테스트 FEE)
END_TIME = "111500"              # 마지막 진입 10:10 + 시간 청산 60분 = 11:10, 그 뒤에 끝냄

#: 설계 문서 10절 선택 값 (strategy_orb.Params 기본값은 아직 출발값이라 여기서 정함)
SELECTED = strategy_orb.Params(windows=SEOUL_ONLY, range_min=10, entry_min=60, buffer_ticks=0,
                               target_r=2.0, hold_min=60)


def to_minute(r: dict) -> Minute:
    return Minute(r["ref_dt"], r["exch_tm"], float(r["open_pric"]), float(r["high_pric"]),
                  float(r["low_pric"]), float(r["close_pric"]), float(r.get("cum_trd_qty") or 0))


def num(v) -> float | None:
    try:
        s = str(v).strip()
        return float(s) if s else None
    except (TypeError, ValueError):
        return None


class Journal:
    """한 줄 = 한 사건 (jsonl). 키·토큰은 넣지 않습니다 (종이라 계좌 정보도 없음)."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.f = path.open("a", encoding="utf-8")

    def write(self, kind: str, **data) -> None:
        rec = {"kind": kind, "at": datetime.now().isoformat(timespec="seconds"), **data}
        self.f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.f.flush()
        if kind in ("event", "fill", "trade") or (kind == "signal" and data.get("side")):
            msg = data.get("text") or data.get("reason") or ""
            c = str(data.get("clock", ""))
            hms = f"{c[:2]}:{c[2:4]}:{c[4:6]}" if len(c) == 6 and c.isdigit() else rec["at"][11:19]
            print(f"[{hms}] {kind:6s} {msg} "
                  f"{ {k: v for k, v in data.items() if k not in ('text', 'reason', 'clock')} }")


# ---------------------------------------------------------------- 체결 담당
class PaperBroker:
    """주문을 내지 않고 기준가 ∓ 1틱(불리한 쪽)으로 체결된 것으로 봅니다. 수수료는 체결마다 fee 달러."""
    mode = "paper"

    def __init__(self, journal: Journal, fee: float = PAPER_FEE):
        self.j, self.fee = journal, fee

    def fill(self, side: str, ref_price: float, clock: str, why: str) -> float:
        px = round(ref_price + SLIP if side == "buy" else ref_price - SLIP, PRICE_DECIMALS)
        self.j.write("fill", clock=clock, side=side, price=px, source="paper", fee=self.fee,
                     text=f"종이 체결 · {why}")
        return px


# ---------------------------------------------------------------- 실행기
class Runner:
    def __init__(self, broker: PaperBroker, journal: Journal, params: strategy_orb.Params = SELECTED):
        self.p = params
        self.strat = strategy_orb.SessionBreakout(params)
        self.broker, self.j = broker, journal
        self.pos: dict | None = None
        self.pending: dict | None = None      # 신호 봉 → 다음 가격에서 진입
        self.trades: list[dict] = []
        self.live_date: str | None = None     # 이 날의 신호부터 진입 (그 전은 준비)
        self.last_price: float | None = None

    def _feed(self, m: Minute) -> list:
        if self.pending and m.date != self.strat.date:
            self.pending = None               # 날이 바뀌면 대기 신호 버림 (백테스트와 같음)
        return self.strat.on_minute(m)

    def warmup(self, minutes: list[Minute]) -> None:
        """준비 봉: 범위는 쌓되 신호로 진입하지 않음 (늦게 켰을 때 오늘 앞부분)."""
        for m in minutes:
            self._feed(m)
        if minutes:
            self.j.write("event", text=f"준비 봉 {len(minutes)}개 ({minutes[0].date} {minutes[0].time} ~ "
                                       f"{minutes[-1].date} {minutes[-1].time})")

    def on_minute_closed(self, m: Minute) -> None:
        for s in self._feed(m):
            if s.side or "신호" in s.reason:
                self.j.write("signal", clock=hhmmss_of(minutes_of(m.time) + 1), date=m.date, bar=m.time,
                             close=m.close, side=s.side, reason=s.reason, window=s.window,
                             range_high=s.range_high, range_low=s.range_low,
                             stop_ticks=s.stop_ticks, target_ticks=s.target_ticks)
            if s.side and self.pos is None and self.pending is None and m.date == self.live_date:
                self.pending = {"side": s.side, "window": s.window, "signal": m.time,
                                "stop_level": s.range_low if s.side == "buy" else s.range_high,
                                "target_ticks": s.target_ticks, "exit_by": s.exit_by}

    def on_price(self, clock: str, price: float, ref_low: float | None = None, ref_high: float | None = None) -> None:
        """가격 한 번. ref_low/high 는 다시 돌리기에서 그 분의 가격 범위를 줄 때 씁니다.
        순서는 백테스트와 같습니다: 시간 청산·마감 정리 → 대기 신호 진입 → 손절 → 익절."""
        lo = price if ref_low is None else ref_low
        hi = price if ref_high is None else ref_high
        self.last_price = price
        if self.pos is not None:
            if clock >= self.p.flatten_at:
                return self._exit(clock, price, "마감 정리")
            if clock >= self.pos["deadline"]:
                return self._exit(clock, price, self.pos["deadline_reason"])
        tradable = in_trading_hours(clock)
        if self.pending and self.pos is None and tradable and clock < self.p.flatten_at:
            self._enter(clock, price)
        if self.pos is None or not tradable:
            return
        side, pos = self.pos["side"], self.pos
        if side == "buy" and lo <= pos["stop"]:
            self._exit(clock, min(price, pos["stop"]) if ref_low is None else pos["stop"], "손절")
        elif side == "sell" and hi >= pos["stop"]:
            self._exit(clock, max(price, pos["stop"]) if ref_high is None else pos["stop"], "손절")
        elif side == "buy" and hi >= pos["target"]:
            self._exit(clock, max(price, pos["target"]) if ref_high is None else pos["target"], "익절")
        elif side == "sell" and lo <= pos["target"]:
            self._exit(clock, min(price, pos["target"]) if ref_low is None else pos["target"], "익절")

    def _enter(self, clock: str, price: float) -> None:
        e, self.pending = self.pending, None
        px = self.broker.fill(e["side"], price, clock, f"진입 ({e['side']})")
        hold_end = hhmmss_of(minutes_of(clock) + self.p.hold_min)
        deadline, why = hold_end, "시간 청산"
        if e["exit_by"] and e["exit_by"] < hold_end:
            deadline, why = e["exit_by"], "다음 개장 전 청산"
        stop = e["stop_level"]
        self.pos = {"side": e["side"], "window": e["window"], "signal_time": e["signal"],
                    "entry_date": self.live_date, "entry_time": clock, "entry": px,
                    "stop": stop, "stop_ticks": abs(ticks_between(px, stop)),
                    "target": target_price(e["side"], px, e["target_ticks"]),
                    "deadline": deadline, "deadline_reason": why, "block": block_of(clock)}
        self.strat.on_entry(e["window"])
        self.j.write("event", clock=clock, text=f"진입 {e['side']} {px} · 손절 {stop} ({self.pos['stop_ticks']}틱) · "
                                                f"익절 {self.pos['target']} ({e['target_ticks']}틱) · 기한 {deadline}")

    def _exit(self, clock: str, ref: float, reason: str) -> None:
        side = "sell" if self.pos["side"] == "buy" else "buy"
        px = self.broker.fill(side, ref, clock, f"청산 · {reason}")
        ticks = pnl_ticks(self.pos["side"], self.pos["entry"], px)
        fee = 2 * self.broker.fee
        t = {**self.pos, "exit_date": self.live_date, "exit_time": clock, "exit": px, "reason": reason,
             "ticks": ticks, "fee": fee, "pnl": ticks * TICK_VALUE - fee}
        self.trades.append(t)
        self.j.write("trade", clock=clock, text=f"{reason} {ticks:+d}틱 · 수수료 ${fee:.0f} → ${t['pnl']:+.1f}",
                     **{k: t[k] for k in ("side", "entry", "exit", "entry_time", "exit_time", "ticks", "fee", "pnl")})
        self.strat.on_exit(t["pnl"], stopped=reason == "손절")
        self.pos = None

    def force_flat(self, clock: str) -> None:
        if self.pos is not None and self.last_price is not None:
            self._exit(clock, self.last_price, "실행 종료 정리")


# ---------------------------------------------------------------- 다시 돌리기
def replay(date: str, out: Path, data: Path | None = None) -> dict:
    j = Journal(out / "journal.jsonl")
    r = Runner(PaperBroker(j), j)
    ms = load_minutes(data, date, date) if data else load_minutes(start=date, end=date)
    r.live_date = date
    for m in ms:
        # 그 분의 가격 범위를 한 번에 줌: 시가에서 정리·진입, 고저로 손절·익절 (백테스트와 같은 순서)
        r.on_price(m.time, m.open)
        r.on_price(m.time, m.close, ref_low=m.low, ref_high=m.high)
        r.on_minute_closed(m)
    if ms:
        r.force_flat(ms[-1].time)
    return {"params": asdict(SELECTED), "trades": r.trades, "summary": summarize(r.trades)}


# ---------------------------------------------------------------- 실제 실행 (종이)
def count_stops(journal: Path, date: str) -> int:
    """기록 파일에서 그날 손절로 끝난 거래 수 (같은 날 다시 켰을 때 「손절 2회」 한도를 이어받으려고)."""
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


def live(sym: str, out: Path, until: str) -> dict:
    from nh_client import NHClient, rows
    if datetime.now().astimezone().utcoffset().total_seconds() != 9 * 3600:
        raise SystemExit("컴퓨터 시간대가 한국(KST)이 아닙니다. 개장·청산 시각이 어긋나므로 중단합니다. (TZ=Asia/Seoul)")
    client = NHClient(env="demo")
    client.token()
    j = Journal(out / "journal.jsonl")
    j.write("event", text=f"서버 {client.env} · 종목 {sym} · 종이 체결 · 값 {asdict(SELECTED)}")
    r = Runner(PaperBroker(j), j)
    today = datetime.now().strftime("%Y%m%d")

    def completed_minutes(req_qty: int) -> list[Minute]:
        now_min = datetime.now().strftime("%H%M") + "00"
        got = [to_minute(x) for x in rows(client.chart_minute(sym, EXCH, start_dt=today, end_dt=today,
                                                              req_qty=req_qty))]
        return sorted([m for m in got if m.date == today and m.time < now_min], key=lambda m: m.time)

    # 처음 분봉 받기도 NH 쪽 일시 오류(11/05 08:50 ETR029 "종목정보를 찾을 수 없습니다")에 죽지 않게 — 끝 시각까지 다시
    while True:
        try:
            hist = completed_minutes(300)
            break
        except Exception as e:
            if datetime.now().strftime("%H%M%S") >= until:
                raise
            j.write("event", text="시작 분봉 받기 실패 — 15초 뒤 다시",
                    error=f"{type(e).__name__}: {str(e)[:200]}")
            time.sleep(15)
    r.warmup(hist)
    r.live_date = today          # 준비 봉에서 난 신호로는 진입하지 않음 — 이 뒤로 닫히는 봉부터
    stops_today = count_stops(out / "journal.jsonl", today)
    if stops_today:
        r.strat.day.stops = stops_today
        j.write("event", text=f"오늘 앞선 실행의 손절 {stops_today}회를 이어받음 (한도 {r.p.max_stops_per_day}회)")
    last_fed = hist[-1].time if hist else ""
    seen_minute = datetime.now().strftime("%H%M")

    try:
        while True:
            now = datetime.now()
            clock = now.strftime("%H%M%S")
            if clock >= until and r.pos is None:
                break
            if now.strftime("%H%M") != seen_minute and now.second >= 3:
                try:
                    for m in [m for m in completed_minutes(10) if m.time > last_fed]:
                        r.on_minute_closed(m)
                        last_fed = m.time
                    seen_minute = now.strftime("%H%M")
                except Exception as e:      # 연결·응답 문제로 하루 실행이 멈추지 않게 — 기록하고 다음 초에 다시
                    j.write("event", clock=clock, text="차트(분) 처리 실패 — 다음 초에 다시",
                            error=f"{type(e).__name__}: {str(e)[:200]}")
            try:
                q = rows(client.price(sym, EXCH))
                px = num(q[0].get("last_pric")) if q else None
                if px:
                    r.on_price(clock, px)
            except Exception as e:
                j.write("event", clock=clock, text="현재가 처리 실패 — 다음 초에 다시",
                        error=f"{type(e).__name__}: {str(e)[:200]}")
            time.sleep(max(0.0, 1.0 - (datetime.now() - now).total_seconds()))
    except KeyboardInterrupt:
        j.write("event", text="Ctrl+C — 보유가 있으면 종이로 정리하고 끝냄")
    finally:
        r.force_flat(datetime.now().strftime("%H%M%S"))
    return {"sym": sym, "params": asdict(SELECTED), "trades": r.trades, "summary": summarize(r.trades)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sym", default="MKUX26", help="종목코드 (11/16 만기 전 MKUZ26 으로)")
    ap.add_argument("--until", default=END_TIME, help="HHMMSS 에 끝냄 (보유 중이면 청산 뒤)")
    ap.add_argument("--replay", metavar="YYYYMMDD", help="받아 둔 1분봉으로 그날 다시 돌리기")
    ap.add_argument("--data", type=Path, help="--replay 에 쓸 1분봉 CSV (안 주면 results/data/bars_1m_MKUX26.csv)")
    args = ap.parse_args()

    stamp = args.replay or datetime.now().strftime("%Y%m%d")
    mode = "replay" if args.replay else "paper"
    out = HERE / "results" / "runs" / f"{stamp}_orb_{mode}"
    res = replay(args.replay, out, args.data) if args.replay else live(args.sym, out, args.until)
    summary = out / "summary.json"
    if summary.exists():        # 같은 날 다시 켰으면 앞선 실행의 요약을 덮어쓰지 않음 (기록 파일은 이어 씀)
        summary = out / f"summary_{datetime.now().strftime('%H%M%S')}.json"
    summary.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    s = res["summary"]
    print(f"\n끝: 거래 {s['trades']} · 순손익 ${s['net_pnl']:+.1f} · {s['by_reason']}  → {out.relative_to(HERE)}")


if __name__ == "__main__":
    main()
