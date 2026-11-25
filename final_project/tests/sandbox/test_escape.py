"""전략 코드가 격리를 벗어나지 못하는지(안전 불변식 12). 실제 Docker로 돈다.

탐지형 테스트는 코드가 금지된 것에 닿으면 매수 목표({"A": {"position": 1}}), 닿지 못하면 None을
돌려주게 짠다.
"""

import asyncio
import hashlib
import json
import os
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nh_mcp.domain import JsonValue, ModelHash, SandboxResult
from nh_mcp.models.store import ModelStore
from nh_mcp.sandbox.container import MODEL_CHUNK, NAME_PREFIX, run_in_container
from nh_mcp.store import db, users

from .wire import bars, job

pytestmark = pytest.mark.docker

SECRETS = ("KEY_ENCRYPTION_KEY", "SANDBOX_TOKEN", "GOOGLE_CLIENT_SECRET")
# 컨테이너 안에 보여도 되는 환경 변수. 이미지 기본값(docker image inspect의 Config.Env:
# PATH, LANG, GPG_KEY, PYTHON_VERSION, PYTHON_SHA256, 그리고 sandbox.Dockerfile이 넣는 수치 계산
# 스레드 수 셋)과 Docker가 실행할 때 넣는 HOSTNAME·HOME, 실행 인자의 PYTHONHASHSEED뿐이다.
# 이미지가 바뀌면 test_env_allowlist_matches_the_image가 알린다.
ALLOWED_ENV = frozenset(
    {
        "PYTHONHASHSEED",
        "PATH",
        "HOME",
        "HOSTNAME",
        "LANG",
        "GPG_KEY",
        "PYTHON_VERSION",
        "PYTHON_SHA256",
        "OPENBLAS_NUM_THREADS",
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
    }
)
RUNTIME_ENV = frozenset({"PYTHONHASHSEED", "HOME", "HOSTNAME"})
NOW = datetime(2026, 12, 1, 1, 0, tzinfo=UTC)


def run(code: str, image: str) -> SandboxResult:
    return asyncio.run(run_in_container(job(code), image, wall_seconds=8.0))


def assert_never_reached(reply: SandboxResult) -> None:
    assert reply.status == "ok", reply
    assert reply.outputs == [None, None, None]


def test_cannot_open_a_network_connection(sandbox_image: str) -> None:
    code = """
import socket

def on_event(event, ctx):
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=2).close()
    except OSError:
        return None
    return {"A": {"position": 1}}
"""
    assert_never_reached(run(code, sandbox_image))


def test_env_allowlist_matches_the_image(sandbox_image: str) -> None:
    inspect = subprocess.run(
        ["docker", "image", "inspect", "-f", "{{json .Config.Env}}", sandbox_image],
        capture_output=True,
        text=True,
        check=True,
    )
    entries: list[str] = json.loads(inspect.stdout)
    assert {entry.split("=", 1)[0] for entry in entries} | RUNTIME_ENV == ALLOWED_ENV


