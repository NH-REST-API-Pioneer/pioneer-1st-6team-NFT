"""배포 설정이 지켜야 할 것.

포트를 열지 않는다, 비밀값을 파일에 적지 않는다, 컨테이너 권한을 줄인다.
"""

import asyncio
import base64
import re
from pathlib import Path

import httpx2
import uvicorn
from conftest import Serve
from fakes import World
from starlette.applications import Starlette

from nh_mcp.config import load_settings
from nh_mcp.server import run_options

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = (ROOT / "deploy" / "compose.yaml").read_text(encoding="utf-8")
DOCKERFILE = (ROOT / "deploy" / "Dockerfile").read_text(encoding="utf-8")
DOCKERIGNORE = (ROOT / ".dockerignore").read_text(encoding="utf-8")
EXECUTOR_DOCKERFILE = (ROOT / "deploy" / "executor.Dockerfile").read_text(encoding="utf-8")
SECRETS = ("GOOGLE_CLIENT_SECRET", "KEY_ENCRYPTION_KEY", "TUNNEL_TOKEN")


def code_lines(text: str) -> list[str]:
    return [
        line.split("#", 1)[0].rstrip()
        for line in text.splitlines()
        if line.split("#", 1)[0].strip()
    ]


def service_block(name: str) -> list[str]:
    """compose에서 서비스 하나의 줄들(주석 뺀 것). 서비스 이름은 들여쓰기 2칸이다."""
    block: list[str] = []
    inside = False
    for line in code_lines(COMPOSE):
        indent = len(line) - len(line.lstrip())
        if indent == 2 and line.strip().endswith(":"):
            inside = line.strip() == f"{name}:"
        elif indent == 0:
            inside = False
        if inside:
            block.append(line)
    return block


def service_names() -> list[str]:
    """compose `services:` 바로 아래(들여쓰기 2칸)의 서비스 이름들."""
    names: list[str] = []
    inside = False
    for line in code_lines(COMPOSE):
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            inside = line.strip() == "services:"
        elif inside and indent == 2 and line.strip().endswith(":"):
            names.append(line.strip().removesuffix(":"))
    return names


def list_items(block: list[str], key: str) -> list[str]:
    """`key:` 아래 `- 값` 항목들(같은 들여쓰기 안에서)."""
    items: list[str] = []
    capturing = False
    key_indent = 0
    for line in block:
        indent = len(line) - len(line.lstrip())
        if line.strip() == f"{key}:":
            capturing, key_indent = True, indent
        elif capturing and indent > key_indent and line.strip().startswith("- "):
            items.append(line.strip().removeprefix("- ").strip().strip('"'))
        elif capturing and indent <= key_indent:
            capturing = False
    return items


def env_names(block: list[str]) -> list[str]:
    names: list[str] = []
    capturing = False
    for line in block:
        indent = len(line) - len(line.lstrip())
        if line.strip() == "environment:":
            capturing = True
        elif capturing and indent > 4:
            names.append(line.split(":", 1)[0].strip())
        elif capturing:
            capturing = False
    return names


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
        "SANDBOX_URL": "http://executor.test",
        "SANDBOX_TOKEN": "fake-sandbox-token-" + "0" * 16,
    }
)


def test_run_options_accept_only_the_public_host() -> None:
    assert run_options(PUBLIC) == {
        "host": "127.0.0.1",
        "port": 8000,
        "host_origin_protection": True,
        "allowed_hosts": ["mcp.example.com"],
        "allowed_origins": ["https://mcp.example.com"],
        "uvicorn_config": {"access_log": False},
    }


def test_the_access_log_is_off_so_approval_links_are_not_written() -> None:
    """접근 로그는 요청 주소를 그대로 적는다. 한도 승인 링크(/limits/approve/<토큰>)가 남는다."""
    config = uvicorn.Config(Starlette(), **run_options(PUBLIC)["uvicorn_config"])

    assert config.access_log is False


def test_compose_passes_the_orders_switch_and_keeps_it_off_by_default() -> None:
    lines = code_lines(COMPOSE)
    assert any(re.fullmatch(r"\s*ORDERS_ENABLED: \$\{ORDERS_ENABLED:-false\}", ln) for ln in lines)
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "ORDERS_ENABLED=false" in code_lines(env_example)


def test_browser_form_through_the_https_tunnel_is_not_refused(serve: Serve) -> None:
    """Tunnel 뒤의 서버는 http로 받지만 브라우저는 https Origin을 보낸다.

    배포했을 때 키 등록 폼이 이것 때문에 막혔다.
    """
    options = run_options(PUBLIC)
    world = World()

    async def scenario() -> tuple[httpx2.Response, httpx2.Response, httpx2.Response]:
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
            # Referrer-Policy가 no-referrer면 브라우저는 Origin을 "null"로 보낸다. 이것은 막힌다.
            hidden = await http.post(form, headers={**headers, "Origin": "null"})
        return ours, evil, hidden

    ours, evil, hidden = asyncio.run(scenario())
    assert "Forbidden Origin" not in ours.text
    assert evil.status_code == 403 and "Forbidden Origin" in evil.text
    assert hidden.status_code == 403


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


