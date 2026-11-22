"""6번 섹터: 실시간 구독 메시지가 명세대로 처리되는지 확인 (모의 서버).

NH 11/04 경고(잦은 재접속은 IP 차단 위험)에 따라 국내 키·해외 키로 연결을 한 번씩만 한다.
연결이 끊길 수 있는 시험은 각 연결의 맨 마지막에 두고, 끊기면 다시 접속하지 않는다.

국내 키 연결
  1 KA 구독·해제 (대조)          2 KC·KD (야간) 구독       3 O1 / O1_K action_code
  4 KB action_sym 없이 (R05)      5 KB action_code 없이 (R05)
  6 구독·해제 6번 연달아 (속도 제한)   7 KA 21종목 구독 (APP Key 당 20건 상한, EST06)
  8 header 에 action 없이 (끊길 수 있음, 마지막)
해외 키 연결
  1 FA·FB 구독 (FB 는 명세 샘플이 비어 있음)   2 O1 / O1_K   3 FA 에 국내 종목   4 KA (국내 코드)

토큰·웹소켓 키는 로그에 남기지 않는다.
사용법: python3 -I check_realtime.py
"""
import importlib.util
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
import websocket  # websocket-client

HERE = Path(__file__).resolve().parent
# 팀 저장소 안(error_reports/<섹터>/)에서도, 로컬 작업 폴더(오류제보/_tools)에서도 돌게 한다
REPO = next((p for p in HERE.parents if (p / "week02" / "nh_client.py").exists()), None) \
    or next(p / "pioneer-1st-6team-NFT" for p in HERE.parents if (p / "pioneer-1st-6team-NFT").exists())
log = []


def load(rel):
    spec = importlib.util.spec_from_file_location(rel.replace("/", "_")[:-3], REPO / rel)
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m.NHClient()


class Conn:
    def __init__(self, client, label):
        self.label, self.c, self.alive = label, client, True
        self.token = client.token()
        key = requests.get(client.base + "/permission/v1/web-socket-key",
                           headers={"Authorization": f"Bearer {self.token}"}, timeout=10).json()["access_key"]
        self.ws = websocket.create_connection(client.base.replace("https://", "wss://") + "/trade/ws-stream?access_key=" + key, timeout=10)
        self.send("session_init", {"header": {"api_ver": "1", "action": "0"},
                                   "body": {"action_code": "session_init", "session_token": self.token}}, wait=3)

    def send(self, case, msg, wait=3.0, quiet=False):
        if not self.alive:
            return []
        text = json.dumps(msg, ensure_ascii=False)
        self.ws.send(text)
        got = self.recv(wait)
        # 응답에 계좌번호(action_acnt)와, action_sym 을 빼고 보내면 서버가 채워 넣는 사용자 ID 가 와서 가린다
        sent_sym = (msg.get("body") or {}).get("action_sym")
        for r in got:
            if isinstance(r, dict) and isinstance(r.get("data"), dict):
                if "action_acnt" in r["data"]:
                    r["data"]["action_acnt"] = "<계좌번호>"
                if sent_sym is None and "action_sym" in r["data"]:
                    hidden = r["data"]["action_sym"]
                    r["data"]["action_sym"] = "<사용자ID?>"
                    r["msg"] = str(r.get("msg", "")).replace(hidden, "<사용자ID?>")
        row = {"conn": self.label, "case": case, "sent": text.replace(self.token, "<TOKEN>"),
               "replies": got, "alive_after": self.alive}
        log.append(row)
        if not quiet:
            print(f"[{self.label}] {case:40s} → {json.dumps(got, ensure_ascii=False)[:220]}{'' if self.alive else '  ※ 연결 끊김'}")
        return got

    def recv(self, wait):
        got, end = [], time.time() + wait
        while time.time() < end and self.alive:
            self.ws.settimeout(max(0.1, end - time.time()))
            try:
                t = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                break
            except Exception as e:
                got.append(f"연결 오류 {type(e).__name__}")
                self.alive = False
                break
            if not t:
                continue
            try:
                j = json.loads(t)
            except ValueError:
                got.append(t[:150])
                continue
            if j.get("code") == "pingpong":
                self.ws.send(t)
                continue
            got.append(j if "header" not in j else {"실시간 데이터": j["header"].get("action_code")})
        return got or ["(응답 없음)"]

    def close(self):
        if self.alive:
            self.ws.close()


def sub(action, code, sym=None):
    body = {"action_code": code}
    if sym is not None:
        body["action_sym"] = sym
    return {"header": {"api_ver": "1", "action": action}, "body": body}