def test_cannot_see_server_secrets_in_environment(
    sandbox_image: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in SECRETS:
        monkeypatch.setenv(name, "server-only-value")
    code = f"""
import os

ALLOWED = {set(ALLOWED_ENV)!r}
SECRETS = {set(SECRETS)!r}

def on_event(event, ctx):
    names = set(os.environ)
    if names - ALLOWED or names & SECRETS:
        return {{"A": {{"position": 1}}}}
    return None
"""
    assert_never_reached(run(code, sandbox_image))


def test_cannot_open_the_database_or_docker_socket(sandbox_image: str) -> None:
    code = """
import os

def on_event(event, ctx):
    if os.path.exists("/data/nh.db") or os.path.exists("/var/run/docker.sock"):
        return {"A": {"position": 1}}
    return None
"""
    assert_never_reached(run(code, sandbox_image))


def test_cannot_import_the_server_package(sandbox_image: str) -> None:
    code = """
import importlib

def on_event(event, ctx):
    try:
        importlib.import_module("nh_mcp")
    except ImportError:
        return None
    return {"A": {"position": 1}}
"""
    assert_never_reached(run(code, sandbox_image))


def test_cannot_write_outside_tmp(sandbox_image: str) -> None:
    code = """
def on_event(event, ctx):
    for path in ("/sandbox/x", "/harness/x", "/x"):
        try:
            with open(path, "w") as handle:
                handle.write("x")
        except OSError:
            continue
        return {"A": {"position": 1}}
    # /tmp에는 쓸 수 있어야 한다(쓰기 실패가 모든 쓰기를 막아서가 아님을 확인).
    with open("/tmp/ok", "w") as handle:
        handle.write("x")
    return None
"""
    assert_never_reached(run(code, sandbox_image))


# init에서 받은 모델 복사본을 지우고 다른 내용으로 다시 쓴 뒤, 다시 쓴 것이 보이는지 확인한다(정말
# 고쳐 썼다는 증거). 서버 쪽 원본 경로가 보이면 매수 목표를 낸다.
OVERWRITES_MODEL = """
import os

def init(ctx):
    os.chmod(ctx.model_path, 0o600)
    with open(ctx.model_path, "wb") as handle:
        handle.write(b"tampered")
    with open(ctx.model_path, "rb") as handle:
        assert handle.read() == b"tampered"

def on_event(event, ctx):
    if os.path.exists(ctx.params["stored"]) or os.path.exists(ctx.params["folder"]):
        return {"A": {"position": 1}}
    return None
"""


def test_overwriting_the_model_copy_leaves_the_stored_file_alone(
    sandbox_image: str, tmp_path: Path
) -> None:
    conn = db.connect(":memory:")
    user = users.get_or_create(conn, "g-a", "a@example.com", NOW)
    store = ModelStore(tmp_path / "models", conn, lambda: NOW)
    original = os.urandom(2 * MODEL_CHUNK + 1)
    draft = store.draft(user)
    draft.write(original)
    assert asyncio.run(store.commit(user, draft, "model.pkl")) == "uploaded"
    sha256 = ModelHash(hashlib.sha256(original).hexdigest())
    stored = tmp_path / "models" / str(user) / sha256
    sent = asyncio.run(store.read(user, sha256))
    assert sent == original
    params: dict[str, JsonValue] = {"stored": str(stored), "folder": str(stored.parent)}
    work = replace(job(OVERWRITES_MODEL, params=params), model=sent)

    assert_never_reached(asyncio.run(run_in_container(work, sandbox_image, wall_seconds=8.0)))

    assert stored.read_bytes() == original
    assert hashlib.sha256(stored.read_bytes()).hexdigest() == sha256
    assert asyncio.run(store.read(user, sha256)) == original


def test_run_without_a_model_has_no_model_file(sandbox_image: str) -> None:
    code = """
import os

def init(ctx):
    assert ctx.model_path is None, ctx.model_path

def on_event(event, ctx):
    if ctx.model_path is not None or os.path.exists("/tmp/model"):
        return {"A": {"position": 1}}
    return None
"""
    assert_never_reached(run(code, sandbox_image))


def test_cannot_see_events_not_yet_sent(sandbox_image: str) -> None:
    # 문자열·정수만 든 dict는 CPython gc가 추적하지 않아 이 검사는 약하다. 실행기가 이벤트를
    # 미리 보내는 잘못은 아래 표준 입력 검사와 test_container의 가짜 하네스 검사가 잡는다.
    code = """
import gc

KEYS = {"type", "symbol", "interval", "at", "open", "high", "low", "close", "volume"}

def on_event(event, ctx):
    seen = sum(1 for obj in gc.get_objects() if isinstance(obj, dict) and set(obj) == KEYS)
    if seen > len(ctx.history("A", "minute")) + 1:
        return {"A": {"position": 1}}
    return None
"""
    assert_never_reached(run(code, sandbox_image))


def test_reading_stdin_for_future_events_times_out(sandbox_image: str) -> None:
    # 하네스를 건너뛰고 표준 입력을 직접 읽어도, 실행기는 출력을 받기 전에 다음 이벤트를 보내지
    # 않으므로 기다리다 시간 초과로 끝난다. 실패한 실행은 출력을 하나도 돌려주지 않는다.
    code = """
import sys

def on_event(event, ctx):
    line = sys.stdin.readline()
    return {"A": {"position": 1 if "event" in line else -1}}
"""
    reply = run(code, sandbox_image)
    assert reply.status == "timeout", reply
    assert reply.outputs == []
    assert reply.index == 0


def test_polling_stdin_finds_no_future_event(sandbox_image: str) -> None:
    # 막히지 않게 엿봐도 표준 입력에는 아무것도 없다(다음 이벤트는 이 출력 뒤에야 온다).
    code = """
import select, sys

def on_event(event, ctx):
    readable, _, _ = select.select([sys.stdin], [], [], 0.3)
    if readable:
        return {"A": {"position": 1}}
    return None
"""
    assert_never_reached(run(code, sandbox_image))


def test_infinite_loop_is_stopped(sandbox_image: str) -> None:
    code = "def on_event(event, ctx):\n    while True:\n        pass\n"
    reply = run(code, sandbox_image)
    assert reply.status in {"cpu", "timeout"}, reply
    assert reply.outputs == []


def test_memory_bomb_is_stopped(sandbox_image: str) -> None:
    # 바이트열 곱셈은 실제로 메모리를 채운다(예약만 하고 끝나지 않는다).
    code = 'def on_event(event, ctx):\n    print(len(b"x" * (2 * 1024**3)))\n'
    reply = run(code, sandbox_image)
    assert reply.status == "memory", reply
    assert reply.outputs == []


def test_fork_bomb_is_stopped(sandbox_image: str) -> None:
    code = "import os\n\ndef on_event(event, ctx):\n    while True:\n        os.fork()\n"
    reply = run(code, sandbox_image)
    assert reply.status == "processes", reply
    assert reply.outputs == []


def test_output_flood_is_stopped(sandbox_image: str) -> None:
    # 프로토콜 출구가 어느 기술자든 걸리게 3~9에 모두 쓴다.
    code = """
import os

def on_event(event, ctx):
    for fd in range(3, 10):
        try:
            os.write(fd, b"x" * 2_000_000)
        except OSError:
            pass
    return None
"""
    reply = run(code, sandbox_image)
    assert reply.status in {"output", "invalid_output"}, reply
    assert reply.outputs == []


def test_deeply_nested_line_is_invalid_output(sandbox_image: str) -> None:
    # 줄 한도(64KiB) 아래(60KB)지만 json.loads가 RecursionError를 내는 줄. 실행기가 죽으면 안 된다.
    code = """
import os

def on_event(event, ctx):
    for fd in range(3, 10):
        try:
            os.write(fd, b"[" * 60_000 + b"\\n")
        except OSError:
            pass
    return None
"""
    reply = run(code, sandbox_image)
    assert reply.status == "invalid_output", reply
    assert reply.outputs == []
    assert reply.index == 0
    assert reply.detail == ""
    assert leftovers() == ""


@pytest.mark.parametrize(
    "flood",
    [
        'os.write(2, b"x" * 2_000_000)',
        'sys.stderr.write("x" * 2_000_000)',
        'print("x" * 2_000_000)',
    ],
    ids=["fd2", "sys-stderr", "print"],
)
def test_stderr_flood_is_stopped(sandbox_image: str, flood: str) -> None:
    # 표준 에러(print도 이리로 간다)도 1MB를 넘으면 끊는다. Docker 로그에는 쌓이지 않는다.
    code = f"""
import os, sys

def on_event(event, ctx):
    {flood}
    return None
"""
    reply = run(code, sandbox_image)
    assert reply.status == "output", reply
    assert reply.outputs == []
    assert leftovers() == ""


SLEEPY = """
import time

def on_event(event, ctx):
    time.sleep(4)
    return None
"""


def test_container_keeps_no_docker_log(sandbox_image: str) -> None:
    """도는 중인 전략 컨테이너의 로그 드라이버가 none이다(호스트 디스크에 출력이 쌓이지 않는다)."""

    async def scenario() -> tuple[str, SandboxResult]:
        task = asyncio.create_task(
            run_in_container(job(SLEEPY, bars(1)), sandbox_image, wall_seconds=20.0)
        )
        log_type = ""
        for _ in range(100):
            running = await asyncio.to_thread(leftovers)
            if running:
                inspect = await asyncio.to_thread(
                    subprocess.run,
                    ["docker", "inspect", "-f", "{{.HostConfig.LogConfig.Type}}", running],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                log_type = inspect.stdout.strip()
                break
            await asyncio.sleep(0.1)
        return log_type, await task

    log_type, reply = asyncio.run(scenario())
    assert log_type == "none"
    assert reply.status == "ok", reply
    assert leftovers() == ""


def leftovers() -> str:
    listing = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name={NAME_PREFIX}", "-q"],
        capture_output=True,
        text=True,
        check=True,
    )
    return listing.stdout.strip()
