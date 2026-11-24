"""배포 설정이 지켜야 할 것.

포트를 열지 않는다, 비밀값을 파일에 적지 않는다, 컨테이너 권한을 줄인다.
"""

import asyncio
import base64
import re
from pathlib import Path

import httpx2
from conftest import Serve
from fakes import World

from nh_mcp.config import load_settings
from nh_mcp.server import run_options

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = (ROOT / "deploy" / "compose.yaml").read_text(encoding="utf-8")
DOCKERFILE = (ROOT / "deploy" / "Dockerfile").read_text(encoding="utf-8")
DOCKERIGNORE = (ROOT / ".dockerignore").read_text(encoding="utf-8")
SECRETS = ("GOOGLE_CLIENT_SECRET", "KEY_ENCRYPTION_KEY", "TUNNEL_TOKEN")


def code_lines(text: str) -> list[str]:
    return [
        line.split("#", 1)[0].rstrip()
        for line in text.splitlines()
        if line.split("#", 1)[0].strip()
    ]


def test_compose_publishes_no_port() -> None:
    lines = code_lines(COMPOSE)
    assert not any(re.match(r"\s*ports\s*:", line) for line in lines)
    assert any(re.match(r"\s*expose\s*:", line) for line in lines)


def test_secrets_come_only_from_the_env_file() -> None:
    for line in code_lines(COMPOSE):
        for name in SECRETS:
            if re.match(rf"\s*{name}\s*:", line):
                assert re.match(rf"\s*{name}\s*:\s*\$\{{", line), line
    # 개발 · 녹화용 NH 키는 서버에 넘기지 않는다.
    assert "NH_DEV_" not in COMPOSE


def test_container_runs_with_reduced_rights() -> None:
    lines = code_lines(COMPOSE)
    for expected in (
        "read_only: true",
        "no-new-privileges:true",
        "- ALL",
        "restart: unless-stopped",
    ):
        assert any(expected in line for line in lines), expected
    assert re.search(r"^USER (?!root)\w+", DOCKERFILE, re.MULTILINE)
    # DB와 Google 로그인 상태는 볼륨(/data)에 둔다.
    assert "DB_PATH=/data/" in DOCKERFILE and "FASTMCP_HOME=/data/" in DOCKERFILE


def test_build_context_only_ships_the_code() -> None:
    assert code_lines(DOCKERIGNORE)[0] == "*"
    allowed = {line.removeprefix("!") for line in code_lines(DOCKERIGNORE) if line.startswith("!")}
    assert allowed == {"pyproject.toml", "uv.lock", "README.md", "src/"}


PUBLIC = load_settings(
    {
        "PUBLIC_BASE_URL": "https://mcp.example.com",
        "GOOGLE_CLIENT_ID": "fake-client-id",
        "GOOGLE_CLIENT_SECRET": "fake-google-secret-0000",
        "ADMIN_EMAILS": "admin@example.com",
        "DB_PATH": "data/nh.db",
        "KEY_ENCRYPTION_KEY": base64.b64encode(bytes(32)).decode(),
    }
)


def test_run_options_accept_only_the_public_host() -> None:
    assert run_options(PUBLIC) == {
        "host": "127.0.0.1",
        "port": 8000,
        "host_origin_protection": True,
        "allowed_hosts": ["mcp.example.com"],
        "allowed_origins": ["https://mcp.example.com"],
    }


def test_browser_form_through_the_https_tunnel_is_not_refused(serve: Serve) -> None:
    """Tunnel 뒤의 서버는 http로 받지만 브라우저는 https Origin을 보낸다.

    배포했을 때 키 등록 폼이 이것 때문에 막혔다.
    """
    options = run_options(PUBLIC)
    world = World()

    async def scenario() -> tuple[httpx2.Response, httpx2.Response]:
        async with (
            serve(
                world.mcp,
                allowed_hosts=options["allowed_hosts"],
                allowed_origins=options["allowed_origins"],
            ) as url,
            httpx2.AsyncClient() as http,
        ):
            form = url.removesuffix("/mcp") + "/keys/register"
            headers = {"Host": "mcp.example.com"}
            ours = await http.post(form, headers={**headers, "Origin": "https://mcp.example.com"})
            evil = await http.post(form, headers={**headers, "Origin": "https://evil.example.com"})
        return ours, evil

    ours, evil = asyncio.run(scenario())
    assert "Forbidden Origin" not in ours.text
    assert evil.status_code == 403 and "Forbidden Origin" in evil.text


def test_server_refuses_requests_for_another_host(serve: Serve) -> None:
    world = World()

    async def scenario() -> tuple[int, int]:
        async with (
            serve(world.mcp, allowed_hosts=["mcp.example.com"]) as url,
            httpx2.AsyncClient() as http,
        ):
            foreign = await http.post(url, headers={"Host": "evil.example.com"}, json={})
            ours = await http.post(url, headers={"Host": "mcp.example.com"}, json={})
        return foreign.status_code, ours.status_code

    foreign, ours = asyncio.run(scenario())
    assert foreign != ours
    assert foreign in (400, 403, 421)
    # 우리 주소로 온 요청은 Host 검사를 지나 로그인 검사에서 막힌다.
    assert ours == 401
