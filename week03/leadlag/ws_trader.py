"""KRX→MKU 연계 실시간 실행기 (기록기 겸용). 근거 문서 6-2절: 1분봉으로는 늦어서 초 단위로 실행한다.

사용법 (week03/leadlag/ 에서):
  python3 ws_trader.py                          # 주문 끔: 기록 + 종이 매매 (화요일 기록용)
  python3 ws_trader.py --orders                 # 모의 주문 켬 (수요일부터)
  python3 ws_trader.py --start-at 084000 --until 154500 --move 0.5 --lookback 45

구독: 국내 키 KA(A756B000) · 해외 키 FA·FB(MKUX26). 끊기면 다시 접속, pingpong 은 그대로 돌려보냄.
신호: KRX 최근 lookback 초 움직임 ≥ move 원, 같은 구간 MKU 호가 중간값이 그 움직임의 rho_max 배도 안 따라왔고,
      호가 차이가 max_spread 틱 이하이면 즉시 MKU 시장가 1계약 (KRX 상승 = 원화 약세 = MKU 매도).
청산: 익절·손절(청산 쪽 호가 기준 틱) · 보유 hold 초 · 15:40 전량. 그날 손절 max_stops 번이면 진입 중단.
시각: KRX 는 exch_tm(한국시간), MKU 는 ko_exec_tm(한국시간 — 실시간 exch_tm 은 싱가포르 시간).
      구간 계산은 받은 순간의 단조 시계로 한다 (실시간 시각은 초 단위라 거칠어서).
기록: results/live/YYYYMMDD/ — raw.jsonl(받은 메시지 전부), events.csv(신호·주문·체결·청산·손익), summary.json
주문은 모의 서버에서만 나갑니다 (nh_client 가 막음).
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import gzip
import json
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import websockets

from fee_test import wait_filled
from nh_client import NHClient, rows
from ws_probe import WS_URLS, ws_key

HERE = Path(__file__).parent
TICK = 0.0001
TICK_USD = 2.5
FEE_TICKS = 2.0 / TICK_USD        # 수수료 편도 $2 (근거 문서 5절)
SILENCE_S = 60                    # 이 시간 동안 아무 메시지(pingpong 포함)도 없으면 다시 접속


@dataclass
class LiveParams:
    lookback_s: float = 45        # KRX 충격 구간 (초)
    move_won: float = 0.8         # KRX 충격 기준 (원) — 11/03 기록 재현으로 0.5 → 0.8 (근거 문서 5절)
    rho_max: float = 0.5          # MKU 반영률 상한
    max_spread_ticks: int = 2     # 이보다 호가가 벌어져 있으면 진입 안 함
    stale_s: float = 20           # 시세가 이만큼 끊겼다 오면 그 전 값은 버림 (끊기기 전 값과 비교한 가짜 충격 방지)
    tp_ticks: float | None = None
    sl_ticks: float = 5
    hold_s: float = 300           # 시간 청산 (초) — 화요일 기록 분석 뒤 확정
    cooldown_s: float = 60
    max_stops_per_day: int = 3
    entry_start: str = "090000"
    entry_end: str = "152000"
    flatten_at: str = "154000"


class Engine:
    """판단만 하는 부분. 시세를 넣으면 할 일(진입·청산)을 돌려준다 — 접속·주문 없이 시험할 수 있게."""

    def __init__(self, p: LiveParams):
        self.p = p
        self.krx: deque[tuple[float, float]] = deque()     # (단조시각, 가격)
        self.mid: deque[tuple[float, float]] = deque()     # (단조시각, MKU 중간값)
        self.bid = self.ask = None
        self.pos: dict | None = None
        self.pending = False      # 주문이 나가 있는 동안 새 판단을 막음
        self.stops = 0
        self.resume_at = 0.0

    # ---- 시세 ----
    def on_krx(self, price: float, now: float, hhmmss: str) -> dict | None:
        self._drop_if_stale(self.krx, now)
        self.krx.append((now, price))
        self._trim(self.krx, now)
        return self._entry(now, hhmmss)

    def on_quote(self, bid: float, ask: float, now: float, hhmmss: str) -> dict | None:
        if bid <= 0 or ask <= 0 or ask < bid:
            return None
        self.bid, self.ask = bid, ask
        self._drop_if_stale(self.mid, now)
        self.mid.append((now, (bid + ask) / 2))
        self._trim(self.mid, now)
        return self._exit(now, hhmmss)

    def on_timer(self, now: float, hhmmss: str) -> dict | None:
        return self._exit(now, hhmmss)

    def _drop_if_stale(self, dq: deque, now: float) -> None:
        # 비운 뒤에는 lookback 만큼 다시 쌓여야 구간 시작값이 생기므로 그동안 진입하지 않는다
        if dq and now - dq[-1][0] > self.p.stale_s:
            dq.clear()

    def _trim(self, dq: deque, now: float) -> None:
        # 구간 시작값이 필요하므로 lookback 보다 조금 더 남김
        while len(dq) > 2 and dq[1][0] < now - self.p.lookback_s - 5:
            dq.popleft()

    @staticmethod
    def _at(dq: deque, t: float) -> float | None:
        """시각 t 에 유효했던 값 (t 이전 마지막 값)."""
        val = None
        for ts, v in dq:
            if ts > t:
                break
            val = v
        return val

    # ---- 진입 ----
    def _entry(self, now: float, hhmmss: str) -> dict | None:
        p = self.p
        if self.pos or self.pending or now < self.resume_at or self.stops >= p.max_stops_per_day:
            return None
        if not (p.entry_start <= hhmmss <= p.entry_end) or self.bid is None:
            return None
        t0 = now - p.lookback_s
        k0, m0 = self._at(self.krx, t0), self._at(self.mid, t0)
        if k0 is None or m0 is None:
            return None
        dk = round(self.krx[-1][1] - k0, 2)      # 1340.8-1340.0 = 0.7999… 방지
        if abs(dk) < p.move_won:
            return None
        spread = round((self.ask - self.bid) / TICK)
        if spread > p.max_spread_ticks:
            return None
        k_dir = 1 if dk > 0 else -1
        m1 = self.mid[-1][1]
        expected = abs(dk) / (1000 * TICK / m1 ** 2)
        followed = -k_dir * (m1 - m0) / TICK
        rho = followed / expected
        if rho >= p.rho_max:
            return None
        side = -k_dir
        self.pending = True
        return {"act": "enter", "side": side, "ref": self.ask if side > 0 else self.bid,
                "dk": round(dk, 2), "expected": round(expected, 2), "followed": round(followed, 2),
                "rho": round(rho, 2), "spread": spread, "time": hhmmss}

    def filled_entry(self, side: int, price: float, now: float, hhmmss: str, ord_no: str = "") -> None:
        self.pos = {"side": side, "price": price, "t": now, "time": hhmmss, "ord_no": ord_no}
        self.pending = False

    # ---- 청산 ----
    def _exit(self, now: float, hhmmss: str) -> dict | None:
        if not self.pos or self.pending or self.bid is None:
            return None
        p, pos = self.p, self.pos
        mark = self.bid if pos["side"] > 0 else self.ask     # 지금 청산하면 받을 호가
        pnl = round(pos["side"] * (mark - pos["price"]) / TICK, 6)   # 0.0003/0.0001 = 2.9999… 방지
        reason = ""
        if p.tp_ticks is not None and pnl >= p.tp_ticks:
            reason = "익절"
        elif pnl <= -p.sl_ticks:
            reason = "손절"
        elif now - pos["t"] >= p.hold_s:
            reason = "시간"
        elif hhmmss >= p.flatten_at:
            reason = "마감"
        if not reason:
            return None
        self.pending = True
        return {"act": "exit", "side": -pos["side"], "ref": mark, "reason": reason, "time": hhmmss}

    def filled_exit(self, price: float, now: float, reason: str) -> dict:
        pos = self.pos
        gross = pos["side"] * (price - pos["price"]) / TICK
        net = gross - 2 * FEE_TICKS
        self.stops += reason == "손절"
        self.pos, self.pending = None, False
        self.resume_at = now + self.p.cooldown_s
        return {"gross_ticks": round(gross, 2), "net_ticks": round(net, 2), "net_usd": round(net * TICK_USD, 2),
                "held_s": round(now - pos["t"], 1)}


def open_raw(day: str):
    """기록 읽기 — 저장소에는 용량 때문에 raw.jsonl.gz 만 올린다."""
    d = HERE / "results" / "live" / day
    if (d / "raw.jsonl").exists():
        return (d / "raw.jsonl").open(encoding="utf-8")
    return gzip.open(d / "raw.jsonl.gz", "rt", encoding="utf-8")


class Recorder:
    FIELDS = ["ts", "kind", "side", "price", "bid", "ask", "krx", "dk", "rho", "spread", "reason",
              "ord_no", "gross_ticks", "net_ticks", "net_usd", "held_s", "note"]

    def __init__(self, day: str):
        self.dir = HERE / "results" / "live" / day
        self.dir.mkdir(parents=True, exist_ok=True)
        self.raw = (self.dir / "raw.jsonl").open("a", encoding="utf-8")
        new = not (self.dir / "events.csv").exists()
        self.ev_f = (self.dir / "events.csv").open("a", newline="", encoding="utf-8")
        self.ev = csv.DictWriter(self.ev_f, fieldnames=self.FIELDS, extrasaction="ignore")
        if new:
            self.ev.writeheader()
        self.counts: dict[str, int] = {}

    def message(self, conn: str, raw: str) -> None:
        self.raw.write(f'{{"recv_ts":"{datetime.now().isoformat(timespec="milliseconds")}","conn":"{conn}","msg":{raw}}}\n')
        self.raw.flush()          # Ctrl+C·강제 종료에도 받은 데까지는 남게

    def event(self, kind: str, **kw) -> None:
        self.counts[kind] = self.counts.get(kind, 0) + 1
        self.ev.writerow({"ts": datetime.now().isoformat(timespec="milliseconds"), "kind": kind, **kw})
        self.ev_f.flush()
        print(f"[{datetime.now():%H:%M:%S}] {kind} {kw}")

    def close(self) -> None:
        self.raw.close()
        self.ev_f.close()


class Trader:
    def __init__(self, args, p: LiveParams):
        self.args, self.p = args, p
        self.eng = Engine(p)
        self.rec = Recorder(datetime.now().strftime("%Y%m%d"))
        self.ovs = NHClient(profile="overseas")
        self.dom = NHClient(profile="domestic")
        self.trades: list[dict] = []
        self.last_krx = None
        self.stop = asyncio.Event()

    # ---- 접속 ----
    async def stream(self, name: str, client: NHClient, subs: list[tuple[str, str]], handler) -> None:
        while not self.stop.is_set():
            try:
                key = await asyncio.to_thread(ws_key, client)
                url = f"{WS_URLS[client.env]}/trade/ws-stream?access_key={key}"
                async with websockets.connect(url, ping_interval=None, max_queue=None) as ws:
                    await ws.send(json.dumps({"header": {"api_ver": "1", "action": "0"},
                                              "body": {"action_code": "session_init", "session_token": client.token()}}))
                    init = json.loads(await asyncio.wait_for(ws.recv(), 10))
                    if init.get("msg") != "INIT_SUCCESS":
                        raise RuntimeError(f"session_init 실패: {init}")
                    for code, sym in subs:
                        await ws.send(json.dumps({"header": {"api_ver": "1", "action": "A"},
                                                  "body": {"action_code": code, "action_sym": sym}}))
                    self.rec.event("접속", note=f"{name} {subs}")
                    last_rx = time.monotonic()
                    while not self.stop.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), 1.0)
                        except asyncio.TimeoutError:
                            # 인터넷이 끊기면 소켓이 닫히지 않고 조용해지기만 한다 → 무응답으로 끊김 판단
                            if time.monotonic() - last_rx > SILENCE_S:
                                raise ConnectionError(f"{SILENCE_S}초 무응답")
                            continue
                        last_rx = time.monotonic()
                        self.rec.message(name, raw)
                        msg = json.loads(raw)
                        if msg.get("code") == "pingpong":
                            await ws.send(raw)
                        elif msg.get("header"):
                            await handler(msg["header"].get("action_code"), msg.get("body") or {})
                        elif msg.get("code") not in ("RT_INIT",):
                            self.rec.event("응답", note=f"{name} {msg}")
            except Exception as e:      # 끊김·키 오류 모두 다시 접속
                if self.stop.is_set():
                    break
                self.rec.event("끊김", note=f"{name} {type(e).__name__}: {e}"[:300])
                await asyncio.sleep(3)

    async def on_dom(self, code: str, b: dict) -> None:
        if code != "KA" or b.get("tick_div") != "1" or not float(b.get("exec_qty") or 0):
            return
        price = float(b["last_pric"])
        self.last_krx = price
        act = self.eng.on_krx(price, time.monotonic(), b.get("exch_tm", "")[:6])
        if act:
            await self.act(act)

    async def on_ovs(self, code: str, b: dict) -> None:
        if code == "FB":
            try:
                bid, ask = float(b["bid_pric_1"]), float(b["ask_pric_1"])
            except (KeyError, ValueError):
                return
            act = self.eng.on_quote(bid, ask, time.monotonic(), b.get("ko_exec_tm", "")[:6])
            if act:
                await self.act(act)

    async def timer(self) -> None:
        while not self.stop.is_set():
            await asyncio.sleep(1)
            hhmmss = datetime.now().strftime("%H%M%S")
            act = self.eng.on_timer(time.monotonic(), hhmmss)
            if act:
                await self.act(act)
            if hhmmss >= self.args.until:
                # 마감 청산이 먼저 (flatten_at 이 until 보다 이름). 그래도 남아 있으면 5분 뒤 알리고 끝냄
                if self.eng.pos and hhmmss < f"{int(self.args.until[:2]):02d}{int(self.args.until[2:4]) + 5:02d}00":
                    continue
                if self.eng.pos:
                    self.rec.event("경고", note="포지션이 남은 채 종료 — 잔고를 직접 확인하세요")
                self.stop.set()

    # ---- 주문 ----
    async def act(self, a: dict) -> None:
        quote = {"bid": self.eng.bid, "ask": self.eng.ask, "krx": self.last_krx}
        kind = "진입신호" if a["act"] == "enter" else "청산신호"
        self.rec.event(kind, side=a["side"], price=a["ref"], dk=a.get("dk"), rho=a.get("rho"),
                       spread=a.get("spread"), reason=a.get("reason"), **quote)
        price, ord_no, note = a["ref"], "", "종이 체결(반대 호가)"
        if self.args.orders:
            try:
                res = await asyncio.to_thread(self.ovs.ovs_market_order, self.args.mku,
                                              "buy" if a["side"] > 0 else "sell", 1)
                ord_no = str(res.get("ord_no", ""))
                fill = await asyncio.to_thread(wait_filled, self.ovs, ord_no, 15)
                if fill:
                    price, note = float(fill["exec_pric"]), f"체결 {fill.get('exec_tm')}"
                else:
                    note = "체결 확인 못함 — 호가로 기록, 잔고 확인 필요"
            except Exception as e:
                self.rec.event("주문오류", note=f"{type(e).__name__}: {e}"[:300], **quote)
                self.eng.pending = False
                if a["act"] == "enter":
                    return
                raise SystemExit("청산 주문 실패 — 직접 확인하세요.") from e
        now = time.monotonic()
        if a["act"] == "enter":
            self.eng.filled_entry(a["side"], price, now, a["time"], ord_no)
            self.rec.event("진입", side=a["side"], price=price, ord_no=ord_no, note=note, **quote)
        else:
            r = self.eng.filled_exit(price, now, a["reason"])
            self.trades.append({**r, "reason": a["reason"]})
            self.rec.event("청산", side=a["side"], price=price, ord_no=ord_no, reason=a["reason"], note=note,
                           **r, **quote)

    async def run(self) -> None:
        if self.args.orders:
            have = [r for r in rows(self.ovs.ovs_open_interest()) if r.get("sym", "").strip() == self.args.mku
                    and float(r.get("opint_qty") or 0)]
            if have:
                raise SystemExit(f"이미 {self.args.mku} 포지션이 있어 시작하지 않습니다: {have}")
        self.rec.event("시작", note=f"주문 {'켬' if self.args.orders else '끔'} · {asdict(self.p)}")
        await asyncio.gather(
            self.stream("국내", self.dom, [("KA", self.args.krx)], self.on_dom),
            self.stream("해외", self.ovs, [("FA", self.args.mku), ("FB", self.args.mku)], self.on_ovs),
            self.timer(),
        )
        net = [t["net_usd"] for t in self.trades]
        summary = {"day": self.rec.dir.name, "orders": self.args.orders, "params": asdict(self.p),
                   "trades": len(net), "net_usd": round(sum(net), 2),
                   "win": sum(x > 0 for x in net), "events": self.rec.counts}
        (self.rec.dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        self.rec.event("종료", note=json.dumps(summary, ensure_ascii=False))
        self.rec.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--orders", action="store_true", help="모의 주문을 실제로 냅니다 (기본: 끔, 종이 매매)")
    ap.add_argument("--mku", default="MKUX26")
    ap.add_argument("--krx", default="A756B000")
    ap.add_argument("--start-at", default="", help="HHMMSS 까지 기다렸다 시작 (예: 084000)")
    ap.add_argument("--until", default="154500", help="HHMMSS 에 끝냄")
    ap.add_argument("--lookback", type=float, default=LiveParams.lookback_s)
    ap.add_argument("--move", type=float, default=LiveParams.move_won)
    ap.add_argument("--rho", type=float, default=LiveParams.rho_max)
    ap.add_argument("--hold", type=float, default=LiveParams.hold_s)
    ap.add_argument("--sl", type=float, default=LiveParams.sl_ticks)
    ap.add_argument("--tp", type=float, default=None)
    args = ap.parse_args()
    p = LiveParams(lookback_s=args.lookback, move_won=args.move, rho_max=args.rho, hold_s=args.hold,
                   sl_ticks=args.sl, tp_ticks=args.tp)
    while args.start_at and datetime.now().strftime("%H%M%S") < args.start_at:
        time.sleep(20)
    asyncio.run(Trader(args, p).run())


if __name__ == "__main__":
    main()
