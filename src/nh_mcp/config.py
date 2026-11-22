"""환경 변수로 받는 설정."""

from collections.abc import Mapping
from dataclasses import dataclass

from nh_mcp.domain import Env

DEMO_BASE_URL = "https://apidemo.futures.co.kr"


class ConfigError(Exception):
    """설정이 없거나 잘못되었다."""


@dataclass(frozen=True)
class Settings:
    nh_base_url: str

    @property
    def env(self) -> Env:
        return "demo" if self.nh_base_url == DEMO_BASE_URL else "not_demo"


def load_settings(environ: Mapping[str, str]) -> Settings:
    raw = environ.get("NH_BASE_URL", "").strip()
    if not raw:
        raise ConfigError(f"NH_BASE_URL이 비어 있습니다. 모의 서버 주소: {DEMO_BASE_URL}")
    return Settings(nh_base_url=raw.rstrip("/"))
