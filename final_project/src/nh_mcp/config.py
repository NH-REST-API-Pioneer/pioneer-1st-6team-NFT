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
    bind_host: str = "127.0.0.1"


def load_settings(environ: Mapping[str, str]) -> Settings:
    def required(name: str) -> str:
        value = environ.get(name, "").strip()
        if not value:
            raise ConfigError(f"{name}이(가) 비어 있습니다.")
        return value

    public_base_url = required("PUBLIC_BASE_URL").rstrip("/")
    if not public_base_url.startswith(("https://", *LOCAL_ORIGINS)):
        raise ConfigError("PUBLIC_BASE_URL은 https 주소여야 합니다(로컬 개발은 localhost만 http).")

    open_signup_raw = environ.get("OPEN_SIGNUP", "false").strip().lower()
    if open_signup_raw not in ("true", "false"):
        raise ConfigError("OPEN_SIGNUP은 true 또는 false여야 합니다.")
    open_signup = open_signup_raw == "true"

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
        bind_host=_bind_host(environ.get("BIND_HOST", BIND_HOSTS[0])),
    )


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
