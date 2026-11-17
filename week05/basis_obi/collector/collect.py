"""KRX 달러선물(국내 키)과 SGX MKU(해외 키)의 실시간 체결·호가를 같이 받아 원문으로 저장. 주문 없음.

사용법 (week05/basis_obi/ 에서):
  .venv/bin/python -m collector.collect --minutes 60     # 60분만 (시험)
  .venv/bin/python -m collector.collect                  # 오늘 COLLECT_END(15:50)까지, 끝나면 Parquet·품질 로그 만듦
  ./run.sh                                               # cron 용 (이미 돌고 있거나 수집 시간이 아니면 바로 끝)

두 연결은 같은 컴퓨터 시계로 recv_ts 를 찍으니, 두 다리 시각 차이를 비교할 수 있습니다.
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import signal
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from collector import build, ws  # noqa: E402

JSONL_DIR = ROOT / "data" / "jsonl"


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


def _at(now: datetime, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return now.replace(hour=h, minute=m, second=0, microsecond=0)


def krx_open(now: datetime | None = None) -> bool:
    now = now or ws.now_kst()
    return now.weekday() < 5 and _at(now, config.KRX_OPEN[0]) <= now <= _at(now, config.KRX_OPEN[1])


def in_window(now: datetime) -> bool:
    return now.weekday() < 5 and _at(now, config.COLLECT_START) <= now < _at(now, config.COLLECT_END)


async def amain(args) -> None:
    from nh_client import NHClient

    clients = {"dom": NHClient(profile="domestic"), "ovs": NHClient(profile="overseas")}
    for c in clients.values():
        if c.env != "demo":
            raise SystemExit("수집기는 모의 서버(demo)에서만 돌립니다.")
    writer = ws.HourlyWriter(JSONL_DIR)
    streams = [ws.Stream(name, config.SUBS[name], writer, get_key=lambda c=c: ws_key(c), get_token=c.token,
                         is_open=krx_open)
               for name, c in clients.items()]
    loop = asyncio.get_running_loop()
    stop_all = lambda: [s.stop.set() for s in streams]          # noqa: E731
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_all)
    now = ws.now_kst()
    end = now + timedelta(minutes=args.minutes) if args.minutes else _at(now, config.COLLECT_END)
    loop.call_later(max(0.0, (end - now).total_seconds()), stop_all)
    ws.log("시작", {k: v for k, v in config.SUBS.items()}, f"~ {end:%H:%M}")
    try:
        await asyncio.gather(*(s.run() for s in streams))
    finally:
        writer.close()
        for s in streams:
            ws.log("끝", s.name, dict(sorted(s.counts.items())))
    day = now.strftime("%Y%m%d")
    ws.log("Parquet·품질 로그", build.build_day(JSONL_DIR, day))
    print(build.summary(build.json.loads((build.QUALITY_DIR / f"{day}.json").read_text())), flush=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=0, help="이만큼만 (0 = 오늘 COLLECT_END 까지)")
    ap.add_argument("--cron", action="store_true", help="수집 시간(평일 08:30~15:50)이 아니면 바로 끝")
    args = ap.parse_args(argv)
    if args.cron and not in_window(ws.now_kst()):
        return 0
    (ROOT / "data").mkdir(exist_ok=True)
    lock = (ROOT / "data" / "collector.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0                                # 이미 돌고 있음
    asyncio.run(amain(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
