"""A3 확인: 실시간 시트(KA)의 Request JSON 예시를 그대로 보냈을 때와 header/body 구조로 보냈을 때 비교.

모의 서버만 쓴다. 토큰·웹소켓 키는 로그에 남기지 않는다.
사용법: python3 -I check_ws_subscribe.py
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import websocket  # websocket-client

HERE = Path(__file__).resolve().parent
# 팀 저장소 안(error_reports/<섹터>/)에서도, 로컬 작업 폴더(오류제보/_tools)에서도 돌게 한다
REPO = next((p for p in HERE.parents if (p / "week02" / "nh_client.py").exists()), None) \
    or next(p / "pioneer-1st-6team-NFT" for p in HERE.parents if (p / "pioneer-1st-6team-NFT").exists())
sys.path.insert(0, str(REPO / "week02"))
from nh_client import NHClient  # noqa: E402

c = NHClient()
token = c.token()
fut = c.post("/trade/v1/domestic/price-all-futures", {"prd": "75"})
sym = next(r["occurs1_sym_exch"] for r in fut["Occurs1"] if r["occurs1_sym_exch"].startswith("A75"))

key_res = c.session.get(f"{c.base}/permission/v1/web-socket-key",
                        headers={"Authorization": f"Bearer {token}"}, timeout=10).json()
access_key = key_res.get("access_key")
if not access_key:
    raise SystemExit(f"웹소켓 키 발급 실패: { {k: v for k, v in key_res.items() if 'key' not in k} }")

ws_url = c.base.replace("https://", "wss://") + "/trade/ws-stream?access_key=" + access_key
ws = websocket.create_connection(ws_url, timeout=10)
log = []


def send(label, msg):
    text = json.dumps(msg, ensure_ascii=False)
    ws.send(text)
    shown = text.replace(token, "<TOKEN>")
    log.append({"t": datetime.now().isoformat(timespec="seconds"), "dir": "보냄", "case": label, "msg": shown})
    print(f">> [{label}] {shown}")


def recv_for(label, seconds):
    end = time.time() + seconds
    while time.time() < end:
        ws.settimeout(max(0.1, end - time.time()))
        try:
            text = ws.recv()
        except websocket.WebSocketTimeoutException:
            break
        except Exception as e:  # 연결이 끊긴 경우도 기록
            log.append({"dir": "오류", "case": label, "msg": repr(e)})
            print(f"<< [{label}] 연결 오류 {e!r}")
            return False
        try:
            body = json.loads(text)
        except ValueError:
            body = None
        if isinstance(body, dict) and body.get("code") == "pingpong":
            ws.send(text)
            continue
        log.append({"t": datetime.now().isoformat(timespec="seconds"), "dir": "받음", "case": label, "msg": text[:500]})
        print(f"<< [{label}] {text[:300]}")
    return True


send("session_init", {"header": {"api_ver": "1", "action": "0"},
                      "body": {"action_code": "session_init", "session_token": token}})
recv_for("session_init", 3)

# 1) 엑셀 KA 시트 Request JSON 그대로 (종목만 현재 월물로 바꿈)
excel = {"api_ver": "1", "action_code": "KA", "action_sym": sym, "header": None, "action": None, "body": None}
send("엑셀 예시 그대로", excel)
alive = recv_for("엑셀 예시 그대로", 4)

# 2) INPUT 표대로 header/body 구조
if alive:
    send("header/body 구조", {"header": {"api_ver": "1", "action": "A"}, "body": {"action_code": "KA", "action_sym": sym}})
    alive = recv_for("header/body 구조", 4)
if alive:
    send("해제", {"header": {"api_ver": "1", "action": "D"}, "body": {"action_code": "KA", "action_sym": sym}})
    recv_for("해제", 3)
ws.close()

out = HERE / "logs" / f"ws_subscribe_{datetime.now():%Y%m%d_%H%M%S}.json"
out.write_text(json.dumps({"server": c.base, "sym": sym, "log": log}, ensure_ascii=False, indent=1), encoding="utf-8")
print("저장:", out)