# ---------- 국내 키 ----------
dom = load("week02/nh_client.py")
H = {"Authorization": f"Bearer {dom.token()}", "Content-Type": "application/json"}
fut = requests.post(dom.base + "/trade/v1/domestic/price-all-futures", headers=H, json={"prd": "75"}, timeout=30).json()["Occurs1"]
syms = [r["occurs1_sym_exch"] for r in fut if r["occurs1_sym_exch"].startswith("A75")]
time.sleep(0.3)
syms += [r["occurs1_sym_exch"] for r in requests.post(dom.base + "/trade/v1/domestic/price-all-futures", headers=H,
                                                       json={"prd": "01"}, timeout=30).json()["Occurs1"]][:5]
S = syms[0]
d = Conn(dom, "국내")
d.send("1 KA 구독", sub("A", "KA", S)); d.send("1 KA 해제", sub("D", "KA", S))
for code in ("KC", "KD"):
    d.send(f"2 {code} 구독", sub("A", code, S)); d.send(f"2 {code} 해제", sub("D", code, S))
d.send("3 O1 구독 (sym 없이)", sub("A", "O1")); d.send("3 O1 해제", sub("D", "O1"))
d.send("3 O1_K 구독 (문서명 그대로)", sub("A", "O1_K"))
d.send("4 KB action_sym 없이", sub("A", "KB"))
d.send("5 KB action_code 없이", {"header": {"api_ver": "1", "action": "A"}, "body": {"action_sym": S}})
# 6 속도: 구독·해제 6번을 기다리지 않고 연달아 보낸 뒤 응답을 한꺼번에 받음
if d.alive:
    t0 = time.time()
    for i in range(3):
        d.ws.send(json.dumps(sub("A", "KA", S))); d.ws.send(json.dumps(sub("D", "KA", S)))
    sent_in = time.time() - t0
    replies = d.recv(4)
    codes = [r.get("code") if isinstance(r, dict) else r for r in replies]
    log.append({"conn": "국내", "case": "6 구독·해제 6번 연달아", "sent_seconds": round(sent_in, 3), "replies": replies})
    print(f"[국내] 6 구독·해제 6번 ({sent_in:.3f}초 안에 보냄) → 응답 {len(replies)}개 {codes}")
# 7 상한: 서로 다른 종목 21개 KA 구독
if d.alive:
    res = []
    for s in syms[:21]:
        r = d.send(f"7 KA 구독 {s}", sub("A", "KA", s), wait=1.0, quiet=True)
        res.append((s, r[0].get("code") if r and isinstance(r[0], dict) else r[0], (r[0].get("msg") if r and isinstance(r[0], dict) else "")))
    ok = sum(1 for _, c, _ in res if c == "RT_INIT")
    print(f"[국내] 7 KA {len(res)}종목 구독 → RT_INIT {ok}개, 나머지 {[x for x in res if x[1] != 'RT_INIT']}")
    log.append({"conn": "국내", "case": "7 요약", "subscribed": len(res), "rt_init": ok, "not_ok": [x for x in res if x[1] != "RT_INIT"]})
    for s, c, _ in res:
        if c == "RT_INIT":
            d.send(f"7 해제 {s}", sub("D", "KA", s), wait=0.5, quiet=True)
d.send("8 header 에 action 없이 (마지막)", {"header": {"api_ver": "1"}, "body": {"action_code": "KA", "action_sym": S}}, wait=4)
d.close()

# ---------- 해외 키 ----------
ovs = load("week03/nh_client.py")
time.sleep(1)
o = Conn(ovs, "해외")
for code in ("FA", "FB"):
    o.send(f"1 {code} 구독 6AZ26", sub("A", code, "6AZ26")); o.send(f"1 {code} 해제", sub("D", code, "6AZ26"))
o.send("2 O1 구독 (sym 없이)", sub("A", "O1")); o.send("2 O1 해제", sub("D", "O1"))
o.send("2 O1_K 구독", sub("A", "O1_K"))
o.send("3 FA 에 국내 종목", sub("A", "FA", S))
o.send("4 KA (국내 코드) 해외 키로", sub("A", "KA", S))
o.close()

out = HERE / "logs" / f"realtime_{datetime.now():%Y%m%d_%H%M%S}.json"
out.parent.mkdir(exist_ok=True)
out.write_text(json.dumps({"run_at": datetime.now().isoformat(timespec="seconds"), "log": log}, ensure_ascii=False, indent=1), encoding="utf-8")
print("저장:", out)
