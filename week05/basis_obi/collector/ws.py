"""웹소켓 연결 한 개(국내 키 또는 해외 키)와 원문 저장.

원문 저장: data/jsonl/YYYYMMDD/HH.jsonl.gz — 한 줄 = {"recv_ts", "conn", "msg"} (3주차 기록과 같은 꼴).
  recv_ts 는 이 컴퓨터가 받은 시각(한국시간, ms). msg 는 NH 원문 그대로 (pingpong·구독 응답 포함).
  지금 쓰는 시간은 .jsonl 로 쓰다가 시간이 바뀌거나 끝낼 때 .gz 로 압축. 강제 종료로 남은 .jsonl 은 다음 시작 때 압축.
사건 기록: data/jsonl/YYYYMMDD/events.jsonl — 접속·끊김·재구독 (품질 로그의 끊김 구간은 여기서 셈).

끊김 처리 (3주차 실시간 기록에서 겪은 것):
  - 60초 동안 아무 메시지(pingpong 포함)도 없으면 끊긴 것으로 보고 다시 접속. 구독 직후엔 40초.
  - 다시 접속 간격 10초 → 30초 → 1분 → 2분 → 5분 → 10분 (NH 요청: 접속키 발급을 자주 반복하지 말 것).
  - 구독 응답이 RT_FAIL 이면 그 구독만 같은 연결에서 다시 (30초 → 1분 → 2분 → 5분).
  - 장중인데 시세가 data_silence_s(120초) 동안 하나도 없으면 같은 연결에서 전부 다시 구독
    (3주차 11/04~06: pingpong 은 30초마다 오는데 KRX 체결만 안 오던 구간이 진입 시간의 39%).
"""
from __future__ import annotations

import asyncio
import gzip
import json
import shutil
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
WS_URL = "wss://apidemo.futures.co.kr"      # 모의 서버만

BACKOFF_S = (10, 30, 60, 120, 300, 600)
SILENCE_S = 60
FIRST_MSG_S = 40
HEALTHY_S = 120
RESUB_S = (30, 60, 120, 300)
DATA_SILENCE_S = 120


def now_kst() -> datetime:
    return datetime.now(KST).replace(tzinfo=None)


def log(*a) -> None:
    print(f"[{now_kst():%m-%d %H:%M:%S}]", *a, flush=True)


def _gzip_into(plain: Path) -> None:
    gz = plain.with_name(plain.name + ".gz")
    with plain.open("rb") as src, gzip.open(gz, "ab") as dst:
        shutil.copyfileobj(src, dst)
    plain.unlink()


class HourlyWriter:
    def __init__(self, root: Path, now=now_kst):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.now = now
        for p in sorted(self.root.glob("*/[0-9][0-9].jsonl")):      # 강제 종료로 남은 것
            _gzip_into(p)
        self.key = self.f = self.path = None

    def write(self, ts: datetime, conn: str, raw: str) -> None:
        key = (ts.strftime("%Y%m%d"), ts.strftime("%H"))
        if key != self.key:
            self._finish()
            d = self.root / key[0]
            d.mkdir(exist_ok=True)
            self.path = d / f"{key[1]}.jsonl"
            self.f = self.path.open("a", encoding="utf-8")
            self.key = key
        self.f.write(f'{{"recv_ts":"{ts.isoformat(timespec="milliseconds")}","conn":"{conn}","msg":{raw}}}\n')
        self.f.flush()

    def event(self, conn: str, kind: str, note: str = "") -> None:
        ts = self.now()
        d = self.root / ts.strftime("%Y%m%d")
        d.mkdir(exist_ok=True)
        with (d / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": ts.isoformat(timespec="milliseconds"), "conn": conn, "kind": kind,
                                "note": note}, ensure_ascii=False) + "\n")
        log(conn, kind, note)

    def _finish(self) -> None:
        if self.f is not None:
            self.f.close()
            _gzip_into(self.path)
            self.key = self.f = self.path = None

    def close(self) -> None:
        self._finish()


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


