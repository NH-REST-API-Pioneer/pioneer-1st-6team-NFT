"""NH선물 Open API REST 클라이언트 — 해외선물 (3주차 것을 4주차로 복사).

2주차 nh_client.py 의 인증·호출 제한·재시도를 그대로 두고, 키 파일과 토큰 파일을 해외 모의계좌
전용으로 나눴습니다 (2주차 국내 키와 섞이지 않게). 시세·주문 경로는 개발자센터 명세의 해외선물
항목(/trade/v1/overseas/...)을 따릅니다.

인증정보는 코드·저장소 밖에서 읽습니다. 먼저 환경변수, 없으면 홈 폴더 파일입니다.
  환경변수:  NH_APP_KEY, NH_APP_SECRET
  파일:      ~/.nh_futures/credentials_overseas.env  (NH_APP_KEY=... / NH_APP_SECRET=..., # 줄은 주석)

서버는 모의(demo)가 기본입니다. 실전(real)은 NH_ENV=real 과 NH_ALLOW_REAL=1 을 둘 다 줘야 열립니다.
토큰 응답의 scopes(S:모의, R:운영)가 고른 서버와 다르면 바로 멈춥니다.
토큰은 ~/.nh_futures/token_overseas_<env>.json 에 만료 전까지 재사용합니다 (저장소 밖).
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path

import requests

BASE_URLS = {
    "demo": "https://apidemo.futures.co.kr",
    "real": "https://api.futures.co.kr",
}
# 토큰 응답 scopes 값 (token 명세: R:운영, S:모의투자)
EXPECTED_SCOPE = {"demo": "S", "real": "R"}
HOME_DIR = Path.home() / ".nh_futures"
CREDENTIALS_FILE = HOME_DIR / "credentials_overseas.env"

# 초당 요청 제한 (설명서 README 「요청 제한」). 한도보다 조금 느리게 보냅니다.
LIMIT_PER_SEC = {"auth": 3, "market": 10, "account": 10, "order": 5}


class NHError(RuntimeError):
    """HTTP 오류이거나 응답이 JSON 이 아닐 때."""

    def __init__(self, path: str, status: int, body: str):
        super().__init__(f"{path} → HTTP {status}: {body[:300]}")
        self.path, self.status, self.body = path, status, body


def _load_credentials() -> tuple[str, str]:
    key, secret = os.environ.get("NH_APP_KEY"), os.environ.get("NH_APP_SECRET")
    file = CREDENTIALS_FILE
    if (not key or not secret) and file.exists():
        values = {}
        for line in file.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                name, _, value = line.partition("=")
                values[name.strip()] = value.strip().strip('"').strip("'")
        key = key or values.get("NH_APP_KEY")
        secret = secret or values.get("NH_APP_SECRET")
    if not key or not secret:
        raise SystemExit(
            "APP Key·Secret 이 없습니다. 환경변수 NH_APP_KEY·NH_APP_SECRET 을 넣거나 "
            f"{file} 파일을 만드세요."
        )
    return key, secret


class _Throttle:
    """등급별로 초당 n 번을 넘지 않게 간격을 둡니다."""

    def __init__(self, per_sec: int):
        self.gap = 1.0 / per_sec * 1.1
        self.last = 0.0
        self.lock = threading.Lock()

    def wait(self) -> None:
        with self.lock:
            delay = self.last + self.gap - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self.last = time.monotonic()


class NHClient:
    def __init__(self, env: str | None = None, timeout: float = 30):
        self.env = env or os.environ.get("NH_ENV", "demo")
        if self.env not in BASE_URLS:
            raise ValueError(f"NH_ENV 는 demo / real 중 하나: {self.env}")
        if self.env == "real" and os.environ.get("NH_ALLOW_REAL") != "1":
            raise SystemExit("실전 서버는 막혀 있습니다. 정말 필요하면 NH_ALLOW_REAL=1 을 함께 주세요.")
        self.base = BASE_URLS[self.env]
        self.scope: str | None = None
        self.timeout = timeout
        self.app_key, self.app_secret = _load_credentials()
        self.session = requests.Session()
        self.throttles = {name: _Throttle(n) for name, n in LIMIT_PER_SEC.items()}
        self._token: str | None = None
        self._token_until = 0.0

    # ---- 인증 ----
    def _token_file(self) -> Path:
        return HOME_DIR / f"token_overseas_{self.env}.json"

    def token(self) -> str:
        """유효한 접근토큰. 만료 10분 전이면 새로 받습니다."""
        if self._token and time.time() < self._token_until - 600:
            return self._token
        cached = self._token_file()
        if cached.exists():
            data = json.loads(cached.read_text(encoding="utf-8"))
            if data.get("app_key_tail") == self.app_key[-4:] and time.time() < data["until"] - 600:
                self._check_scope(data.get("scopes"))
                self._token, self._token_until = data["access_token"], data["until"]
                return self._token

        self.throttles["auth"].wait()
        # 토큰 발급만 form-urlencoded 입니다 (data=)
        try:
            res = self.session.post(
                f"{self.base}/auth-service/v1/token",
                data={"grant_type": "client_credentials", "appkey": self.app_key, "appsecret": self.app_secret},
                timeout=self.timeout,
            )
        except requests.RequestException as e:      # 연결 끊김·시간 초과도 NHError 로 — 호출한 쪽이 한 가지만 잡게
            raise NHError("/auth-service/v1/token", 0, f"{type(e).__name__}: {e}") from None
        body = self._json(res, "/auth-service/v1/token")
        if not body.get("access_token"):
            safe = {k: v for k, v in body.items() if "token" not in k}
            raise NHError("/auth-service/v1/token", res.status_code, json.dumps(safe, ensure_ascii=False))
        self._check_scope(body.get("scopes"))
        self._token = body["access_token"]
        self._token_until = time.time() + int(body.get("expires_in") or 86400)

        HOME_DIR.mkdir(mode=0o700, exist_ok=True)
        cached.write_text(json.dumps({
            "access_token": self._token,
            "until": self._token_until,
            "app_key_tail": self.app_key[-4:],
            "scopes": body.get("scopes"),
            "issued_at": datetime.now().isoformat(timespec="seconds"),
        }), encoding="utf-8")
        cached.chmod(0o600)
        return self._token

    def _check_scope(self, scopes: str | None) -> None:
        """키가 고른 서버와 같은 환경인지 확인합니다. 다르면 아무것도 호출하지 않고 멈춥니다."""
        expected = EXPECTED_SCOPE[self.env]
        if scopes is None:
            print(f"[경고] 토큰 응답에 scopes 가 없어 모의/실전 키를 확인하지 못했습니다 (서버: {self.env}).")
            return
        if str(scopes).strip().upper() != expected:
            raise SystemExit(f"키 환경이 서버와 다릅니다: 서버 {self.env}(기대 {expected}) · 토큰 scopes {scopes}. 중단합니다.")
        self.scope = str(scopes).strip().upper()

    # ---- 공통 호출 ----
    def post(self, path: str, body: dict, kind: str = "market", retries: int = 3) -> dict:
        """JSON 본문 POST. 호출 제한 초과(429·EGW020·EGW036)면 간격을 두고 다시 보냅니다.

        연결 끊김·시간 초과(requests 예외)도 NHError 로 바꿔 올립니다 — 실행기가 한 종류만 잡으면 되게.
        주문(retries=1)은 시간 초과여도 다시 보내지 않습니다 (서버가 이미 받았을 수 있음).
        """
        res = None
        for attempt in range(retries):
            self.throttles[kind].wait()
            try:
                res = self.session.post(
                    f"{self.base}{path}",
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.token()}",
                        # Postman 은 안 보내지만 week01 first_call.py 가 보내서 성공한 헤더
                        "appkey": self.app_key,
                        "appsecret": self.app_secret,
                    },
                    json=body,
                    timeout=self.timeout,
                )
            except requests.RequestException as e:
                if attempt + 1 < retries:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                raise NHError(path, 0, f"{type(e).__name__}: {e}") from None
            text = res.text
            if res.status_code == 429 or "EGW020" in text or "EGW036" in text:
                time.sleep(1.0 * (attempt + 1))
                continue
            # 4주차: 시세 받다가 「유효하지 않은 토큰입니다」(코드 없이 400) 가 와서 같이 처리 (2026-11-08)
            if res.status_code == 401 or "EGW018" in text or "EGW052" in text or "유효하지 않은 토큰" in text:
                self._token, self._token_until = None, 0.0  # 만료 → 다시 발급
                self._token_file().unlink(missing_ok=True)
                continue
            return self._json(res, path)
        raise NHError(path, res.status_code if res is not None else 0, res.text if res is not None else "")

    @staticmethod
    def _json(res: requests.Response, path: str) -> dict:
        try:
            body = res.json()
        except ValueError:
            raise NHError(path, res.status_code, res.text) from None
        if res.status_code >= 400:
            raise NHError(path, res.status_code, json.dumps(body, ensure_ascii=False))
        return body

    # ---- 해외 시세 ----
    def price(self, sym: str, exch_cd: str) -> dict:
        """해외 현재가. 요청이 배열이지만 서버는 한 번에 한 종목만 받습니다."""
        return self.post("/trade/v1/overseas/price",
                         {"Iccurs1": [{"iccurs1_exch_cd": exch_cd, "iccurs1_sym": sym}]})

    def chart_minute(self, sym: str, exch_cd: str, *, start_dt: str, end_dt: str, dcnt: int = 1,
                     req_qty: int = 300, nxt_key: str = "99999999",
                     start_tm: str = "000000", end_tm: str = "235959") -> dict:
        """해외 차트(분). 줄이 최신부터 오고, nxt_key 로 과거로 이어 받습니다.

        start_dt 는 「이 날짜까지 거슬러 가라」는 아래끝입니다 (NH 앱 overseas_history.py 의 관찰).
        해외에는 연결선물이 없어 cidx_yn 은 늘 0 입니다.
        """
        return self.post("/trade/v1/overseas/chart-minute", {
            "exch_cd": exch_cd, "sym": sym, "inq_strt_dt": start_dt, "strt_tm": start_tm,
            "inq_ed_dt": end_dt, "ed_tm": end_tm, "req_qty": str(req_qty), "dcnt": str(dcnt),
            "cidx_yn": "0", "nxt_key": nxt_key,
        })

    def product_expiry(self, sym: str, rem_dy: str = "999") -> dict:
        """해외 만기일조회. 종목마다 최초통보일·만기일·최종결제일·잔존일수."""
        return self.post("/trade/v1/overseas/product-expiry",
                         {"fut_opt_div_cd": "", "nxt_key": "", "rem_dy": rem_dy, "sym": sym}, kind="account")

    def product_detail(self, prd_cd: str, exch_cd: str = "SGX") -> dict:
        """품목별세부정보 (계약단위·틱사이즈·틱값). 품목코드(MKU) 단위입니다."""
        return self.post("/trade/v1/overseas/product-detail", {
            "cur_cd": "", "exch_cd": exch_cd, "fut_opt_div_cd": "", "nxt_key": "", "prd_cd": prd_cd,
            "prd_grp_cd": "", "prd_nm": "", "sett_cd": "",
        }, kind="account")

    def market_operations(self, prd_cd: str, exch_cd: str = "SGX") -> dict:
        """장운영정보 — 품목별 거래시간 (한국 시간)."""
        return self.post("/trade/v1/overseas/market-operations", {
            "cur_cd": "", "exch_cd": exch_cd, "fut_opt_div_cd": "", "nxt_key": "", "prd_cd": prd_cd,
            "prd_grp_cd": "", "prd_nm": "",
        }, kind="account")


    # ---- 해외 계좌·주문 (3주차 leadlag/nh_client.py 에서 옮김) — 주문은 모의 서버에서만 ----
    def ovs_deposit(self, cur_cd: str = "USD", biz_dt: str = "") -> dict:
        """해외 예수금. biz_dt 가 비면 ETR018 이 나서 오늘 날짜를 넣습니다 (3주차 확인)."""
        biz_dt = biz_dt or datetime.now().strftime("%Y%m%d")
        return self.post("/trade/v1/overseas/deposit", {"biz_dt": biz_dt, "cur_cd": cur_cd, "cvs_div": "1"},
                         kind="account")

    def ovs_open_interest(self) -> dict:
        """해외 잔고(보유 포지션) — 계좌 전체. 종목은 호출한 쪽이 거릅니다."""
        return self.post("/trade/v1/overseas/open-interest", {"nxt_key": ""}, kind="account")

    def ovs_order_history(self, prd_cd: str = "") -> dict:
        """해외 주문·체결·미체결 전체 (inq_div 0)."""
        return self.post("/trade/v1/overseas/order-history",
                         {"trd_div_cd": "0", "inq_div": "0", "nxt_key": "", "prd_cd": prd_cd}, kind="account")

    def ovs_orderable(self, sym: str) -> dict:
        """해외 주문가능수량 (시장가 기준)."""
        return self.post("/trade/v1/overseas/orderable-quantity",
                         {"sym": sym, "glb_ord_tp_cd": "1", "ord_pric": "0", "stop_pric": ""}, kind="account")

    def ovs_market_order(self, sym: str, side: str, qty: int) -> dict:
        """해외 시장가 신규주문. 재시도하지 않습니다 (시간 초과여도 서버가 받았을 수 있어 중복 주문 위험)."""
        if self.env != "demo":
            raise SystemExit("주문은 모의 서버(demo)에서만 보냅니다. 중단합니다.")
        return self.post("/trade/v1/overseas/order", {
            "qt_div_cd": "1", "sym": sym, "trd_div_cd": {"buy": "1", "sell": "2"}[side], "glb_ord_tp_cd": "1",
            "ord_qty": str(qty), "ord_pric": "0", "stop_pric": "", "org_ord_no": "0",
        }, kind="order", retries=1)


def rows(body: dict) -> list[dict]:
    """`Occurs1` 배열을 꺼내고 필드명 앞의 `occurs1_` 를 뗍니다.

    명세의 응답 예시가 납작하게 적혀 있어 실제 모양을 아직 모릅니다. 배열이면 그대로,
    아니면 빈 목록을 돌려주고 원본은 호출한 쪽이 저장합니다.
    """
    for key, value in body.items():
        if key.lower().startswith("occurs") and isinstance(value, list):
            return [{k.split("_", 1)[1] if k.lower().startswith("occurs") else k: v for k, v in row.items()}
                    for row in value if isinstance(row, dict)]
    return []
