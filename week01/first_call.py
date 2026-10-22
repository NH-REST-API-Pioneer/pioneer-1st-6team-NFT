"""
NH선물 REST API 첫 호출 (1주차 과제용)
  1) APP Key/Secret으로 접근토큰 발급
  2) 국내선물 현재가 조회
  3) 호출 결과를 results/ 폴더에 JSON 파일로 저장

실행 전 터미널에서 키를 환경변수로 넣어 주세요:
  export NH_APP_KEY="발급받은 APP Key"
  export NH_APP_SECRET="발급받은 Secret Key"
  python3 first_call.py

필요 패키지: pip install requests
"""
import json
import os
from datetime import datetime
from pathlib import Path

import requests

BASE_URL = "https://api.futures.co.kr"  # 실전(조회만). 모의는 https://apidemo.futures.co.kr (모의용 키 필요)
SYMBOL = "A016C000"                     # 코스피200 F 202612
ENV_NAME = "demo" if "apidemo" in BASE_URL else "real"

RESULTS_DIR = Path(__file__).parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)
RUN_AT = datetime.now()

app_key = os.environ["NH_APP_KEY"]
app_secret = os.environ["NH_APP_SECRET"]


def save_json(name: str, data: dict) -> Path:
    """results/{이름}.json 으로 저장 """
    path = RESULTS_DIR / f"{name}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"저장: {path}")
    return path


# 1) 접근토큰 발급: JSON이 아니라 form-urlencoded로 보내야 함
res = requests.post(
    f"{BASE_URL}/auth-service/v1/token",
    data={"appkey": app_key, "appsecret": app_secret, "grant_type": "client_credentials"},
    timeout=10,
)
token_data = res.json()
token = token_data.get("access_token")

# 토큰 응답 저장: 토큰 값 자체는 GitHub에 올라가면 안 되므로 가리고 저장
token_record = {k: v for k, v in token_data.items() if k != "access_token"}
token_record["access_token"] = "***masked***" if token else None
save_json("token", {
    "requested_at": RUN_AT.isoformat(timespec="seconds"),
    "environment": ENV_NAME,
    "request": {"method": "POST", "path": "/auth-service/v1/token"},
    "http_status": res.status_code,
    "response": token_record,
})

if not token:
    raise SystemExit(f"토큰 발급 실패: {token_data}")
print(f"토큰 발급 성공 (만료: {token_data.get('expires_at')})")

# 2) 국내선물 현재가 조회
headers = {
    "Content-Type": "application/json;UTF-8",
    "authorization": f"Bearer {token}",
    "appkey": app_key,
    "appsecret": app_secret,
}
body = {"sym": SYMBOL}
res = requests.post(f"{BASE_URL}/trade/v1/domestic/price", headers=headers, json=body, timeout=10)
price = res.json()

save_json("domestic_price", {
    "requested_at": datetime.now().isoformat(timespec="seconds"),
    "environment": ENV_NAME,
    "request": {"method": "POST", "path": "/trade/v1/domestic/price", "body": body},
    "http_status": res.status_code,
    "response": price,
})

print("HTTP", res.status_code)
print("종목:", price.get("sym_nm"))
print("현재가:", price.get("last_pric"))
print("전체 응답:")
print(json.dumps(price, ensure_ascii=False, indent=2))