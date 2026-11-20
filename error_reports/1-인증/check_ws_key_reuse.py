"""1번 섹터: 웹소켓 키를 두 번 쓸 수 있는지 (명세: 「웹소켓 연결 시 1회 사용」).

접속은 3번만 한다 (NH 의 잦은 재접속 경고 때문에 최소로).
  1) 키 A 로 접속 → session_init → 닫기
  2) 같은 키 A 로 다시 접속 → 되는지
  3) 대조군: 새 키 B 로 접속 → session_init → 닫기
키·토큰 값은 로그에 남기지 않는다.

사용법: python3 -I check_ws_key_reuse.py
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
WSS = c.base.replace("https://", "wss://") + "/trade/ws-stream?access_key="
log = []


def new_key():
    time.sleep(0.5)
    return c.session.get(f"{c.base}/permission/v1/web-socket-key",
                         headers={"Authorization": f"Bearer {token}"}, timeout=10).json()["access_key"]


def attempt(label, key):
    time.sleep(2)
    row = {"case": label, "t": datetime.now().isoformat(timespec="seconds")}
    try:
        ws = websocket.create_connection(WSS + key, timeout=10)
    except websocket.WebSocketBadStatusException as e:
        row.update(connected=False, status=e.status_code,
                   body=(e.resp_body.decode("utf-8", "replace")[:300] if e.resp_body else ""))
    except Exception as e:
        row.update(connected=False, error=repr(e)[:200])
    else:
        row["connected"] = True
        ws.send(json.dumps({"header": {"api_ver": "1", "action": "0"},
                            "body": {"action_code": "session_init", "session_token": token}}))
        try:
            ws.settimeout(5)
            row["init_reply"] = ws.recv()[:200]
        except Exception as e:
            row["init_reply"] = repr(e)[:200]
        ws.close()
    log.append(row)
    print(json.dumps(row, ensure_ascii=False))


key_a = new_key()
attempt("1) 키 A 첫 접속", key_a)
attempt("2) 같은 키 A 로 다시 접속", key_a)
attempt("3) 대조군: 새 키 B 로 접속", new_key())

out = HERE / "logs" / f"ws_key_reuse_{datetime.now():%Y%m%d_%H%M%S}.json"
out.parent.mkdir(exist_ok=True)
import re  # noqa: E402
text = json.dumps({"server": c.base, "log": log}, ensure_ascii=False, indent=1)
out.write_text(re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<UUID>", text), encoding="utf-8")
print("저장:", out)
