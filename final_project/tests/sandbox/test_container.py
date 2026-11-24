"""실행기와 하네스가 프로토콜대로 주고받는지. 실제 Docker로 돈다."""

import asyncio
import secrets
import subprocess
import sys
import time
from pathlib import Path

import pytest

from nh_mcp.domain import JsonValue
from nh_mcp.sandbox import container
from nh_mcp.sandbox.container import (
    NAME_PREFIX,
    ContainerStartError,
    docker_argv,
    remove_leftovers,
    run_in_container,
)
from nh_mcp.sandbox.wire import MAX_DETAIL, Reply, WireBar
from nh_mcp.strategies.examples import MEAN_REVERSION, MEAN_REVERSION_PARAMS

from .wire import bars

pytestmark = pytest.mark.docker

WALL = 8.0


def run(
    code: str,
    image: str,
    wire: list[WireBar],
    params: dict[str, JsonValue] | None = None,
    wall_seconds: float = WALL,
) -> Reply:
    return asyncio.run(run_in_container(code, params or {}, wire, image, wall_seconds=wall_seconds))


def test_argv_is_constant() -> None:
    assert docker_argv("img", "nhmcp-sbx-abc") == [
        "docker",
        "run",
        "-i",
        "--network",
        "none",
        "--read-only",
        "--tmpfs",
        "/tmp:size=16m,noexec,nosuid",
        "--user",
        "65534:65534",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--memory",
        "512m",
        "--memory-swap",
        "512m",
        "--pids-limit",
        "32",
        "--cpus",
        "1",
        "--ulimit",
        "cpu=60:60",
        "--ulimit",
        "nofile=64:64",
        "--ulimit",
        "fsize=16777216:16777216",
        "-e",
        "PYTHONHASHSEED=0",
        "--workdir",
        "/sandbox",
        "--log-driver",
        "none",
        "--pull",
        "never",
        "--name",
        "nhmcp-sbx-abc",
        "img",
        "python",
        "-s",
        "/harness/harness.py",
    ]


def test_runs_example_and_returns_one_signal_per_bar(sandbox_image: str) -> None:
    reply = run(MEAN_REVERSION, sandbox_image, bars(25), MEAN_REVERSION_PARAMS)
    assert reply.status == "ok", reply
    assert len(reply.signals) == 25
    assert reply.bar_index is None


SEEDED_CHOICE = """
import random

def on_bar(bar, history, params):
    # 문자열 set의 순회 순서는 PYTHONHASHSEED에 달려 있다.
    names = [name for name in {"buy", "sell", "exit", "none"}]
    return names[int(random.random() * len(names))]
"""


def test_same_input_gives_same_signals(sandbox_image: str) -> None:
    first = run(SEEDED_CHOICE, sandbox_image, bars(10))
    second = run(SEEDED_CHOICE, sandbox_image, bars(10))
    assert first.status == "ok", first
    assert first.signals == second.signals


NUMPY_CHOICE = """
import numpy
import pandas

def on_bar(bar, history, params):
    frame = pandas.DataFrame(history + [bar])
    assert len(frame) == len(history) + 1
    return ["buy", "sell", "exit", "none"][int(numpy.random.rand() * 4)]
"""


def test_numpy_and_pandas_are_importable(sandbox_image: str) -> None:
    first = run(NUMPY_CHOICE, sandbox_image, bars(10))
    second = run(NUMPY_CHOICE, sandbox_image, bars(10))
    assert first.status == "ok", first
    assert first.signals == second.signals


def test_korean_comments_run(sandbox_image: str) -> None:
    code = (
        '# 한글 주석: 늘 유지한다\ndef on_bar(bar, history, params):\n    return "none"  # 유지\n'
    )
    reply = run(code, sandbox_image, bars(3))
    assert reply.status == "ok", reply
    assert reply.signals == ["none", "none", "none"]


def test_print_does_not_break_the_protocol(sandbox_image: str) -> None:
    code = 'def on_bar(bar, history, params):\n    print("hello")\n    return "none"\n'
    reply = run(code, sandbox_image, bars(3))
    assert reply.status == "ok", reply
    assert reply.signals == ["none", "none", "none"]


@pytest.mark.parametrize(
    "code",
    [
        "def on_bar(bar, history, params)\n    return 'none'\n",
        "def other(bar, history, params):\n    return 'none'\n",
        "def on_bar(bar, history):\n    return 'none'\n",
        "on_bar = 1\n",
    ],
    ids=["syntax", "missing", "two-args", "not-callable"],
)
def test_bad_code_is_invalid_code(sandbox_image: str, code: str) -> None:
    reply = run(code, sandbox_image, bars(3))
    assert reply.status == "invalid_code", reply
    assert reply.signals == []
    assert reply.bar_index is None


@pytest.mark.parametrize(
    ("value", "detail"), [("None", "NoneType"), ('"BUY"', "'BUY'"), ('{"a": 1}', "dict")]
)
def test_non_string_signal_is_invalid_signal(sandbox_image: str, value: str, detail: str) -> None:
    code = f"def on_bar(bar, history, params):\n    return {value}\n"
    reply = run(code, sandbox_image, bars(3))
    assert reply.status == "invalid_signal", reply
    assert reply.bar_index == 0
    assert reply.signals == []
    # detail에는 전략 코드가 낸 것만 담는다. 설명 문장은 서비스가 붙인다.
    assert reply.detail == detail


