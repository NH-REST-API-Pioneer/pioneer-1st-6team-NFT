"""6번 섹터 평일 장중 추가 검증: 실시간 데이터 필드 대조·구독 속도·20건 상한·header.action 누락.

NH 11/04 경고(잦은 재접속 IP 차단)에 따라 국내 키 1회·해외 키 1회(총 2회)만 접속한다.
연결을 끊을 수 있는 header.action 누락 시험은 국내 연결의 맨 마지막에 둔다.

국내 키 연결 (1회):
  1. KA·KB 구독 (A756C000, A016C000) → 최대 15초 실제 실시간 데이터 수신 → 명세 응답 표와 body 키 1:1 대조 → 해제
  2. 속도 제한: 구독·해제 6번 연달아(1초 내) 전송 → 응답 확인
  3. 20건 상한: 서로 다른 종목 21개 KA 구독 → 21번째 EST06 / RT_FAIL 확인 → 등록 건 전부 해제
  4. 마지막: header.action 누락 (연결 끊길 수 있음)

해외 키 연결 (1회):
  1. FA·FB 구독 (6AZ26, ESZ26) → 최대 20초 실제 실시간 데이터 수신 → 명세 응답 표와 body 키 1:1 대조 → 해제

토큰·웹소켓 키·계좌번호·사용자 ID 는 로그에 남기지 않는다.
사용법: /opt/anaconda3/bin/python -I check_realtime_weekday.py
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
REPO = next((p for p in HERE.parents if (p / "week02" / "nh_client.py").exists()), None) \
    or next(p / "pioneer-1st-6team-NFT" for p in HERE.parents if (p / "pioneer-1st-6team-NFT").exists())
SPEC = json.loads((HERE.parent / "0-명세대조" / "spec" / "spec_web.json").read_text(encoding="utf-8"))
CODE_TO_NAME = {"KA": "국내_시세체결", "KB": "국내_시세호가", "FA": "해외_시세체결", "FB": "해외_시세호가"}


def load_client(rel):
    spec = importlib.util.spec_from_file_location(rel.replace("/", "_")[:-3], REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.NHClient()


def sub(action, code, sym=None):
    body = {"action_code": code}
    if sym is not None:
        body["action_sym"] = sym
    return {"header": {"api_ver": "1", "action": action}, "body": body}


def compare_body_keys(code, body_dict):
    api_name = CODE_TO_NAME[code]
    spec_keys = {f["key"] for f in SPEC[api_name]["response"] if not f["parent"]}
    got = set(body_dict)
    nonstr = sorted(k for k, v in body_dict.items() if not isinstance(v, str))
    return {
        "spec_count": len(spec_keys),
        "got_count": len(got),
        "응답에만": sorted(got - spec_keys),
        "명세에만": sorted(spec_keys - got),
        "문자열 아닌 값": nonstr,
    }


class Conn:
    def __init__(self, client, label):
        assert client.env == "demo", "모의 서버에서만"
        self.label, self.c, self.alive = label, client, True
        self.token = client.token()
        key = requests.get(client.base + "/permission/v1/web-socket-key",
                           headers={"Authorization": f"Bearer {self.token}"}, timeout=10).json()["access_key"]
        self.ws = websocket.create_connection(
            client.base.replace("https://", "wss://") + "/trade/ws-stream?access_key=" + key, timeout=10
        )
        self.log = []
        self.send("session_init", {"header": {"api_ver": "1", "action": "0"},
                                   "body": {"action_code": "session_init", "session_token": self.token}}, wait=3.0)

    def send(self, case, msg, wait=2.0, quiet=False):
        if not self.alive:
            return []
        text = json.dumps(msg, ensure_ascii=False)
        self.ws.send(text)
        got = self.recv_control(wait)
        row = {"conn": self.label, "case": case, "sent": text.replace(self.token, "<TOKEN>"),
               "replies": got, "alive_after": self.alive}
        self.log.append(row)
        if not quiet:
            print(f"[{self.label}] {case:42s} → {json.dumps(got, ensure_ascii=False)[:200]}{'' if self.alive else '  ※ 연결 끊김'}")
        return got

    def recv_control(self, wait):
        """구독/해제 응답(header 없는 메시지) 또는 오류를 기다린다."""
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
            if "header" in j:
                continue  # 시세 푸시는 제어 응답 대기 중 무시
            got.append(j)
            # 정상 RT_INIT/RT_FAIL/INIT 이면 즉시 반환해 속도 확보
            if j.get("code") in ("INIT", "RT_INIT", "RT_FAIL"):
                break
        return got or ["(응답 없음)"]

    def collect_data(self, target_codes, seconds):
        """실제 실시간 푸시(header+body)를 수집한다."""
        samples = {c: [] for c in target_codes}
        end = time.time() + seconds
        while time.time() < end and self.alive:
            if all(len(v) >= 2 for v in samples.values()):
                break
            self.ws.settimeout(max(0.2, end - time.time()))
            try:
                t = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                break
            except Exception as e:
                self.log.append({"conn": self.label, "case": "collect_err", "error": repr(e)})
                self.alive = False
                break
            if not t:
                continue
            try:
                j = json.loads(t)
            except ValueError:
                continue
            if j.get("code") == "pingpong":
                self.ws.send(t)
                continue
            hdr = j.get("header")
            if isinstance(hdr, dict) and isinstance(j.get("body"), dict):
                ac = hdr.get("action_code")
                if ac in samples and len(samples[ac]) < 3:
                    samples[ac].append({"header": hdr, "body": j["body"]})
        return samples

    def close(self):
        if self.alive:
            try:
                self.ws.close()
            except Exception:
                pass


result = {"run_at": datetime.now().isoformat(timespec="seconds"), "domestic": {}, "overseas": {}}

# ---------- 1. 국내 키 (연결 1회) ----------
dom = load_client("week02/nh_client.py")
H = {"Authorization": f"Bearer {dom.token()}", "Content-Type": "application/json"}
fut75 = requests.post(dom.base + "/trade/v1/domestic/price-all-futures", headers=H, json={"prd": "75"}, timeout=30).json()["Occurs1"]
time.sleep(0.25)
fut01 = requests.post(dom.base + "/trade/v1/domestic/price-all-futures", headers=H, json={"prd": "01"}, timeout=30).json()["Occurs1"]
sym_usd = next(r["occurs1_sym_exch"] for r in fut75 if r["occurs1_sym_exch"].startswith("A75"))
sym_k200 = next(r["occurs1_sym_exch"] for r in fut01 if r["occurs1_sym_exch"].startswith("A01"))

# 21개 서로 다른 종목 목록 확보
syms_21 = []
for row in fut01 + fut75:
    s = row.get("occurs1_sym_exch", "")
    if s and s not in syms_21:
        syms_21.append(s)
    if len(syms_21) >= 21:
        break
print(f"국내 21종목 확보: {len(syms_21)}개 (주력 {sym_k200}, {sym_usd})")

d = Conn(dom, "국내")
# 1) KA·KB 실제 데이터 수신 + 명세 키 대조
for code in ("KA", "KB"):
    d.send(f"1 {code} 구독 {sym_k200}", sub("A", code, sym_k200))
    d.send(f"1 {code} 구독 {sym_usd}", sub("A", code, sym_usd))
dom_samples = d.collect_data(["KA", "KB"], seconds=15)
dom_field_check = {}
for code, items in dom_samples.items():
    if items:
        dom_field_check[code] = {
            "count": len(items),
            "header_sample": items[0]["header"],
            "key_diff": compare_body_keys(code, items[0]["body"]),
            "body_sample": items[0]["body"],
        }
    else:
        dom_field_check[code] = {"count": 0}
    print(f"[국내] {code} 수신 {len(items)}건 → {json.dumps(dom_field_check[code].get('key_diff', '없음'), ensure_ascii=False)}")
result["domestic"]["data_fields"] = dom_field_check
for code in ("KA", "KB"):
    d.send(f"1 {code} 해제 {sym_k200}", sub("D", code, sym_k200), wait=1.5)
    d.send(f"1 {code} 해제 {sym_usd}", sub("D", code, sym_usd), wait=1.5)

# 2) 속도 제한: 구독·해제 6번(3쌍) 1초 안에 연달아 전송
if d.alive:
    t0 = time.time()
    for _ in range(3):
        d.ws.send(json.dumps(sub("A", "KA", sym_usd)))
        d.ws.send(json.dumps(sub("D", "KA", sym_usd)))
    sent_in = round(time.time() - t0, 4)
    # 6개 제어 응답 모두 수집 (최대 4초)
    replies, end = [], time.time() + 4.0
    while len(replies) < 6 and time.time() < end and d.alive:
        d.ws.settimeout(max(0.1, end - time.time()))
        try:
            t = d.ws.recv()
        except websocket.WebSocketTimeoutException:
            break
        except Exception as e:
            replies.append(f"연결 오류 {type(e).__name__}")
            d.alive = False
            break
        try:
            j = json.loads(t)
        except ValueError:
            continue
        if j.get("code") == "pingpong":
            d.ws.send(t)
            continue
        if "header" in j:
            continue
        replies.append(j)
    result["domestic"]["rate_6_burst"] = {"sent_in_sec": sent_in, "reply_count": len(replies), "replies": replies}
    print(f"[국내] 2 속도 6연속 ({sent_in}초) → 응답 {len(replies)}개 {[r.get('code') if isinstance(r, dict) else r for r in replies]}")

# 3) 20건 상한: 21종목 KA 구독 (종목당 0.2초 간격)
if d.alive:
    sub_res = []
    for idx, s in enumerate(syms_21, 1):
        time.sleep(0.2)
        r = d.send(f"3 상한 {idx:02d} KA {s}", sub("A", "KA", s), wait=2.0, quiet=True)
        first = r[0] if r else None
        if isinstance(first, dict):
            sub_res.append({"idx": idx, "sym": s, "code": first.get("code"), "msg": first.get("msg"), "raw": first if first.get("code") != "RT_INIT" else None})
        else:
            sub_res.append({"idx": idx, "sym": s, "raw": first})
    ok_cnt = sum(1 for x in sub_res if x.get("code") == "RT_INIT")
    non_ok = [x for x in sub_res if x.get("code") != "RT_INIT"]
    result["domestic"]["limit_21"] = {"attempted": len(sub_res), "rt_init_count": ok_cnt, "non_ok": non_ok}
    print(f"[국내] 3 21종목 구독 → RT_INIT {ok_cnt}개, 나머지 {json.dumps(non_ok, ensure_ascii=False)}")
    # 전부 해제
    for x in sub_res:
        if x.get("code") == "RT_INIT" and d.alive:
            time.sleep(0.15)
            d.send(f"3 해제 {x['sym']}", sub("D", "KA", x["sym"]), wait=1.0, quiet=True)

# 4) 마지막: header.action 누락 (연결 끊길 수 있음)
if d.alive:
    r = d.send("4 header.action 누락 (마지막)", {"header": {"api_ver": "1"}, "body": {"action_code": "KA", "action_sym": sym_usd}}, wait=4.0)
    result["domestic"]["missing_header_action"] = {"replies": r, "alive_after": d.alive}
d.close()
result["domestic"]["log"] = d.log

# ---------- 2. 해외 키 (연결 1회) ----------
ovs = load_client("week03/nh_client.py")
time.sleep(1.0)
o = Conn(ovs, "해외")
for sym in ("ESZ26", "6AZ26"):
    for code in ("FA", "FB"):
        o.send(f"1 {code} 구독 {sym}", sub("A", code, sym))
ovs_samples = o.collect_data(["FA", "FB"], seconds=20)
ovs_field_check = {}
for code, items in ovs_samples.items():
    if items:
        ovs_field_check[code] = {
            "count": len(items),
            "header_sample": items[0]["header"],
            "key_diff": compare_body_keys(code, items[0]["body"]),
            "body_sample": items[0]["body"],
        }
    else:
        ovs_field_check[code] = {"count": 0}
    print(f"[해외] {code} 수신 {len(items)}건 → {json.dumps(ovs_field_check[code].get('key_diff', '없음'), ensure_ascii=False)}")
result["overseas"]["data_fields"] = ovs_field_check
for sym in ("ESZ26", "6AZ26"):
    for code in ("FA", "FB"):
        o.send(f"1 {code} 해제 {sym}", sub("D", code, sym), wait=1.5)
o.close()
result["overseas"]["log"] = o.log

out = HERE / "logs" / f"realtime_weekday_{datetime.now():%Y%m%d_%H%M%S}.json"
out.parent.mkdir(exist_ok=True)
out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
print("\n저장:", out)
