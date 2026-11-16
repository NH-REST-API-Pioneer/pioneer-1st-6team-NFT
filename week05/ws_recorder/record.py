"""해외선물 8종목 실시간 기록기 — NH 웹소켓으로 체결(FA)·호가 5단계(FB)를 받는 그대로 저장. 주문 없음.

REST 차트는 1분봉이 가장 잘게 쪼갠 단위라, 체결 하나하나와 호가는 웹소켓으로만 받을 수 있습니다.
웹소켓도 지나간 데이터는 주지 않아서, 켜 둔 동안 들어온 것만 쌓입니다.

저장: data/raw/YYYYMMDD/HH.jsonl.gz (한국시간 기준 1시간에 1개, 한 줄 = {"recv_ts", "msg"})
  - recv_ts 는 이 컴퓨터가 받은 시각(한국시간, ms). msg 는 NH 가 보낸 원문 그대로 (pingpong·구독 응답 포함).
  - 지금 쓰는 시간은 .jsonl 로 쓰다가, 시간이 바뀌거나 끝낼 때 .gz 로 압축. 강제 종료로 남은 .jsonl 은 다음 시작 때 압축.
  - 종목별 표로 바꾸기: python convert.py  (→ data/csv/YYYYMMDD/trades_<종목>.csv.gz, quotes_<종목>.csv.gz)

사용법 (week05/ws_recorder/ 에서):
  .venv/bin/python record.py                 # 끌 때까지 계속 (Ctrl+C)
  .venv/bin/python record.py --seconds 60    # 60초만
  ./run.sh                                   # cron 용: 이미 돌고 있으면 그냥 끝남

끊김 처리 (3주차 실시간 기록에서 겪은 것 그대로):
  - 60초 동안 아무 메시지(pingpong 포함)가 없으면 끊긴 것으로 보고 다시 접속. 구독 직후엔 40초.
  - 다시 접속은 10초 → 30초 → 1분 → 2분 → 5분 → 10분 간격으로 늘림 (NH 요청: 접속키 발급을 자주 반복하지 말 것).
    기록기는 포기하지 않고 10분 간격으로 계속 시도. 메시지를 받으며 2분 넘게 버틴 연결이어야 간격을 처음으로 되돌림.
  - 구독 응답이 RT_FAIL 이면 연결은 그대로 두고 그 구독만 다시 보냄 (30초 → 1분 → 2분 → 5분).
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import gzip
import json
import shutil
import signal
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
RAW_DIR = HERE / "data" / "raw"
KST = ZoneInfo("Asia/Seoul")
WS_URL = "wss://apidemo.futures.co.kr"      # 모의 서버만

# 종목 → 기록할 월물. 2026-11-16 기준 거래가 가장 많은 월물 (CL 은 12월물이 11/20 거래 끝이라 1월물).
# 월물이 바뀌면 여기만 고치거나 --syms 로 넘기면 됩니다.
CONTRACTS = {"ES": "ESZ26", "NQ": "NQZ26", "CL": "CLF27", "GC": "GCZ26",
             "HG": "HGZ26", "6E": "6EZ26", "6J": "6JZ26", "ZN": "ZNZ26"}
CODES = ("FA", "FB")                        # FA 체결, FB 호가 5단계

BACKOFF_S = (10, 30, 60, 120, 300, 600)
SILENCE_S = 60
FIRST_MSG_S = 40
HEALTHY_S = 120
RESUB_S = (30, 60, 120, 300)
REPORT_S = 600                              # 10분마다 받은 건수 출력


def now_kst() -> datetime:
    return datetime.now(KST).replace(tzinfo=None)


def log(*a) -> None:
    print(f"[{now_kst():%m-%d %H:%M:%S}]", *a, flush=True)


# ---------- 저장 ----------

def _gzip_into(plain: Path) -> None:
    """plain 을 같은 이름의 .gz 뒤에 이어 붙이고 plain 은 지움 (gzip 은 여러 덩어리를 이어 붙여도 읽힘)."""
    gz = plain.with_name(plain.name + ".gz")
    with plain.open("rb") as src, gzip.open(gz, "ab") as dst:
        shutil.copyfileobj(src, dst)
    plain.unlink()


class HourlyWriter:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        for p in sorted(self.root.glob("*/*.jsonl")):       # 강제 종료로 남은 것
            _gzip_into(p)
        self.key: tuple[str, str] | None = None
        self.f = None
        self.path: Path | None = None

    def write(self, ts: datetime, raw: str) -> None:
        key = (ts.strftime("%Y%m%d"), ts.strftime("%H"))
        if key != self.key:
            self._finish()
            d = self.root / key[0]
            d.mkdir(exist_ok=True)
            self.path = d / f"{key[1]}.jsonl"
            self.f = self.path.open("a", encoding="utf-8")
            self.key = key
        self.f.write(f'{{"recv_ts":"{ts.isoformat(timespec="milliseconds")}","msg":{raw}}}\n')
        self.f.flush()

    def _finish(self) -> None:
        if self.f is not None:
            self.f.close()
            _gzip_into(self.path)
            self.f = self.path = self.key = None

    def close(self) -> None:
        self._finish()


# ---------- 접속 ----------

class Backoff:
    def __init__(self, steps=BACKOFF_S):
        self.steps, self.fails = steps, 0

    def ok(self) -> None:
        self.fails = 0

    def fail(self) -> float:
        self.fails += 1
        return self.steps[min(self.fails - 1, len(self.steps) - 1)]


def _sub(code: str, sym: str) -> str:
    return json.dumps({"header": {"api_ver": "1", "action": "A"}, "body": {"action_code": code, "action_sym": sym}})


class Recorder:
    def __init__(self, writer, subs, *, get_key, get_token, connect=None, url_base=WS_URL,
                 silence_s=SILENCE_S, first_msg_s=FIRST_MSG_S, poll_s=1.0, resub_s=RESUB_S,
                 healthy_s=HEALTHY_S, sleep=asyncio.sleep, backoff=None, now=now_kst):
        if connect is None:
            import websockets
            connect = websockets.connect
        self.writer, self.subs = writer, list(subs)
        self.get_key, self.get_token, self.connect, self.url_base = get_key, get_token, connect, url_base
        self.silence_s, self.first_msg_s, self.poll_s = silence_s, first_msg_s, poll_s
        self.resub_s, self.healthy_s, self.sleep, self.now = resub_s, healthy_s, sleep, now
        self.backoff = backoff or Backoff()
        self.stop = asyncio.Event()
        self.counts: Counter = Counter()
        self.got_data = False

    async def session(self) -> None:
        """접속 1번. 끊기면 예외로 끝남 (stop 이면 조용히 끝남)."""
        key = await asyncio.to_thread(self.get_key)
        url = f"{self.url_base}/trade/ws-stream?access_key={key}"
        async with self.connect(url, ping_interval=None, max_queue=None) as ws:
            await ws.send(json.dumps({"header": {"api_ver": "1", "action": "0"},
                                      "body": {"action_code": "session_init", "session_token": self.get_token()}}))
            raw = await asyncio.wait_for(ws.recv(), 10)
            self.writer.write(self.now(), raw)
            if json.loads(raw).get("msg") != "INIT_SUCCESS":
                raise RuntimeError(f"session_init 실패: {raw[:200]}")
            for code, sym in self.subs:
                await ws.send(_sub(code, sym))
            log("접속", f"구독 {len(self.subs)}개")
            opened = last_rx = time.monotonic()
            got_any = False
            resub: dict[tuple[str, str], list] = {}         # (코드, 종목) → [다음 시도 시각, 실패 횟수]
            while not self.stop.is_set():
                for k, r in list(resub.items()):
                    if time.monotonic() >= r[0]:
                        r[0] = float("inf")
                        log("재구독", *k, f"{r[1]}번째 실패 뒤")
                        await ws.send(_sub(*k))
                try:
                    raw = await asyncio.wait_for(ws.recv(), self.poll_s)
                except asyncio.TimeoutError:
                    limit = self.silence_s if got_any else self.first_msg_s
                    if time.monotonic() - last_rx > limit:
                        raise ConnectionError(f"{limit}초 무응답" + ("" if got_any else " (구독 뒤 첫 메시지 없음)"))
                    continue
                last_rx = time.monotonic()
                self.writer.write(self.now(), raw)
                msg = json.loads(raw)
                if msg.get("code") == "pingpong":
                    await ws.send(raw)
                    self.counts["pingpong"] += 1
                elif msg.get("header"):
                    h = msg["header"]
                    self.counts[f"{h.get('action_code')} {h.get('action_sym')}"] += 1
                    self.got_data = True
                else:                                       # 구독 응답 — 살아 있다는 증거로 치지 않음
                    d = msg.get("data") or {}
                    k = (d.get("action_code"), d.get("action_sym"))
                    if msg.get("code") == "RT_FAIL" and k in self.subs:
                        n = resub.get(k, [0, 0])[1] + 1
                        resub[k] = [time.monotonic() + self.resub_s[min(n - 1, len(self.resub_s) - 1)], n]
                        log("구독 실패", raw[:200])
                    elif msg.get("code") == "RT_INIT" and k in resub:
                        del resub[k]
                        log("재구독 성공", *k)
                    elif msg.get("code") != "RT_INIT":
                        log("응답", raw[:200])
                    continue
                got_any = True
                if self.backoff.fails and last_rx - opened >= self.healthy_s:
                    self.backoff.ok()

    async def report(self, every: float = REPORT_S) -> None:
        last, t0 = Counter(), time.monotonic()
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.stop.wait(), every)
            except asyncio.TimeoutError:
                pass
            diff = self.counts - last
            last, t1 = self.counts.copy(), time.monotonic()
            data = sum(v for k, v in diff.items() if k != "pingpong")
            log(f"최근 {(t1 - t0) / 60:.1f}분 시세 {data}건", dict(sorted(diff.items())))
            t0 = t1

    async def run(self, max_sessions: int | None = None) -> None:
        n = 0
        while not self.stop.is_set():
            n += 1
            try:
                await self.session()
            except Exception as e:      # 끊김·키 오류 → 간격을 늘려 가며 다시 접속
                if self.stop.is_set():
                    break
                wait = self.backoff.fail()
                log("끊김", f"{type(e).__name__}: {e}"[:300], f"· 연속 {self.backoff.fails}회 · {wait}초 뒤 다시")
                if max_sessions is not None and n >= max_sessions:
                    break
                try:
                    await asyncio.wait_for(self._sleep_or_stop(wait), wait + 1)
                except asyncio.TimeoutError:
                    pass
                continue
            if max_sessions is not None and n >= max_sessions:
                break

    async def _sleep_or_stop(self, s: float) -> None:
        sleeper = asyncio.ensure_future(self.sleep(s))
        stopper = asyncio.ensure_future(self.stop.wait())
        await asyncio.wait({sleeper, stopper}, return_when=asyncio.FIRST_COMPLETED)
        for t in (sleeper, stopper):
            t.cancel()


# ---------- 실행 ----------

def ws_key(client) -> str:
    """웹소켓 접근키 (GET, 접속 1회용)."""
    client.throttles["auth"].wait()
    res = client.session.get(f"{client.base}/permission/v1/web-socket-key", timeout=client.timeout,
                             headers={"Authorization": f"Bearer {client.token()}",
                                      "appkey": client.app_key, "appsecret": client.app_secret})
    body = client._json(res, "/permission/v1/web-socket-key")
    if not body.get("access_key"):
        raise RuntimeError(f"웹소켓 키 발급 실패: { {k: v for k, v in body.items() if 'key' not in k} }")
    return body["access_key"]


def single_instance(lock: Path):
    lock.parent.mkdir(parents=True, exist_ok=True)
    f = lock.open("w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return None
    return f


async def amain(args) -> None:
    from nh_client import NHClient

    client = NHClient()
    if client.env != "demo":
        raise SystemExit("기록기는 모의 서버(demo)에서만 돌립니다.")
    syms = args.syms.split(",") if args.syms else list(CONTRACTS.values())
    subs = [(code, s) for s in syms for code in CODES]
    writer = HourlyWriter(RAW_DIR)
    rec = Recorder(writer, subs, get_key=lambda: ws_key(client), get_token=client.token)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, rec.stop.set)
    if args.seconds:
        loop.call_later(args.seconds, rec.stop.set)
    log("시작", ",".join(syms), f"→ {RAW_DIR.relative_to(HERE)}")
    reporter = asyncio.ensure_future(rec.report())
    try:
        await rec.run()
    finally:
        rec.stop.set()
        await reporter
        writer.close()
        log("끝", dict(sorted(rec.counts.items())))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=0, help="이만큼만 기록 (0 = 끌 때까지)")
    ap.add_argument("--syms", default="", help="월물 목록 (쉼표). 기본은 CONTRACTS 8종목")
    args = ap.parse_args(argv)
    lock = single_instance(HERE / "data" / "recorder.lock")
    if lock is None:
        return 0                                # 이미 돌고 있음 (cron 이 5분마다 불러도 하나만)
    asyncio.run(amain(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