class Stream:
    def __init__(self, name, subs, writer, *, get_key, get_token, connect=None, url_base=WS_URL,
                 silence_s=SILENCE_S, first_msg_s=FIRST_MSG_S, poll_s=1.0, resub_s=RESUB_S, healthy_s=HEALTHY_S,
                 data_silence_s=DATA_SILENCE_S, is_open=lambda: False, backoff=None, sleep=asyncio.sleep,
                 now=now_kst):
        if connect is None:
            import websockets
            connect = websockets.connect
        self.name, self.subs, self.writer = name, list(subs), writer
        self.get_key, self.get_token, self.connect, self.url_base = get_key, get_token, connect, url_base
        self.silence_s, self.first_msg_s, self.poll_s = silence_s, first_msg_s, poll_s
        self.resub_s, self.healthy_s, self.data_silence_s, self.is_open = resub_s, healthy_s, data_silence_s, is_open
        self.backoff = backoff or Backoff()
        self.sleep, self.now = sleep, now
        self.stop = asyncio.Event()
        self.counts: Counter = Counter()

    async def session(self) -> None:
        key = await asyncio.to_thread(self.get_key)
        url = f"{self.url_base}/trade/ws-stream?access_key={key}"
        async with self.connect(url, ping_interval=None, max_queue=None) as sock:
            await sock.send(json.dumps({"header": {"api_ver": "1", "action": "0"},
                                        "body": {"action_code": "session_init", "session_token": self.get_token()}}))
            raw = await asyncio.wait_for(sock.recv(), 10)
            self.writer.write(self.now(), self.name, raw)
            if json.loads(raw).get("msg") != "INIT_SUCCESS":
                raise RuntimeError(f"session_init 실패: {raw[:200]}")
            for code, sym in self.subs:
                await sock.send(_sub(code, sym))
            self.writer.event(self.name, "connect", f"구독 {len(self.subs)}개")
            opened = last_rx = last_data = time.monotonic()
            got_any = False
            resub: dict[tuple[str, str], list] = {}
            while not self.stop.is_set():
                t = time.monotonic()
                for k, r in list(resub.items()):
                    if t >= r[0]:
                        r[0] = float("inf")
                        self.writer.event(self.name, "resub", f"{k[0]} {k[1]} · {r[1]}번째 실패 뒤")
                        await sock.send(_sub(*k))
                if self.is_open() and t - last_data > self.data_silence_s:
                    self.writer.event(self.name, "resub_data", f"장중 시세 {self.data_silence_s:.0f}초 없음")
                    for code, sym in self.subs:
                        await sock.send(_sub(code, sym))
                    last_data = t
                try:
                    raw = await asyncio.wait_for(sock.recv(), self.poll_s)
                except asyncio.TimeoutError:
                    limit = self.silence_s if got_any else self.first_msg_s
                    if time.monotonic() - last_rx > limit:
                        raise ConnectionError(f"{limit}초 무응답" + ("" if got_any else " (구독 뒤 첫 메시지 없음)"))
                    continue
                last_rx = time.monotonic()
                self.writer.write(self.now(), self.name, raw)
                msg = json.loads(raw)
                if msg.get("code") == "pingpong":
                    await sock.send(raw)
                    self.counts["pingpong"] += 1
                elif msg.get("header"):
                    h = msg["header"]
                    self.counts[f"{h.get('action_code')} {h.get('action_sym')}"] += 1
                    last_data = last_rx
                else:
                    d = msg.get("data") or {}
                    k = (d.get("action_code"), d.get("action_sym"))
                    if msg.get("code") == "RT_FAIL" and k in self.subs:
                        n = resub.get(k, [0, 0])[1] + 1
                        resub[k] = [time.monotonic() + self.resub_s[min(n - 1, len(self.resub_s) - 1)], n]
                        self.writer.event(self.name, "sub_fail", raw[:200])
                    elif msg.get("code") == "RT_INIT" and k in resub:
                        del resub[k]
                        self.writer.event(self.name, "resub_ok", f"{k[0]} {k[1]}")
                    elif msg.get("code") != "RT_INIT":
                        self.writer.event(self.name, "response", raw[:200])
                    continue
                got_any = True
                if self.backoff.fails and last_rx - opened >= self.healthy_s:
                    self.backoff.ok()

    async def run(self) -> None:
        while not self.stop.is_set():
            try:
                await self.session()
            except Exception as e:
                if self.stop.is_set():
                    break
                wait = self.backoff.fail()
                self.writer.event(self.name, "disconnect",
                                  f"{type(e).__name__}: {e}"[:250] + f" · 연속 {self.backoff.fails}회 · {wait}초 뒤")
                sleeper = asyncio.ensure_future(self.sleep(wait))
                stopper = asyncio.ensure_future(self.stop.wait())
                await asyncio.wait({sleeper, stopper}, return_when=asyncio.FIRST_COMPLETED)
                for x in (sleeper, stopper):
                    x.cancel()
