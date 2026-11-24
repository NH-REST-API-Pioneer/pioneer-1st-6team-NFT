"""전략 코드가 격리를 벗어나지 못하는지(안전 불변식 12). 실제 Docker로 돈다.

탐지형 테스트는 코드가 금지된 것에 닿으면 buy, 닿지 못하면 none을 돌려주게 짠다.
"""

import asyncio
import json
import subprocess

import pytest

from nh_mcp.sandbox.container import NAME_PREFIX, run_in_container
from nh_mcp.sandbox.wire import Reply

from .wire import bars

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


def run(code: str, image: str) -> Reply:
    return asyncio.run(run_in_container(code, {}, bars(3), image, wall_seconds=8.0))


def assert_never_reached(reply: Reply) -> None:
    assert reply.status == "ok", reply
    assert set(reply.signals) == {"none"}


def test_cannot_open_a_network_connection(sandbox_image: str) -> None:
    code = """
import socket

def on_bar(bar, history, params):
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=2).close()
    except OSError:
        return "none"
    return "buy"
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

def on_bar(bar, history, params):
    names = set(os.environ)
    if names - ALLOWED or names & SECRETS:
        return "buy"
    return "none"
"""
    assert_never_reached(run(code, sandbox_image))


def test_cannot_open_the_database_or_docker_socket(sandbox_image: str) -> None:
    code = """
import os

def on_bar(bar, history, params):
    if os.path.exists("/data/nh.db") or os.path.exists("/var/run/docker.sock"):
        return "buy"
    return "none"
"""
    assert_never_reached(run(code, sandbox_image))


def test_cannot_import_the_server_package(sandbox_image: str) -> None:
    code = """
import importlib

def on_bar(bar, history, params):
    try:
        importlib.import_module("nh_mcp")
    except ImportError:
        return "none"
    return "buy"
"""
    assert_never_reached(run(code, sandbox_image))


def test_cannot_write_outside_tmp(sandbox_image: str) -> None:
    code = """
def on_bar(bar, history, params):
    for path in ("/sandbox/x", "/harness/x", "/x"):
        try:
            with open(path, "w") as handle:
                handle.write("x")
        except OSError:
            continue
        return "buy"
    # /tmp에는 쓸 수 있어야 한다(쓰기 실패가 모든 쓰기를 막아서가 아님을 확인).
    with open("/tmp/ok", "w") as handle:
        handle.write("x")
    return "none"
"""
    assert_never_reached(run(code, sandbox_image))


def test_cannot_see_bars_not_yet_sent(sandbox_image: str) -> None:
    # 문자열·정수만 든 dict는 CPython gc가 추적하지 않아 이 검사는 약하다. 실행기가 봉을
    # 미리 보내는 잘못은 아래 test_reading_stdin_for_future_bars_times_out이 잡는다.
    code = """
import gc

KEYS = {"at", "open", "high", "low", "close", "volume"}

def on_bar(bar, history, params):
    seen = sum(1 for obj in gc.get_objects() if isinstance(obj, dict) and set(obj) == KEYS)
    if seen > len(history) + 1:
        return "buy"
    return "none"
"""
    assert_never_reached(run(code, sandbox_image))


def test_reading_stdin_for_future_bars_times_out(sandbox_image: str) -> None:
    code = """
import sys

def on_bar(bar, history, params):
    sys.stdin.readline()
    return "buy"
"""
    reply = run(code, sandbox_image)
    assert reply.status == "timeout", reply
    assert reply.signals == []


def test_infinite_loop_is_stopped(sandbox_image: str) -> None:
    code = "def on_bar(bar, history, params):\n    while True:\n        pass\n"
    reply = run(code, sandbox_image)
    assert reply.status in {"cpu", "timeout"}, reply
    assert reply.signals == []


def test_memory_bomb_is_stopped(sandbox_image: str) -> None:
    # 바이트열 곱셈은 실제로 메모리를 채운다(예약만 하고 끝나지 않는다).
    code = 'def on_bar(bar, history, params):\n    return str(len(b"x" * (2 * 1024**3)))\n'
    reply = run(code, sandbox_image)
    assert reply.status == "memory", reply
    assert reply.signals == []


def test_fork_bomb_is_stopped(sandbox_image: str) -> None:
    code = "import os\n\ndef on_bar(bar, history, params):\n    while True:\n        os.fork()\n"
    reply = run(code, sandbox_image)
    assert reply.status == "processes", reply
    assert reply.signals == []


def test_output_flood_is_stopped(sandbox_image: str) -> None:
    # 프로토콜 출구가 어느 기술자든 걸리게 3~9에 모두 쓴다.
    code = """
import os

def on_bar(bar, history, params):
    for fd in range(3, 10):
        try:
            os.write(fd, b"x" * 2_000_000)
        except OSError:
            pass
    return "none"
"""
    reply = run(code, sandbox_image)
    assert reply.status in {"output", "invalid_signal"}, reply
    assert reply.signals == []


def test_deeply_nested_line_is_invalid_signal(sandbox_image: str) -> None:
    # 1MB 아래(약 200KB)지만 json.loads가 RecursionError를 내는 줄. 실행기가 죽으면 안 된다.
    code = """
import os

def on_bar(bar, history, params):
    for fd in range(3, 10):
        try:
            os.write(fd, b"[" * 200_000 + b"\\n")
        except OSError:
            pass
    return "none"
"""
    reply = run(code, sandbox_image)
    assert reply.status == "invalid_signal", reply
    assert reply.signals == []
    assert reply.bar_index == 0
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

def on_bar(bar, history, params):
    {flood}
    return "none"
"""
    reply = run(code, sandbox_image)
    assert reply.status == "output", reply
    assert reply.signals == []
    assert leftovers() == ""


SLEEPY = """
import time

def on_bar(bar, history, params):
    time.sleep(4)
    return "none"
"""


def test_container_keeps_no_docker_log(sandbox_image: str) -> None:
    """도는 중인 전략 컨테이너의 로그 드라이버가 none이다(호스트 디스크에 출력이 쌓이지 않는다)."""

    async def scenario() -> tuple[str, Reply]:
        task = asyncio.create_task(
            run_in_container(SLEEPY, {}, bars(1), sandbox_image, wall_seconds=20.0)
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
