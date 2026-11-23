"""로그인을 건너뛰는 길이 운영 코드에 없게 막는다(안전 불변식 10)."""

import inspect
from pathlib import Path

import fastmcp
import pytest
from fastmcp.server.auth.providers.google import GoogleProvider

from nh_mcp.config import load_settings
from nh_mcp.server import build_server, create_server
from nh_mcp.store import db

SRC = Path(__file__).resolve().parents[1] / "src"
# 테스트에서만 쓰는 인증기. 운영 코드에 나오면 안 된다.
TEST_ONLY_AUTH = ("StaticTokenVerifier", "DebugTokenVerifier", "InMemoryOAuthProvider")


def test_server_cannot_be_built_without_auth() -> None:
    params = inspect.signature(build_server).parameters
    assert params["auth"].default is inspect.Parameter.empty
    assert params["gate"].default is inspect.Parameter.empty


def test_production_server_uses_google_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Google 로그인이 OAuth 상태를 저장할 폴더를 테스트용으로 돌린다.
    monkeypatch.setattr(fastmcp.settings, "home", tmp_path)
    settings = load_settings(
        {
            "PUBLIC_BASE_URL": "http://localhost:8000",
            "GOOGLE_CLIENT_ID": "fake-client-id",
            "GOOGLE_CLIENT_SECRET": "fake-google-secret-0000",
            "ADMIN_EMAILS": "admin@example.com",
            "DB_PATH": str(tmp_path / "nh.db"),
        }
    )
    server = create_server(settings, db.connect(":memory:"))
    assert isinstance(server.auth, GoogleProvider)


def test_source_has_no_test_only_auth() -> None:
    files = sorted(SRC.rglob("*.py"))
    assert files
    offenders = [
        f"{path.relative_to(SRC)}: {name}"
        for path in files
        for name in TEST_ONLY_AUTH
        if name in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
