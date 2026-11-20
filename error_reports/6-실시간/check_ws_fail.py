"""제보 3 보강: 형식은 맞고 내용이 틀린 구독 메시지에 RT_FAIL 이 오는지 확인.

모의 서버만 쓴다. 토큰·웹소켓 키는 로그에 남기지 않는다.
사용법: python3 -I check_ws_fail.py
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

def connect():
    time.sleep(0.5)
    key_res = c.session.get(f"{c.base}/permission/v1/web-socket-key",
                            headers={"Authorization": f"Bearer {token}"}, timeout=10).json()
    access_key = key_res.get("access_key")
    if not access_key:
        raise SystemExit("웹소켓 키 발급 실패")
    return websocket.create_connection(c.base.replace("https://", "wss://") + "/trade/ws-stream?access_key=" + access_key,
                                       timeout=10)


ws = None
log = []


def send(label, msg):
    text = msg if isinstance(msg, str) else json.dumps(msg, ensure_ascii=False)
    ws.send(text)
    shown = text.replace(token, "<TOKEN>")
    log.append({"t": datetime.now().isoformat(timespec="seconds"), "dir": "보냄", "case": label, "msg": shown})
    print(f"\n>> [{label}] {shown}")


def recv_for(label, seconds):
    got = 0
    end = time.time() + seconds
    while time.time() < end:
        ws.settimeout(max(0.1, end - time.time()))
        try:
            text = ws.recv()
        except websocket.WebSocketTimeoutException:
            break
        except Exception as e:
            log.append({"dir": "오류", "case": label, "msg": repr(e)})
            print(f"<< [{label}] 연결 오류 {e!r}")
            return None
        try:
            body = json.loads(text)
        except ValueError:
            body = None
        if isinstance(body, dict) and body.get("code") == "pingpong":
            ws.send(text)
            continue
        got += 1
        log.append({"t": datetime.now().isoformat(timespec="seconds"), "dir": "받음", "case": label, "msg": text[:500]})
        print(f"<< [{label}] {text[:300]}")
    if not got:
        log.append({"t": datetime.now().isoformat(timespec="seconds"), "dir": "받음", "case": label, "msg": f"({seconds}초 동안 응답 없음)"})
        print(f"<< [{label}] ({seconds}초 동안 응답 없음)")
    return got


def sub(action, code, s):
    return {"header": {"api_ver": "1", "action": action}, "body": {"action_code": code, "action_sym": s}}


def fresh():
    """케이스마다 새 연결 + session_init. 형식 오류에 서버가 연결을 끊기 때문."""
    global ws
    if ws:
        ws.close()
    ws = connect()
    send("session_init", {"header": {"api_ver": "1", "action": "0"},
                          "body": {"action_code": "session_init", "session_token": token}})
    recv_for("session_init", 3)


CASES = [
    ("내용 오류: 없는 종목코드", sub("A", "KA", "A999X999")),
    ("내용 오류: 없는 실시간 코드", sub("A", "ZZ", sym)),
    ("내용 오류: action 값 X", sub("X", "KA", sym)),
    ("형식 오류: body 없음", {"header": {"api_ver": "1", "action": "A"}}),
    ("형식 오류: header 없음", {"body": {"action_code": "KA", "action_sym": sym}}),
    ("형식 오류: header·body 가 null", {"header": None, "body": None}),
    ("형식 오류: 엑셀 예시(납작)", {"api_ver": "1", "action_code": "KA", "action_sym": sym,
                                "header": None, "action": None, "body": None}),
    ("형식 오류: 납작(null 키 없이)", {"api_ver": "1", "action": "A", "action_code": "KA", "action_sym": sym}),
    ("형식 오류: JSON 아님", "hello"),
    ("대조군: 정상 구독 후 해제", None),
]
for label, msg in CASES:
    fresh()
    if msg is None:
        send(label, sub("A", "KA", sym)); recv_for(label, 3)
        send(label, sub("D", "KA", sym)); recv_for(label, 3)
        continue
    send(label, msg)
    recv_for(label, 5)
    # 같은 연결이 살아 있는지: 정상 구독을 한 번 더 보내 본다
    try:
        send(label + " → 이어서 정상 구독", sub("A", "KA", sym))
        if recv_for(label + " → 이어서 정상 구독", 3):
            send(label + " → 해제", sub("D", "KA", sym)); recv_for(label + " → 해제", 2)
    except Exception as e:
        log.append({"dir": "오류", "case": label, "msg": "이어서 보내기 실패 " + repr(e)})
        print("   이어서 보내기 실패:", repr(e))
ws.close()

out = HERE / "logs" / f"ws_fail_{datetime.now():%Y%m%d_%H%M%S}.json"
out.write_text(json.dumps({"server": c.base, "sym": sym, "log": log}, ensure_ascii=False, indent=1), encoding="utf-8")
print("\n저장:", out)
