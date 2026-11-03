"""실시간(WebSocket) 접속 확인 — 해외 키로 MKU FA·FB, 국내 키로 KRX 달러선물 KA.

사용법 (week03/leadlag/ 에서):
  python3 ws_probe.py                     # 40초 동안 받기
  python3 ws_probe.py --seconds 120 --mku MKUX26 --krx A756B000

받은 메시지는 results/ws_probe/ 에 JSON Lines 로 저장합니다 (수신 시각 recv_ts 포함). 주문 없음.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime
from pathlib import Path

import websockets

from nh_client import NHClient

HERE = Path(__file__).parent
WS_URLS = {"demo": "wss://apidemo.futures.co.kr", "real": "wss://api.futures.co.kr"}


def ws_key(client: NHClient) -> str:
    """웹소켓 접근키 (GET, 접속 1회용)."""
    client.throttles["auth"].wait()
    res = client.session.get(f"{client.base}/permission/v1/web-socket-key", timeout=client.timeout,
                             headers={"Authorization": f"Bearer {client.token()}",
                                      "appkey": client.app_key, "appsecret": client.app_secret})
    body = client._json(res, "/permission/v1/web-socket-key")
    if not body.get("access_key"):
        raise SystemExit(f"웹소켓 키 발급 실패: { {k: v for k, v in body.items() if 'key' not in k} }")
    return body["access_key"]


async def run(name: str, client: NHClient, subs: list[tuple[str, str]], seconds: float, out) -> dict:
    stats = {"name": name, "init": None, "sub": {}, "data": {}}
    url = f"{WS_URLS[client.env]}/trade/ws-stream?access_key={ws_key(client)}"
    async with websockets.connect(url, ping_interval=None) as ws:
        await ws.send(json.dumps({"header": {"api_ver": "1", "action": "0"},
                                  "body": {"action_code": "session_init", "session_token": client.token()}}))
        init = json.loads(await asyncio.wait_for(ws.recv(), 10))
        stats["init"] = init.get("msg")
        print(f"[{name}] session_init → {init}")
        for code, sym in subs:
            await ws.send(json.dumps({"header": {"api_ver": "1", "action": "A"},
                                      "body": {"action_code": code, "action_sym": sym}}))
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                raw = await asyncio.wait_for(ws.recv(), max(0.1, end - time.monotonic()))
            except asyncio.TimeoutError:
                break
            msg = json.loads(raw)
            out.write(json.dumps({"recv_ts": datetime.now().isoformat(timespec="milliseconds"),
                                  "conn": name, "msg": msg}, ensure_ascii=False) + "\n")
            if msg.get("code") == "pingpong":
                await ws.send(raw)
            elif msg.get("header"):
                key = f"{msg['header'].get('action_code') or msg['header'].get('tr_cd') or '?'}"
                if key not in stats["data"]:
                    b = msg.get("body") or {}
                    print(f"[{name}] 첫 실시간 {key}: header={msg['header']} body 일부="
                          f"{ {k: b.get(k) for k in ('sym', 'last_pric', 'exec_qty', 'ask_best_pric', 'bid_best_pric', 'ko_exec_tm', 'exch_tm') if k in b} }")
                stats["data"][key] = stats["data"].get(key, 0) + 1
            else:
                print(f"[{name}] 응답: {msg}")
                stats["sub"][str(msg.get("rt_cd"))] = stats["sub"].get(str(msg.get("rt_cd")), 0) + 1
    return stats


async def main_async(args) -> None:
    out_dir = HERE / "results" / "ws_probe"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"ws_probe_{datetime.now():%Y%m%d_%H%M%S}.jsonl"
    ovs, dom = NHClient(profile="overseas"), NHClient(profile="domestic")
    with path.open("w", encoding="utf-8") as out:
        results = await asyncio.gather(
            run("해외", ovs, [("FA", args.mku), ("FB", args.mku)], args.seconds, out),
            run("국내", dom, [("KA", args.krx)], args.seconds, out),
            return_exceptions=True,
        )
    for r in results:
        print("요약:", r)
    print(f"저장: {path.relative_to(HERE)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=40)
    ap.add_argument("--mku", default="MKUX26")
    ap.add_argument("--krx", default="A756B000")
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