def test_exception_reports_type_and_bar(sandbox_image: str) -> None:
    code = (
        "def on_bar(bar, history, params):\n"
        "    if len(history) == 1:\n"
        "        raise ZeroDivisionError('가' * 2000)\n"
        "    return 'none'\n"
    )
    reply = run(code, sandbox_image, bars(3))
    assert reply.status == "exception", reply
    assert reply.bar_index == 1
    assert "ZeroDivisionError" in reply.detail
    assert len(reply.detail) <= MAX_DETAIL


def test_self_exit_is_exception_with_exit_code(sandbox_image: str) -> None:
    code = "import os\n\ndef on_bar(bar, history, params):\n    os._exit(3)\n"
    reply = run(code, sandbox_image, bars(3))
    assert reply.status == "exception", reply
    assert "3" in reply.detail
    assert reply.bar_index == 0


def leftover_containers() -> str:
    listing = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name={NAME_PREFIX}", "-q"],
        capture_output=True,
        text=True,
        check=True,
    )
    return listing.stdout.strip()


def test_no_container_is_left_behind(sandbox_image: str) -> None:
    ok = run('def on_bar(bar, history, params):\n    return "none"\n', sandbox_image, bars(2))
    stuck = run(
        "def on_bar(bar, history, params):\n    while True:\n        pass\n",
        sandbox_image,
        bars(2),
        wall_seconds=4.0,
    )
    broken = run("def on_bar(bar, history, params):\n    return 1 / 0\n", sandbox_image, bars(2))
    assert (ok.status, stuck.status, broken.status) == ("ok", "timeout", "exception")
    assert leftover_containers() == ""


# 줄 단위로 정확히 읽는 가짜 하네스. 봉을 받을 때마다 다음 봉이 벌써 와 있는지 본다.
# (진짜 하네스는 표준 입력을 버퍼로 미리 읽어 이 검사를 못 한다. gc 검사도 약하다.)
EAGER_PROBE = """
import json, os, select

def line():
    data = b""
    while not data.endswith(b"\\n"):
        chunk = os.read(0, 1)
        if not chunk:
            return None
        data += chunk
    return json.loads(data)

line()
os.write(1, b'{"ready": true}\\n')
while line() is not None:
    early, _, _ = select.select([0], [], [], 0.3)
    os.write(1, b'{"signal": "buy"}\\n' if early else b'{"signal": "none"}\\n')
"""


def test_next_bar_is_sent_only_after_the_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(EAGER_PROBE, encoding="utf-8")

    # 컨테이너 대신 가짜 하네스를 띄운다. 실행기의 주고받는 순서만 본다.
    def probe_argv(image: str, name: str) -> list[str]:
        return [sys.executable, "-I", str(probe)]

    monkeypatch.setattr(container, "docker_argv", probe_argv)
    reply = run("", "unused", bars(3))
    assert reply == Reply("ok", ["none", "none", "none"], None, "")


def test_cpu_limit_is_reported_as_cpu(sandbox_image: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # 60초를 기다리지 않게 CPU 한도만 1초로 줄인다. soft와 hard가 같으면 커널은 SIGXCPU(152)
    # 대신 SIGKILL(137)을 보낸다. OOM 표시가 없는 137을 cpu로 읽는지 본다.
    shortened = tuple("cpu=1:1" if arg == "cpu=60:60" else arg for arg in container.FIXED_ARGS)
    assert shortened != container.FIXED_ARGS
    monkeypatch.setattr(container, "FIXED_ARGS", shortened)
    code = "def on_bar(bar, history, params):\n    while True:\n        pass\n"
    reply = run(code, sandbox_image, bars(2))
    assert reply.status == "cpu", reply
    assert reply.bar_index == 0


MISSING_IMAGE = "nh-mcp-sandbox:missing-tag"


def test_missing_image_is_a_start_error_not_a_strategy_failure() -> None:
    """이미지가 없으면 코드가 돌지 못했다. 전략 사유가 아니라 예외다(받으러 가지도 않는다)."""
    started = time.monotonic()
    with pytest.raises(ContainerStartError):
        run('def on_bar(bar, history, params):\n    return "none"\n', MISSING_IMAGE, bars(2))
    assert time.monotonic() - started < 10
    assert leftover_containers() == ""


def exists(name: str) -> bool:
    found = subprocess.run(
        ["docker", "inspect", "-f", "{{.Name}}", name], capture_output=True, check=False
    )
    return found.returncode == 0


def test_leftover_containers_are_removed(sandbox_image: str) -> None:
    """실행기가 시작할 때 남은 전략 컨테이너를 지운다. 이름이 접두어로 시작하지 않으면 둔다."""
    leftover = NAME_PREFIX + "leftover-" + secrets.token_hex(4)
    unrelated = "other-" + NAME_PREFIX + secrets.token_hex(4)
    for name in (leftover, unrelated):
        subprocess.run(
            ["docker", "create", "--name", name, sandbox_image, "python", "-c", "pass"],
            capture_output=True,
            check=True,
        )
    try:
        assert asyncio.run(remove_leftovers()) == 1
        assert exists(leftover) is False
        assert exists(unrelated) is True
    finally:
        subprocess.run(
            ["docker", "rm", "-f", leftover, unrelated], capture_output=True, check=False
        )
