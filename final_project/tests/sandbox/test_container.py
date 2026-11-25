"""실행기와 하네스가 프로토콜대로 주고받는지. 실제 Docker로 돈다."""

import asyncio
import hashlib
import os
import secrets
import subprocess
import sys
import time
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from nh_mcp.domain import (
    MAX_MODEL_BYTES,
    BarEvent,
    Candle,
    JsonValue,
    MarketEvent,
    SandboxJob,
    SandboxResult,
    Target,
)
from nh_mcp.sandbox import container
from nh_mcp.sandbox.container import (
    FIXED_ARGS,
    NAME_PREFIX,
    ContainerStartError,
    docker_argv,
    remove_leftovers,
    run_in_container,
)
from nh_mcp.sandbox.wire import MAX_DETAIL
from nh_mcp.strategies.backtest import simulate_events
from nh_mcp.strategies.examples import MEAN_REVERSION, MEAN_REVERSION_PARAMS

from .wire import SYMBOL, T0, TERMS, bars, job

pytestmark = pytest.mark.docker

WALL = 8.0


def run(work: SandboxJob, image: str, wall_seconds: float = WALL) -> SandboxResult:
    return asyncio.run(run_in_container(work, image, wall_seconds=wall_seconds))


def test_argv_is_constant() -> None:
    assert docker_argv("img", "nhmcp-sbx-abc") == [
        "docker",
        "run",
        "-i",
        "--network",
        "none",
        "--read-only",
        "--tmpfs",
        "/tmp:size=66m,noexec,nosuid",
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
        "fsize=52428800:52428800",
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


# 모델 파일 전(M4a U4까지)의 고정 인자. 모델 파일 때문에 바뀐 것은 아래 둘뿐이어야 한다.
ARGS_BEFORE_MODELS = (
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
)
MIB = 1024 * 1024


def test_model_files_changed_only_the_tmp_size_and_the_file_size_cap() -> None:
    assert len(FIXED_ARGS) == len(ARGS_BEFORE_MODELS)
    changed = {
        before: after
        for before, after in zip(ARGS_BEFORE_MODELS, FIXED_ARGS, strict=True)
        if before != after
    }
    assert changed == {
        "/tmp:size=16m,noexec,nosuid": "/tmp:size=66m,noexec,nosuid",
        "fsize=16777216:16777216": "fsize=52428800:52428800",
    }
    # /tmp는 예전 16MiB에 모델 상한을 더한 만큼, 파일 하나는 모델 상한까지다.
    assert 66 * MIB == 16 * MIB + MAX_MODEL_BYTES
    assert MAX_MODEL_BYTES == 52428800
    # 메모리 한도는 그대로다(tmpfs도 이 안에서 쓴다).
    assert FIXED_ARGS[FIXED_ARGS.index("--memory") + 1] == "512m"


def closes(prices: list[str]) -> list[MarketEvent]:
    """시가=고가=저가=종가인 A의 1분봉."""
    out: list[MarketEvent] = []
    for i, text in enumerate(prices):
        p = Decimal(text)
        out.append(BarEvent(SYMBOL, "minute", Candle(T0 + timedelta(minutes=i), p, p, p, p, 1)))
    return out


def test_runs_example_and_outputs_line_up_with_fills(sandbox_image: str) -> None:
    # 평균 100인 봉 20개 뒤 97에서 사고(98.05에 체결), 100에서 정리한다(99.95에 체결).
    events = closes(["99", "101"] * 10 + ["97", "98", "100", "100"])
    reply = run(job(MEAN_REVERSION, events, MEAN_REVERSION_PARAMS), sandbox_image)
    assert reply.status == "ok", reply
    assert reply.index is None
    # 봉 24개와 체결 이벤트 2개에 출력이 하나씩이다.
    assert len(reply.outputs) == 26
    report = simulate_events(events, reply.outputs, TERMS)
    assert (report.fills, len(report.trades)) == (2, 1)
    assert report.net_pnl == (Decimal("99.95") - Decimal("98.05")) * 250000


SEEDED_CHOICE = """
import random

def on_event(event, ctx):
    # 문자열 set의 순회 순서는 PYTHONHASHSEED에 달려 있다.
    names = [name for name in {"long", "short", "flat", "keep"}]
    pick = names[int(random.random() * len(names))]
    if pick == "keep":
        return None
    return {"A": {"position": {"long": 1, "short": -1, "flat": 0}[pick]}}
"""


def test_same_input_gives_same_outputs(sandbox_image: str) -> None:
    first = run(job(SEEDED_CHOICE, bars(10)), sandbox_image)
    second = run(job(SEEDED_CHOICE, bars(10)), sandbox_image)
    assert first.status == "ok", first
    assert first.outputs == second.outputs
    assert any(o is not None for o in first.outputs)


NUMPY_CHOICE = """
import numpy
import pandas

def on_event(event, ctx):
    if event["type"] != "bar":
        return None
    history = ctx.history("A", "minute")
    frame = pandas.DataFrame(history + [event])
    assert len(frame) == len(history) + 1
    return {"A": {"position": int(numpy.random.randint(-1, 2))}}
"""


def test_numpy_and_pandas_are_importable(sandbox_image: str) -> None:
    first = run(job(NUMPY_CHOICE, bars(10)), sandbox_image)
    second = run(job(NUMPY_CHOICE, bars(10)), sandbox_image)
    assert first.status == "ok", first
    assert first.outputs == second.outputs


def test_korean_comments_run(sandbox_image: str) -> None:
    code = "# 한글 주석: 늘 그대로 둔다\ndef on_event(event, ctx):\n    return None  # 유지\n"
    reply = run(job(code), sandbox_image)
    assert reply == SandboxResult("ok", [None, None, None], None, "")


def test_print_does_not_break_the_protocol(sandbox_image: str) -> None:
    code = 'def on_event(event, ctx):\n    print("hello")\n    return None\n'
    reply = run(job(code), sandbox_image)
    assert reply == SandboxResult("ok", [None, None, None], None, "")


INIT_ONCE = """
CALLS = []

def init(ctx):
    CALLS.append(("init", dict(ctx.params), ctx.positions, ctx.model_path))

def on_event(event, ctx):
    CALLS.append("event")
    # 첫 이벤트에서 init이 정확히 한 번 먼저 돌았으면 지금 포지션(0)을 목표로 낸다(주문 없음).
    if CALLS == [("init", {"w": 3}, {"A": 0}, None), "event"]:
        return {"A": {"position": 0}}
    return None
"""


def test_init_runs_once_with_params_zero_positions_and_no_model(sandbox_image: str) -> None:
    reply = run(job(INIT_ONCE, params={"w": 3}), sandbox_image)
    assert reply == SandboxResult("ok", [{SYMBOL: Target(0)}, None, None], None, "")


# init에서 모델 파일을 읽어 내용의 해시·크기·권한이 params와 맞는지 본다. 틀리면 init이 예외를
# 내 invalid_code로 끝난다. 이벤트마다 같은 경로가 보이는지도 본다.
READS_MODEL = """
import hashlib
import os

def init(ctx):
    assert ctx.model_path == "/tmp/model", ctx.model_path
    with open(ctx.model_path, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    assert digest == ctx.params["sha256"], digest
    info = os.stat(ctx.model_path)
    assert info.st_size == ctx.params["size"], info.st_size
    assert info.st_mode & 0o777 == 0o400, oct(info.st_mode)

def on_event(event, ctx):
    assert ctx.model_path == "/tmp/model"
    return None
"""


def model_job(model: bytes) -> SandboxJob:
    params: dict[str, JsonValue] = {"sha256": hashlib.sha256(model).hexdigest(), "size": len(model)}
    return replace(job(READS_MODEL, params=params), model=model)


def test_model_arrives_whole_at_model_path_before_init(sandbox_image: str) -> None:
    # 조각(768KiB) 경계를 넘고 마지막 조각이 짧은 크기.
    model = os.urandom(3 * container.MODEL_CHUNK + 12345)
    reply = run(model_job(model), sandbox_image)
    assert reply == SandboxResult("ok", [None, None, None], None, ""), reply


def test_largest_model_fits_tmp_and_the_file_size_cap(sandbox_image: str) -> None:
    reply = run(model_job(os.urandom(MAX_MODEL_BYTES)), sandbox_image, wall_seconds=60.0)
    assert reply == SandboxResult("ok", [None, None, None], None, ""), reply


@pytest.mark.parametrize(
    "code",
    [
        "def on_event(event, ctx)\n    return None\n",
        "def other(event, ctx):\n    return None\n",
        "def on_event(event):\n    return None\n",
        "on_event = 1\n",
        "def on_event(event, ctx):\n    return None\n\ndef init():\n    pass\n",
        "def on_event(event, ctx):\n    return None\n\ninit = 1\n",
        "def on_event(event, ctx):\n    return None\n\ndef init(ctx):\n    raise ValueError('x')\n",
    ],
    ids=[
        "syntax",
        "missing",
        "one-arg",
        "not-callable",
        "init-no-arg",
        "init-value",
        "init-raises",
    ],
)
def test_bad_code_is_invalid_code(sandbox_image: str, code: str) -> None:
    reply = run(job(code), sandbox_image)
    assert reply.status == "invalid_code", reply
    assert reply.outputs == []
    assert reply.index is None


BAD_OUTPUTS = [
    pytest.param('{"B": {"position": 1}}', id="undeclared-symbol"),
    pytest.param('{"A": {"position": True}}', id="bool-position"),
    pytest.param('{"A": {"position": 1.0}}', id="float-position"),
    pytest.param('{"A": {"position": 2}}', id="over-max"),
    pytest.param('{"A": {"position": -2}}', id="over-max-short"),
    pytest.param('{"A": {"position": 1, "price": "1e3"}}', id="exponent-price"),
    pytest.param('{"A": {"position": 1, "price": 350.05}}', id="float-price"),
    pytest.param('{"A": {"position": 1, "price": "350.03"}}', id="off-tick"),
    pytest.param('{"A": {"position": 1, "price": "0"}}', id="zero-price"),
    pytest.param('{"A": {"position": 1, "price": "-350"}}', id="negative-price"),
    pytest.param('{"A": {"price": "350"}}', id="no-position"),
    pytest.param('{"A": {"position": 1, "qty": 1}}', id="extra-key"),
    pytest.param('{"A": 1}', id="target-not-dict"),
    pytest.param('"buy"', id="string"),
    pytest.param("[1]", id="list"),
]


@pytest.mark.parametrize("value", BAD_OUTPUTS)
def test_output_outside_the_contract_is_invalid_output(sandbox_image: str, value: str) -> None:
    # 두 번째 봉(index 1)에서 낸다.
    code = (
        f"def on_event(event, ctx):\n    if ctx.history('A', 'minute'):\n        return {value}\n"
    )
    reply = run(job(code), sandbox_image)
    assert reply.status == "invalid_output", reply
    assert reply.index == 1
    assert reply.outputs == []


def test_invalid_output_detail_is_the_value_the_strategy_gave(sandbox_image: str) -> None:
    # detail에는 전략 코드가 낸 값만 담는다(설명 문장은 서비스가 붙인다).
    code = 'def on_event(event, ctx):\n    return {"B": {"position": True}}\n'
    reply = run(job(code), sandbox_image)
    assert (reply.status, reply.index) == ("invalid_output", 0)
    assert reply.detail == '{"B": {"position": true}}'


def test_unserializable_output_is_invalid_output(sandbox_image: str) -> None:
    code = "def on_event(event, ctx):\n    return object()\n"
    reply = run(job(code), sandbox_image)
    assert (reply.status, reply.index, reply.detail) == ("invalid_output", 0, "object")


def test_outputs_inside_the_contract_pass(sandbox_image: str) -> None:
    code = (
        "def on_event(event, ctx):\n"
        "    if ctx.history('A', 'minute'):\n"
        "        return {'A': {'position': -1, 'price': '999.95'}}\n"
        "    return {'A': {'position': 1, 'price': None}}\n"
    )
    reply = run(job(code, bars(2)), sandbox_image)
    assert reply.status == "ok", reply
    # 시장가 매수가 봉1에서 체결되고, 체결 이벤트와 봉1 모두에 지정가 매도 목표를 낸다.
    assert reply.outputs == [
        {SYMBOL: Target(1)},
        {SYMBOL: Target(-1, Decimal("999.95"))},
        {SYMBOL: Target(-1, Decimal("999.95"))},
    ]


FOLLOWS_FILLS = """
FILL_KEYS = {"type", "symbol", "at", "side", "qty", "price"}

def on_event(event, ctx):
    held = ctx.positions["A"]
    if event["type"] == "fill":
        first = (
            set(event) == FILL_KEYS
            and (event["side"], event["qty"], event["price"]) == ("buy", 1, "351.05")
            and event["at"] == "2026-11-02T09:01:00+09:00"
        )
        # 체결 이벤트에도 출력을 낼 수 있다. 첫 체결을 본 뒤 한 계약 더 산다.
        return {"A": {"position": 2}} if first and held == 1 else None
    if not ctx.history("A", "minute"):
        return {"A": {"position": 1}}
    return None
"""


def test_fill_reaches_the_strategy_first_and_its_output_is_applied(sandbox_image: str) -> None:
    events = bars(4)
    reply = run(job(FOLLOWS_FILLS, events, max_position=2), sandbox_image)
    assert reply.status == "ok", reply
    # 봉0 → 목표 1. 봉1에서 351.05에 체결 → 체결 이벤트가 봉1보다 먼저 가고, ctx.positions가 1이라
    # 목표 2를 낸다. 그 목표가 봉2에서 352.05에 체결된다(두 번째 체결 이벤트).
    assert reply.outputs == [{SYMBOL: Target(1)}, {SYMBOL: Target(2)}, None, None, None, None]
    report = simulate_events(events, reply.outputs, TERMS)
    [held] = report.open_positions
    assert (report.fills, held.quantity, held.entry_price) == (2, 2, Decimal("351.55"))


def test_error_on_a_fill_event_reports_the_market_event_index(sandbox_image: str) -> None:
    code = (
        "def on_event(event, ctx):\n"
        "    if event['type'] == 'fill':\n"
        "        raise ValueError('체결')\n"
        "    return {'A': {'position': 1}}\n"
    )
    reply = run(job(code), sandbox_image)
    assert reply.status == "exception", reply
    # 봉0의 목표가 봉1에서 체결된다. 그 체결 이벤트에서 난 오류는 봉1(index 1)의 것이다.
    assert reply.index == 1
    assert "ValueError" in reply.detail


def test_exception_reports_type_and_event(sandbox_image: str) -> None:
    code = (
        "def on_event(event, ctx):\n"
        "    if len(ctx.history('A', 'minute')) == 1:\n"
        "        raise ZeroDivisionError('가' * 2000)\n"
        "    return None\n"
    )
    reply = run(job(code), sandbox_image)
    assert reply.status == "exception", reply
    assert reply.index == 1
    assert "ZeroDivisionError" in reply.detail
    assert len(reply.detail) <= MAX_DETAIL


def test_self_exit_is_exception_with_exit_code(sandbox_image: str) -> None:
    code = "import os\n\ndef on_event(event, ctx):\n    os._exit(3)\n"
    reply = run(job(code), sandbox_image)
    assert reply.status == "exception", reply
    assert "3" in reply.detail
    assert reply.index == 0


def leftover_containers() -> str:
    listing = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name={NAME_PREFIX}", "-q"],
        capture_output=True,
        text=True,
        check=True,
    )
    return listing.stdout.strip()