# 전략 실행기(executor). docker 소켓을 쥐고 전략 컨테이너를 띄우는 유일한 서비스다.


def test_executor_has_no_secrets() -> None:
    """실행기는 토큰과 이미지 이름만 안다. 키·DB·Google 비밀값은 받지 않는다."""
    assert env_names(service_block("executor")) == ["SANDBOX_TOKEN", "SANDBOX_IMAGE"]


def test_executor_reads_no_env_file() -> None:
    """.env를 통째로 넘기면 키·Google 비밀값이 실행기에 들어간다. 두 값만 environment로 준다."""
    executor = service_block("executor")
    assert executor
    assert not any(re.match(r"\s*env_file\s*:", ln) for ln in executor)


def test_executor_mounts_only_the_docker_socket() -> None:
    """실행기에는 DB 볼륨도, 다른 호스트 경로도 붙이지 않는다."""
    assert list_items(service_block("executor"), "volumes") == [
        "/var/run/docker.sock:/var/run/docker.sock"
    ]


def test_only_mcp_uses_the_data_volume() -> None:
    """DB와 Google 로그인 상태가 든 mcpdata 볼륨은 mcp 서비스만 붙인다."""
    names = service_names()
    assert {"mcp", "executor", "cloudflared", "sandbox-image"} <= set(names)
    assert [n for n in names if any("mcpdata" in ln for ln in service_block(n))] == ["mcp"]


def test_only_the_executor_mounts_the_docker_socket() -> None:
    mounts = [
        name
        for name in ("mcp", "executor", "cloudflared")
        if any("/var/run/docker.sock" in ln for ln in service_block(name))
    ]
    assert mounts == ["executor"]
    assert sum("/var/run/docker.sock" in ln for ln in code_lines(COMPOSE)) == 1


def test_executor_is_only_on_the_internal_network() -> None:
    executor = service_block("executor")
    assert list_items(executor, "networks") == ["sandbox"]
    assert not any(re.match(r"\s*ports\s*:", ln) for ln in executor)
    # sandbox 망은 internal: true라 바깥으로 나가지 못한다.
    top = code_lines(COMPOSE)
    start = top.index("networks:")
    assert top[start + 1 : start + 3] == ["  sandbox:", "    internal: true"]
    # 바깥에서 들어오는 cloudflared는 이 망에 없다. mcp만 두 망에 걸친다.
    assert "sandbox" not in list_items(service_block("cloudflared"), "networks")
    assert sorted(list_items(service_block("mcp"), "networks")) == ["default", "sandbox"]


def test_executor_runs_with_reduced_rights() -> None:
    executor = service_block("executor")
    for expected in (
        "read_only: true",
        "no-new-privileges:true",
        "- ALL",
        "restart: unless-stopped",
    ):
        assert any(expected in ln for ln in executor), expected
    # 소켓 권한은 root가 아니라 docker 그룹으로 받는다.
    assert any("DOCKER_GID:?" in ln for ln in executor)
    assert re.search(r"^USER (?!root)\w+", EXECUTOR_DOCKERFILE, re.MULTILINE)
    assert 'CMD ["python", "-m", "nh_mcp.sandbox.executor"]' in EXECUTOR_DOCKERFILE
    # docker CLI는 정확한 버전의 공식 이미지에서 복사한다(latest 금지).
    assert re.search(
        r"COPY --from=docker:29\.\d+\.\d+-cli /usr/local/bin/docker ", EXECUTOR_DOCKERFILE
    )


def test_mcp_gets_sandbox_settings_from_env() -> None:
    mcp = service_block("mcp")
    assert any(re.fullmatch(r"\s*SANDBOX_URL: http://executor:8100", ln) for ln in mcp)
    assert any(re.fullmatch(r"\s*SANDBOX_TOKEN: \$\{SANDBOX_TOKEN:\?[^}]+\}", ln) for ln in mcp)
    executor = service_block("executor")
    assert any(
        re.fullmatch(r"\s*SANDBOX_TOKEN: \$\{SANDBOX_TOKEN:\?[^}]+\}", ln) for ln in executor
    )


def test_strategy_image_is_built_by_its_own_profile_service() -> None:
    image = service_block("sandbox-image")
    assert any("deploy/sandbox.Dockerfile" in ln for ln in image)
    assert any("image: nh-mcp-sandbox:latest" in ln for ln in image)
    assert list_items(image, "profiles") == ["build"]


def test_docker_gid_is_documented_in_the_env_example() -> None:
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "DOCKER_GID=" in code_lines(env_example)
    assert "stat -c %g /var/run/docker.sock" in env_example
