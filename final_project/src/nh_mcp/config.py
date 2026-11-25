"""설정.

NH 주소는 모의 서버로 고정한다. 실전 주소로 바꾸는 환경 변수나 인자는 두지 않는다.
나머지는 환경 변수로 받고, 빠졌거나 틀리면 서버를 띄우지 않는다.
"""

import base64
import binascii
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from nh_mcp.domain import Env

KEY_BYTES = 32
# 실행기 토큰의 최소 길이. 짧으면 추측으로 뚫릴 수 있다.
SANDBOX_TOKEN_MIN = 32

NH_BASE_URL = "https://apidemo.futures.co.kr"
NH_STREAM_URL = "wss://apidemo.futures.co.kr"
ENV: Env = "demo"

# 로컬 개발에서만 http를 허용한다. 배포 주소는 https여야 한다.
LOCAL_ORIGINS = ("http://localhost", "http://127.0.0.1")
# 서버가 받는 주소. 컨테이너 안에서만 0.0.0.0을 쓴다(밖으로 여는 포트는 없다).
BIND_HOSTS = ("127.0.0.1", "0.0.0.0")


class ConfigError(Exception):
    """설정이 없거나 잘못됐다. 메시지에 비밀값을 넣지 않는다."""


@dataclass(frozen=True)
class Settings:
    public_base_url: str
    google_client_id: str
    google_client_secret: str = field(repr=False)
    admin_emails: frozenset[str]
    open_signup: bool
    db_path: Path
    key_encryption_key: bytes = field(repr=False)
    # 전략 실행기 주소와 그 실행기에 말을 걸 때 쓰는 토큰. 주문 스위치와 상관없이 필수다.
    sandbox_url: str
    sandbox_token: str = field(repr=False)
    bind_host: str = "127.0.0.1"
    # 주문 차단 스위치. 꺼져 있으면 주문 도구를 등록하지 않고 서비스도 거부한다.
    orders_enabled: bool = False

    @property
    def models_dir(self) -> Path:
        """사용자가 올린 모델 파일을 두는 폴더. DB 파일과 같은 곳(배포는 /data 볼륨)이다."""
        return self.db_path.parent / "models"

    @property
    def ticks_db_path(self) -> Path:
        """저장한 틱·호가(`ticks.db`). 쓰기가 잦아 주 DB와 나누되 같은 폴더(배포는 /data 볼륨)다."""
        return self.db_path.parent / "ticks.db"


def load_settings(environ: Mapping[str, str]) -> Settings:
    def required(name: str) -> str:
        value = environ.get(name, "").strip()
        if not value:
            raise ConfigError(f"{name}이(가) 비어 있습니다.")
        return value

    public_base_url = required("PUBLIC_BASE_URL").rstrip("/")
    if not public_base_url.startswith(("https://", *LOCAL_ORIGINS)):
        raise ConfigError("PUBLIC_BASE_URL은 https 주소여야 합니다(로컬 개발은 localhost만 http).")

    open_signup = _switch(environ, "OPEN_SIGNUP")

    admin_emails = frozenset(
        email.strip().lower()
        for email in environ.get("ADMIN_EMAILS", "").split(",")
        if email.strip()
    )
    if not open_signup and not admin_emails:
        raise ConfigError(
            "OPEN_SIGNUP=false이면 ADMIN_EMAILS가 있어야 합니다. 아무도 로그인할 수 없습니다."
        )

    return Settings(
        public_base_url=public_base_url,
        google_client_id=required("GOOGLE_CLIENT_ID"),
        google_client_secret=required("GOOGLE_CLIENT_SECRET"),
        admin_emails=admin_emails,
        open_signup=open_signup,
        db_path=Path(required("DB_PATH")),
        key_encryption_key=_encryption_key(required("KEY_ENCRYPTION_KEY")),
        sandbox_url=_sandbox_url(required("SANDBOX_URL")),
        sandbox_token=_sandbox_token(required("SANDBOX_TOKEN")),
        bind_host=_bind_host(environ.get("BIND_HOST", BIND_HOSTS[0])),
        orders_enabled=_switch(environ, "ORDERS_ENABLED"),
    )


def _switch(environ: Mapping[str, str], name: str) -> bool:
    """켜고 끄는 설정. 없으면 false, true·false 밖의 값(빈 값 포함)이면 서버를 띄우지 않는다."""
    raw = environ.get(name, "false").strip().lower()
    if raw not in ("true", "false"):
        raise ConfigError(f"{name}은(는) true 또는 false여야 합니다.")
    return raw == "true"


def _encryption_key(raw: str) -> bytes:
    try:
        key = base64.b64decode(raw, validate=True)
    except binascii.Error:
        raise ConfigError("KEY_ENCRYPTION_KEY는 base64여야 합니다.") from None
    if len(key) != KEY_BYTES:
        raise ConfigError(
            f"KEY_ENCRYPTION_KEY는 {KEY_BYTES}바이트를 base64로 적은 값이어야 합니다."
        )
    return key


def _bind_host(raw: str) -> str:
    host = raw.strip()
    if host not in BIND_HOSTS:
        raise ConfigError(f"BIND_HOST는 {' 또는 '.join(BIND_HOSTS)}여야 합니다.")
    return host


def _sandbox_url(raw: str) -> str:
    # 실행기는 같은 호스트의 내부 네트워크에만 있어 http다. 밖으로 나가는 주소가 아니다.
    if not raw.startswith("http://"):
        raise ConfigError("SANDBOX_URL은 http:// 로 시작해야 합니다.")
    return raw.rstrip("/")


def _sandbox_token(raw: str) -> str:
    if len(raw) < SANDBOX_TOKEN_MIN:
        raise ConfigError(f"SANDBOX_TOKEN은 {SANDBOX_TOKEN_MIN}자 이상이어야 합니다.")
    return raw
