"""국내 코스피200 선물 수집기 — 외국인 흐름 전략용. 주문 없음, 모의 서버만.

두 가지를 같이 모읍니다.
  1) 웹소켓 체결(KA)·호가(KB): 코스피200 선물·미니코스피200 근월물. 받는 그대로 저장.
     → data/raw/YYYYMMDD/HH.jsonl.gz (한 줄 = {"recv_ts", "msg"})
  2) REST 30초마다: 투자자 동향(trend-investor, 30초 지연 누적 스냅샷) 선물·미니·콜·풋 × 수량·금액,
     그리고 두 선물의 현재가(웹소켓이 끊겼을 때 대비).
     → data/raw/YYYYMMDD/rest.jsonl (한 줄 = {"recv_ts", "kind", "key", "body"})
  투자자 동향은 분별 과거를 API 가 주지 않아서(그날 누적 + 직전 값 스냅샷 하나) 직접 쌓아야 합니다.
  웹소켓에는 투자자별 항목이 없습니다.

웹소켓 부분은 은석 week05/ws_recorder/record.py 를 가져왔습니다 (Recorder·HourlyWriter·Backoff 그대로).
끊김 처리도 같음: 60초 무응답이면 재접속, 10초→30초→1분→2분→5분→10분 간격 (NH 요청: 키 발급 반복 금지).
국내 키(~/.nh_futures/credentials.env)를 써서 해외 기록기와 연결·구독 한도가 겹치지 않습니다.

장 시간: 평일 08:50~15:50 에만 돌고, 그 밖에 켜면 바로 끝납니다 (국내 모의는 주간장만, 09:00~15:45).

사용법 (week05/foreign_flow/ 에서):
  .venv/bin/python record.py                 # 15:50 까지 (Ctrl+C 로 중간에 끝냄)
  .venv/bin/python record.py --seconds 60    # 60초만 (장 시간 검사 안 함, 시험용)
  ./run.sh                                   # cron 용: 이미 돌고 있으면 그냥 끝남
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

# 2026-11-17 기준 근월물 (price-all-futures 로 확인, 12월물 만기 12/10). 월물이 바뀌면 여기만.
CONTRACTS = {"K200": "A016C000", "MINI": "A056C000"}
CODES = ("KA", "KB")                        # KA 체결, KB 호가
# 투자자 동향 품목: 거래소(01 코스피200, 05 미니) + 1 선물 / 2 콜 / 3 풋
INVESTOR_PRDS = ("011", "051", "012", "013")
INVESTOR_TPS = ("1", "2")                   # 1 수량, 2 금액
REST_EVERY_S = 30
WINDOW = ("08:50", "15:50")

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


# ---------- REST 조회 (투자자 동향·현재가) ----------

def in_window(t: datetime, window=WINDOW) -> bool:
    return t.weekday() < 5 and window[0] <= t.strftime("%H:%M") < window[1]


class RestWriter:
    """하루 1개 파일에 줄 단위로 덧붙임 (30초마다 10건 남짓이라 압축 안 함)."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def write(self, ts: datetime, kind: str, key: str, body) -> None:
        d = self.root / ts.strftime("%Y%m%d")
        d.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"recv_ts": ts.isoformat(timespec="milliseconds"), "kind": kind, "key": key,
                           "body": body}, ensure_ascii=False)
        with (d / "rest.jsonl").open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def rest_jobs(contracts=CONTRACTS):
    """(kind, key, 경로, 본문) 목록 — 한 바퀴에 보낼 REST 조회."""
    jobs = []
    for prd in INVESTOR_PRDS:
        for tp in INVESTOR_TPS:
            jobs.append(("investor", f"{prd}:{tp}", "/trade/v1/domestic/trend-investor",
                         lambda dt, prd=prd, tp=tp: {"opt_prd": prd, "ref_dt": dt, "inq_tp": tp}))
    for name, sym in contracts.items():
        jobs.append(("price", sym, "/trade/v1/domestic/price", lambda dt, sym=sym: {"sym": sym}))
    return jobs


async def rest_loop(post, writer: RestWriter, stop: asyncio.Event, *, every=REST_EVERY_S, now=now_kst,
                    jobs=None, counts: Counter | None = None) -> None:
    """every 초 간격(시계에 맞춤)으로 jobs 를 차례로 조회해 저장. 실패는 기록만 하고 다음 바퀴에 다시."""
    jobs = jobs or rest_jobs()
    while not stop.is_set():
        t = now()
        dt = t.strftime("%Y%m%d")
        for kind, key, path, body in jobs:
            try:
                res = await asyncio.to_thread(post, path, body(dt))
            except Exception as e:      # 조회 하나 실패해도 수집은 계속
                res = {"_error": f"{type(e).__name__}: {e}"[:300]}
                log("REST 실패", kind, key, res["_error"][:120])
            writer.write(now(), kind, key, res)
            if counts is not None:
                counts[f"rest {kind}"] += 1
        wait = every - (time.time() % every)
        try:
            await asyncio.wait_for(stop.wait(), wait)
        except asyncio.TimeoutError:
            pass


# ---------- 실행 ----------

def ws_key(client) -> str:
    """웹소켓 접근키 (GET, 접속 1회용). 국내 키로 발급."""
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
        raise SystemExit("수집기는 모의 서버(demo)에서만 돌립니다.")
    t = now_kst()
    if not args.seconds and not in_window(t):
        log("장 시간 밖이라 끝냄", WINDOW)
        return
    syms = args.syms.split(",") if args.syms else list(CONTRACTS.values())
    subs = [(code, s) for s in syms for code in CODES]
    writer = HourlyWriter(RAW_DIR)
    rec = Recorder(writer, subs, get_key=lambda: ws_key(client), get_token=client.token)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, rec.stop.set)
    if args.seconds:
        loop.call_later(args.seconds, rec.stop.set)
    else:                                           # 장 끝 시각에 스스로 끝냄
        end = t.replace(hour=int(WINDOW[1][:2]), minute=int(WINDOW[1][3:]), second=0, microsecond=0)
        loop.call_later(max(0.0, (end - t).total_seconds()), rec.stop.set)
    log("시작", ",".join(syms), f"→ {RAW_DIR.relative_to(HERE)}")
    reporter = asyncio.ensure_future(rec.report())
    rest = asyncio.ensure_future(rest_loop(client.post, RestWriter(RAW_DIR), rec.stop, counts=rec.counts))
    try:
        await rec.run()
    finally:
        rec.stop.set()
        await reporter
        await rest
        writer.close()
        log("끝", dict(sorted(rec.counts.items())))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=0, help="이만큼만 기록 (0 = 끌 때까지)")
    ap.add_argument("--syms", default="", help="월물 목록 (쉼표). 기본은 CONTRACTS 2종목")
    args = ap.parse_args(argv)
    lock = single_instance(HERE / "data" / "recorder.lock")
    if lock is None:
        return 0                                # 이미 돌고 있음 (cron 이 5분마다 불러도 하나만)
    asyncio.run(amain(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