def test_no_container_is_left_behind(sandbox_image: str) -> None:
    ok = run(job("def on_event(event, ctx):\n    return None\n"), sandbox_image)
    stuck = run(
        job("def on_event(event, ctx):\n    while True:\n        pass\n"),
        sandbox_image,
        wall_seconds=4.0,
    )
    broken = run(job("def on_event(event, ctx):\n    return 1 / 0\n"), sandbox_image)
    assert (ok.status, stuck.status, broken.status) == ("ok", "timeout", "exception")
    assert leftover_containers() == ""


# 줄 단위로 정확히 읽는 가짜 하네스. 이벤트를 받을 때마다 다음 이벤트가 벌써 와 있는지 본다.
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
    os.write(1, b'{"output": {"A": {"position": 1}}}\\n' if early else b'{"output": null}\\n')
"""


def test_next_event_is_sent_only_after_the_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(EAGER_PROBE, encoding="utf-8")

    # 컨테이너 대신 가짜 하네스를 띄운다. 실행기의 주고받는 순서만 본다.
    def probe_argv(image: str, name: str) -> list[str]:
        return [sys.executable, "-I", str(probe)]

    monkeypatch.setattr(container, "docker_argv", probe_argv)
    reply = run(job(""), "unused")
    assert reply == SandboxResult("ok", [None, None, None], None, "")


def test_cpu_limit_is_reported_as_cpu(sandbox_image: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # 60초를 기다리지 않게 CPU 한도만 1초로 줄인다. soft와 hard가 같으면 커널은 SIGXCPU(152)
    # 대신 SIGKILL(137)을 보낸다. OOM 표시가 없는 137을 cpu로 읽는지 본다.
    shortened = tuple("cpu=1:1" if arg == "cpu=60:60" else arg for arg in container.FIXED_ARGS)
    assert shortened != container.FIXED_ARGS
    monkeypatch.setattr(container, "FIXED_ARGS", shortened)
    code = "def on_event(event, ctx):\n    while True:\n        pass\n"
    reply = run(job(code, bars(2)), sandbox_image)
    assert reply.status == "cpu", reply
    assert reply.index == 0


MISSING_IMAGE = "nh-mcp-sandbox:missing-tag"


def test_missing_image_is_a_start_error_not_a_strategy_failure() -> None:
    """이미지가 없으면 코드가 돌지 못했다. 전략 사유가 아니라 예외다(받으러 가지도 않는다)."""
    started = time.monotonic()
    with pytest.raises(ContainerStartError):
        run(job("def on_event(event, ctx):\n    return None\n"), MISSING_IMAGE)
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
