"""1번 섹터: 토큰 폐기(revoke)가 무엇을 폐기하는지 확인 (국내 모의 키).

명세: 요청 본문은 appkey·appsecret 뿐이고, 요청 헤더에 Authorization(접근토큰)이 있음.
  R1 폐기 전 토큰 T0 으로 시세 조회 (대조)
  R2 Authorization: Bearer T0 + 본문 appkey·appsecret 으로 폐기
  R3 T0 으로 다시 시세 조회 → 막히는지, 어떤 오류인지
  R4 토큰 재발급 → T0 과 다른 토큰인지 (유효할 땐 같은 토큰을 돌려주므로)
  R5 Authorization 없이 본문만으로 폐기 → 되는지, T1 이 막히는지
  R6 appsecret 을 틀리게 해서 폐기 → 거절되는지, T 가 살아 있는지
  끝: 쓸 수 있는 토큰을 다시 받아 팀 클라이언트 캐시(~/.nh_futures/token_demo.json)를 새로 채움

같은 APP Key 의 토큰을 쓰는 다른 프로그램도 영향을 받으니, 돌고 있는 게 없을 때만 실행.
토큰·키 값은 로그에 남기지 않는다 (같은지 다른지만).
사용법: python3 -I check_revoke.py
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
import nh_client  # noqa: E402

BASE = nh_client.BASE_URLS["demo"]
KEY, SECRET = nh_client._load_credentials()
log, seen = [], {}


def tag(token):
    """토큰 값 대신 T0, T1 … 이름표."""
    if token not in seen:
        seen[token] = f"T{len(seen)}"
    return seen[token]


def body_of(r):
    try:
        b = r.json()
    except ValueError:
        return {"_text": r.text[:150]}
    if isinstance(b, dict) and "access_token" in b:
        b = {**b, "access_token": tag(b["access_token"])}
    return b


def step(case, r, **extra):
    row = {"case": case, "status": r.status_code, "body": body_of(r), **extra}
    log.append(row)
    s = json.dumps(row, ensure_ascii=False)
    for v in [KEY, SECRET, *seen]:
        s = s.replace(v, "<가림>")
    print(s[:260])
    time.sleep(0.5)
    return r


def issue():
    r = requests.post(BASE + "/auth-service/v1/token", timeout=30,
                      data={"grant_type": "client_credentials", "appkey": KEY, "appsecret": SECRET})
    return r, r.json().get("access_token", "")


def price(tok):
    return requests.post(BASE + "/trade/v1/domestic/price", timeout=30, json={"sym": "A0169000"},
                         headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})


def revoke(tok=None, secret=SECRET):
    h = {"Content-Type": "application/json"}
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return requests.post(BASE + "/auth-service/v1/revoke", timeout=30, headers=h,
                         json={"appkey": KEY, "appsecret": secret})


def alive(tok):
    return price(tok).status_code == 200


r, t0 = issue()
step("R0 토큰 발급", r, token=tag(t0))
step("R1 폐기 전 T0 으로 시세", price(t0))
step("R2 폐기 (Authorization T0 + 본문)", revoke(t0))
step("R3 폐기 뒤 T0 으로 시세", price(t0))
r, t1 = issue()
step("R4 재발급", r, token=tag(t1), same_as_T0=(t1 == t0))
step("R4b T1 으로 시세", price(t1))
step("R5 폐기 (Authorization 없이 본문만)", revoke(None), )
log[-1]["T1_alive_after"] = alive(t1)
print("   R5 뒤 T1 살아 있나:", log[-1]["T1_alive_after"])
time.sleep(0.5)
r, t2 = issue()
step("R5b 재발급", r, token=tag(t2), same_as_T1=(t2 == t1))
step("R6 폐기 (appsecret 틀림, Authorization T2)", revoke(t2, secret="x" * len(SECRET)))
log[-1]["T2_alive_after"] = alive(t2)
print("   R6 뒤 T2 살아 있나:", log[-1]["T2_alive_after"])

# 끝: 팀 클라이언트 캐시를 지금 쓸 수 있는 토큰으로 다시 채움
cache = nh_client.HOME_DIR / "token_demo.json"
cache.unlink(missing_ok=True)
c = nh_client.NHClient()
final = c.token()
print("끝: 캐시 다시 채움, 시세 조회", "정상" if alive(final) else "실패")

out = HERE / "logs" / f"revoke_{datetime.now():%Y%m%d_%H%M%S}.json"
text = json.dumps({"server": BASE, "log": log}, ensure_ascii=False, indent=1)
for v in [KEY, SECRET, *seen]:
    text = text.replace(v, "<가림>")
out.write_text(re.sub(r"eyJ[A-Za-z0-9_\-.]{20,}", "<JWT>", text), encoding="utf-8")
print("저장:", out)
