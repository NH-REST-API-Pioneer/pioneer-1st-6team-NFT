"""1번 섹터: 인증 API 가 명세대로 동작하는지 확인 (토큰 발급·웹소켓 키 발급).

토큰 폐기(revoke)와 웹소켓 접속은 하지 않는다. 폐기하면 같은 키를 쓰는 다른 프로그램의
토큰까지 끊길 수 있고, 접속을 반복하면 NH 가 경고한 IP 차단 위험이 있어서 따로 확인받고 한다.

토큰·키·웹소켓 키 값은 로그에 남기지 않는다 (길이·모양만).
사용법: python3 -I check_auth.py
"""
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
# 팀 저장소 안(error_reports/<섹터>/)에서도, 로컬 작업 폴더(오류제보/_tools)에서도 돌게 한다
REPO = next((p for p in HERE.parents if (p / "week02" / "nh_client.py").exists()), None) \
    or next(p / "pioneer-1st-6team-NFT" for p in HERE.parents if (p / "pioneer-1st-6team-NFT").exists())
sys.path.insert(0, str(REPO / "week02"))
from nh_client import BASE_URLS, _load_credentials  # noqa: E402

BASE = BASE_URLS["demo"]
KEY, SECRET = _load_credentials()
SECRETS = [KEY, SECRET]
LOG = HERE / "logs"
LOG.mkdir(exist_ok=True)
results = []


def mask(text: str) -> str:
    for s in SECRETS:
        if s:
            text = text.replace(s, "<가림>")
    text = re.sub(r"eyJ[A-Za-z0-9_\-.]{20,}", "<JWT>", text)
    return re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<UUID>", text)


def describe(v):
    """값 대신 타입과 모양만."""
    if isinstance(v, str):
        if v.startswith("eyJ"):
            return f"str(JWT, {len(v)}자)"
        if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", v):
            return "str(UUID 36자)"
        return f"str {v!r}" if len(v) <= 40 else f"str({len(v)}자)"
    return f"{type(v).__name__} {v!r}"


def run(case, method, path, **kw):
    time.sleep(0.4)  # API 디폴트 3/초 아래로
    r = requests.request(method, BASE + path, timeout=30, **kw)
    try:
        body = r.json()
    except ValueError:
        body = {"_text": r.text[:200]}
    row = {"case": case, "method": method, "path": path, "status": r.status_code,
           "content_type": r.headers.get("Content-Type", ""),
           "body": {k: describe(v) for k, v in body.items()} if isinstance(body, dict) else describe(body)}
    results.append(row)
    print(f"{case:48s} HTTP {r.status_code}  {mask(json.dumps(row['body'], ensure_ascii=False))[:150]}")
    return r, body


FORM = {"grant_type": "client_credentials", "appkey": KEY, "appsecret": SECRET}
TOKEN = "/auth-service/v1/token"
WSKEY = "/permission/v1/web-socket-key"

# ---------- 토큰 발급 ----------
_, t1 = run("T1 정상 (form-urlencoded)", "POST", TOKEN, data=FORM)
token = t1.get("access_token", "")
SECRETS.append(token)
_, t1b = run("T1b 바로 다시 발급", "POST", TOKEN, data=FORM)
results[-1]["same_token_as_T1"] = t1b.get("access_token") == token
print("   같은 토큰이 다시 오는가:", results[-1]["same_token_as_T1"])
if "expires_at" in t1:
    results[0]["expires_at_format_ok"] = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", str(t1["expires_at"])))
    print("   expires_at 이 명세 형식(YYYY-MM-DD HH:mm:ss)인가:", results[0]["expires_at_format_ok"], "/", t1["expires_at"])
run("T2 JSON 본문으로 (명세는 form)", "POST", TOKEN, json=FORM)
run("T3 grant_type 없음", "POST", TOKEN, data={"appkey": KEY, "appsecret": SECRET})
run("T4 grant_type 틀림 (password)", "POST", TOKEN, data={**FORM, "grant_type": "password"})
run("T5 appsecret 틀림", "POST", TOKEN, data={**FORM, "appsecret": "x" * len(SECRET)})
run("T6 appkey 없음", "POST", TOKEN, data={"grant_type": "client_credentials", "appsecret": SECRET})
run("T7 GET 으로 (명세 POST)", "GET", TOKEN, params={"grant_type": "client_credentials"})

# ---------- 웹소켓 키 발급 (접속은 안 함) ----------
auth = {"Authorization": f"Bearer {token}"}
_, w1 = run("W1 정상 GET", "GET", WSKEY, headers=auth)
_, w1b = run("W1b 바로 다시 발급", "GET", WSKEY, headers=auth)
k1, k2 = w1.get("access_key"), w1b.get("access_key")
results[-1]["same_key_as_W1"] = (k1 == k2) if k1 and k2 else None
print("   웹소켓 키가 매번 새로 나오는가:", None if results[-1]["same_key_as_W1"] is None else not results[-1]["same_key_as_W1"])
run("W2 POST 로 (명세 GET)", "POST", WSKEY, headers=auth)
run("W3 Authorization 없음", "GET", WSKEY)
run("W4 Authorization 깨진 토큰", "GET", WSKEY, headers={"Authorization": "Bearer abc.def.ghi"})
run("W5 Bearer 없이 토큰만", "GET", WSKEY, headers={"Authorization": token})

# ---------- 대조군: 다른 API·다른 메서드 ----------
run("X1 시세 API 를 Authorization 없이", "POST", "/trade/v1/domestic/price", json={"sym": "A0169000"})
run("X2 시세 API 를 GET 으로 (명세 POST)", "GET", "/trade/v1/domestic/price", headers=auth)
run("X3 토큰 발급을 PUT 으로", "PUT", TOKEN, data={"grant_type": "client_credentials"})
run("X4 토큰 발급을 GET, 인자 없이", "GET", TOKEN)
run("X5 없는 경로 /auth-service/v1/nothing", "GET", "/auth-service/v1/nothing")

out = LOG / f"auth_{datetime.now():%Y%m%d_%H%M%S}.json"
out.write_text(mask(json.dumps({"server": BASE, "results": results}, ensure_ascii=False, indent=1)), encoding="utf-8")
print("저장:", out)
