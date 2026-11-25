"""전략 코드를 격리 컨테이너에서 돌리고 이벤트마다 출력(목표 포지션)을 받아 온다(안전 불변식 12).

코드는 문자열 그대로 하네스에 넘기기만 한다. 이 프로세스에서는 실행하지도 파싱하지도 않는다.
docker run 인자는 상수다. 바뀌는 값은 이미지 이름과 무작위 컨테이너 이름뿐이고, 셸을 거치지 않는다.
체결 계산(strategies/backtest.py)을 대화 안에서 돌려 체결 이벤트를 만들고, 출력은 여기서 검사한다.
하네스는 전략 코드가 고칠 수 있는 프로세스라 그 검사를 믿지 않는다.
"""

import asyncio
import base64
import contextlib
import json
import logging
import secrets
from typing import cast

from nh_mcp.domain import JsonValue, Output, SandboxFailure, SandboxJob, SandboxResult
from nh_mcp.sandbox.wire import (
    MAX_DETAIL,
    BadOutput,
    event_to_wire,
    output_from_wire,
    preview,
)
from nh_mcp.strategies.backtest import Simulator, decision_time

NAME_PREFIX = "nhmcp-sbx-"
# 실행기 쪽 벽시계 마감. 컨테이너 안 CPU 한도(60초)보다 길다.
WALL_SECONDS = 70.0
# 하네스가 쓰는 프로토콜 줄 하나의 상한(64KiB). 표준 출력 스트림의 limit이기도 하다.
MAX_LINE = 65_536
# 표준 출력 합계 상한은 max(MAX_STDOUT, STDOUT_PER_EVENT * 넣은 이벤트 줄 수)다. 이벤트마다 목표를
# 내는 전략이 25,000 이벤트에서도 끝까지 돌게 넣은 만큼 늘린다. 설정·모델 줄은 늘리지 않는다.
MAX_STDOUT = 1_048_576
STDOUT_PER_EVENT = 256
# 표준 에러(전략 코드의 print도 이리로 온다)도 이만큼만 받는다. 넘으면 출력 폭주로 끊는다.
MAX_STDERR = 1_048_576
# docker kill · rm · inspect 한 번을 기다리는 시간.
DOCKER_CLI_SECONDS = 30.0
# CPU 한도(--ulimit cpu=60:60)는 soft와 hard가 같아 커널이 SIGKILL(128+9)을 먼저 보낸다.
# SIGXCPU(128+24)는 soft < hard일 때만 오지만 같이 받아 둔다. OOM도 137이라 OOM 표시를 먼저 본다.
CPU_EXIT_CODES = frozenset({"137", "152"})
# docker run이 컨테이너를 띄우지 못했을 때 내는 종료 코드(125 docker 오류, 126·127 실행 파일 문제).
DOCKER_START_CODES = frozenset({125, 126, 127})
# 모델 파일을 하네스에 넘기는 조각 하나의 바이트 수. base64로 한 줄 1MiB가 된다.
MODEL_CHUNK = 768 * 1024

_LOG = logging.getLogger(__name__)

# 고정 인자. 요청 값은 어느 것도 여기에 들어가지 않는다.
FIXED_ARGS = (
    "-i",
    "--network",
    "none",
    "--read-only",
    # 전략이 쓰는 16MiB에 모델 파일 자리(최대 50MiB, 하네스가 /tmp/model에 쓴다)를 더했다.
    # tmpfs는 컨테이너 메모리(512MB)에서 빠진다.
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
    # 파일 하나의 크기 상한. 모델 파일(최대 50MiB) 하나가 들어가야 한다. 쓰는 양의 합계는 위
    # tmpfs 크기가 묶는다(이미지는 읽기 전용).
    "--ulimit",
    "fsize=52428800:52428800",
    "-e",
    "PYTHONHASHSEED=0",
    "--workdir",
    "/sandbox",
    # 전략 출력을 호스트의 Docker 로그(기본 json-file)에 쌓지 않는다. 출력은 실행기가 읽어 버린다.
    "--log-driver",
    "none",
    # 이미지가 없으면 받으러 가지 않고 바로 실패한다.
    "--pull",
    "never",
)


def docker_argv(image: str, name: str) -> list[str]:
    return [
        "docker",
        "run",
        *FIXED_ARGS,
        "--name",
        name,
        image,
        "python",
        "-s",
        "/harness/harness.py",
    ]


def failure(status: SandboxFailure, index: int | None, detail: str) -> SandboxResult:
    return SandboxResult(status, [], index, detail[:MAX_DETAIL])


class ContainerStartError(Exception):
    """컨테이너가 하네스를 돌리지 못했다(이미지 없음, 데몬 오류 등). 전략 코드의 실패가 아니다.

    실행기는 이 예외를 HTTP 500으로 답하고, MCP 서버는 실행 기록을 남기지 않는다.
    """


class _OutputOverflowError(Exception):
    """표준 출력이 한도를 넘었다. 메시지는 실패 사유의 detail이다."""


class _HarnessEndedError(Exception):
    """하네스가 답 없이 끝났다(표준 출력 EOF 또는 표준 입력 파이프 끊김)."""


def _decode(line: bytes) -> dict[str, object] | None:
    try:
        value: object = json.loads(line)
    except (ValueError, RecursionError):
        # 전략 코드가 출구에 쓴 줄일 수 있다. 깊게 중첩된 줄은 RecursionError를 낸다.
        return None
    if not isinstance(value, dict):
        return None
    # JSON 객체의 키는 늘 문자열이다.
    return cast(dict[str, object], value)


def _harness_reason(value: object, loading: bool) -> SandboxFailure | None:
    """하네스가 알릴 수 있는 사유만 받는다.

    전략 코드가 프로토콜 출구에 거짓 줄을 써도 시간 초과·CPU·출력 같은 다른 사유는 못 만든다.
    """
    if not isinstance(value, str):
        return None
    match value:
        case "memory" | "processes" as reason:
            return reason
        case "invalid_code" as reason if loading:
            return reason
        case "invalid_output" | "exception" as reason if not loading:
            return reason
        case _:
            return None


def _streams(job: SandboxJob) -> dict[str, JsonValue]:
    return {symbol: [*sorted(names)] for symbol, names in sorted(job.streams.items())}


class _Conversation:
    """하네스와 한 줄씩 주고받는다. 읽은 양을 세고, 지금 처리하는 시장 이벤트를 적어 둔다."""

    def __init__(self, stdin: asyncio.StreamWriter, stdout: asyncio.StreamReader) -> None:
        self.stdin = stdin
        self.stdout = stdout
        # 지금 넣는 시장 이벤트의 순번. 그 이벤트가 낸 체결 이벤트를 넣는 동안에도 같다.
        self.index: int | None = None
        self.read_total = 0
        # 넣은 이벤트 줄(시장·체결) 수. 표준 출력 합계 상한이 이만큼 늘어난다.
        self.fed = 0
        # 하네스가 ready를 보냈는지. 그 전에 끝났다면 컨테이너가 아예 못 떴을 수도 있다.
        self.ready = False

    async def send(self, message: dict[str, object]) -> None:
        self.stdin.write(json.dumps(message).encode() + b"\n")
        try:
            await self.stdin.drain()
        except ConnectionError as exc:
            raise _HarnessEndedError from exc

    async def feed(self, event: JsonValue) -> None:
        await self.send({"event": event})
        self.fed += 1

    def count(self, size: int) -> None:
        self.read_total += size
        cap = max(MAX_STDOUT, STDOUT_PER_EVENT * self.fed)
        if self.read_total > cap:
            raise _OutputOverflowError(f"표준 출력이 한도({cap}바이트)를 넘었습니다")

    async def receive(self) -> dict[str, object] | None:
        try:
            line = await self.stdout.readuntil(b"\n")
        except asyncio.LimitOverrunError as exc:
            raise _OutputOverflowError("출력 한 줄이 64KiB를 넘었습니다") from exc
        except asyncio.IncompleteReadError as exc:
            self.count(len(exc.partial))
            raise _HarnessEndedError from exc
        self.count(len(line))
        return _decode(line)

    def refusal(self, message: dict[str, object] | None, loading: bool) -> SandboxResult:
        """출력(또는 ready) 대신 온 줄을 실패 사유로 바꾼다.

        detail에는 전략 코드가 낸 것(하네스 줄의 detail)만 담는다. 프로토콜 출구는 전략 코드도
        쓸 수 있어서다. 설명 문장은 MCP 서버가 붙이고, 전략 코드가 낸 부분을 표시한다.
        """
        if message is not None:
            reason = _harness_reason(message.get("error"), loading)
            if reason is not None:
                return failure(reason, self.index, str(message.get("detail", "")))
        # 읽지 못한 줄은 보여 주지 않는다.
        return failure("invalid_output", self.index, "")

    async def send_model(self, model: bytes) -> None:
        """모델 파일을 base64 조각 줄로 보내고 끝을 알린다. 전략 코드는 아직 불러오기 전이다."""
        view = memoryview(model)
        for start in range(0, len(model), MODEL_CHUNK):
            piece = base64.b64encode(view[start : start + MODEL_CHUNK]).decode("ascii")
            await self.send({"model_chunk": piece})
        await self.send({"model_end": True})

    async def run(self, job: SandboxJob) -> SandboxResult:
        setup = {"code": job.code, "params": job.params, "streams": _streams(job)}
        await self.send({**setup, "model": job.model is not None})
        if job.model is not None:
            await self.send_model(job.model)
        ready = await self.receive()
        if ready is None or ready.get("ready") is not True:
            return self.refusal(ready, loading=True)
        self.ready = True
        sim = Simulator(job.terms)
        outputs: list[Output] = []
        for index, event in enumerate(job.events):
            self.index = index
            # 이 이벤트에서 난 체결이 먼저, 이벤트 자신이 다음이다. 출력을 받기 전에는 다음
            # 이벤트를 보내지 않는다. 앞으로 올 이벤트는 컨테이너에 없다.
            for fed in [*sim.on_market(event), event]:
                await self.feed(event_to_wire(fed))
                answer = await self.receive()
                if answer is None or "output" not in answer:
                    return self.refusal(answer, loading=False)
                try:
                    output = output_from_wire(answer["output"], job)
                except BadOutput:
                    return failure("invalid_output", index, preview(answer["output"]))
                sim.on_output(output, decision_time(fed))
                outputs.append(output)
        self.stdin.close()
        return SandboxResult("ok", outputs, None, "")


async def _docker(*args: str) -> str | None:
    """docker CLI를 한 번 부르고 표준 출력을 돌려준다.

    정리 단계에서도 쓰므로 실패해도 예외를 내지 않는다(원래 결과나 예외를 덮지 않게).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), DOCKER_CLI_SECONDS)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        return None
    return out.decode(errors="replace") if proc.returncode == 0 else None


async def _discard(stream: asyncio.StreamReader) -> None:
    # 안 읽으면 파이프가 차서 컨테이너가 쓰다가 멈춘다.
    while await stream.read(65536):
        pass


class _ErrorStream:
    """표준 에러를 읽어 버리며 양을 센다. 한도를 넘으면 docker run을 끊는다.

    안 읽으면 파이프가 차서 컨테이너가 쓰다가 멈춘다. 끊긴 컨테이너는 마지막 정리(rm -f)가 지운다.
    """

    def __init__(self, stream: asyncio.StreamReader, proc: asyncio.subprocess.Process) -> None:
        self.stream = stream
        self.proc = proc
        self.total = 0

    @property
    def flooded(self) -> bool:
        return self.total > MAX_STDERR

    async def drain(self) -> None:
        cut = False
        while chunk := await self.stream.read(65536):
            self.total += len(chunk)
            if self.flooded and not cut:
                # docker run을 끊으면 표준 출력도 닫혀 대화가 바로 끝난다.
                cut = True
                with contextlib.suppress(ProcessLookupError):
                    self.proc.kill()


async def _exit_reason(name: str, talk: _Conversation, cli_code: int | None) -> SandboxResult:
    """하네스가 답 없이 끝났을 때 컨테이너 상태로 사유를 가른다.

    컨테이너가 하네스를 돌리지 못했으면(상태를 못 읽음, 데몬이 남긴 오류, ready 전 docker 오류
    종료 코드) 전략 사유로 답하지 않고 ContainerStartError를 낸다.
    """
    state = await _docker(
        "inspect", "-f", "{{.State.OOMKilled}} {{.State.ExitCode}} {{.State.Error}}", name
    )
    if state is None:
        raise ContainerStartError(
            f"전략 컨테이너 상태를 읽지 못했습니다(docker 종료 코드 {cli_code})"
        )
    oom, _, rest = state.strip().partition(" ")
    code, _, error = rest.partition(" ")
    if error:
        raise ContainerStartError(f"전략 컨테이너를 띄우지 못했습니다: {error[:200]}")
    # 전략 코드가 스스로 같은 코드로 끝냈으면(컨테이너 종료 코드와 같음) 코드의 실패다.
    if not talk.ready and cli_code in DOCKER_START_CODES and code != str(cli_code):
        raise ContainerStartError(f"docker run이 종료 코드 {cli_code}로 끝났습니다")
    index = talk.index
    if oom == "true":
        return failure("memory", index, "메모리 한도(512MB)를 넘었습니다")
    if code in CPU_EXIT_CODES:
        return failure("cpu", index, "CPU 시간 한도(60초)를 넘었습니다")
    detail = (
        f"전략 프로세스가 종료 코드 {code}로 끝났습니다" if code else "종료 코드를 알 수 없습니다"
    )
    return failure("exception", index, detail)


async def run_in_container(
    job: SandboxJob, image: str, wall_seconds: float = WALL_SECONDS
) -> SandboxResult:
    """전략 코드를 새 컨테이너 하나에서 돌린다. 끝나면 어떤 경우에도 컨테이너를 지운다."""
    name = NAME_PREFIX + secrets.token_hex(8)
    proc = await asyncio.create_subprocess_exec(
        *docker_argv(image, name),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=MAX_LINE,
    )
    stdin, stdout, stderr = proc.stdin, proc.stdout, proc.stderr
    if stdin is None or stdout is None or stderr is None:
        await _docker("rm", "-f", name)
        raise RuntimeError("docker run 파이프를 열지 못했습니다")
    errors = _ErrorStream(stderr, proc)
    stderr_drain = asyncio.create_task(errors.drain())
    talk = _Conversation(stdin, stdout)
    reply: SandboxResult | None = None
    try:
        try:
            # 마감은 하나다. 모든 쓰기·읽기가 남은 시간 안에서 돈다.
            async with asyncio.timeout(wall_seconds):
                try:
                    reply = await talk.run(job)
                except _HarnessEndedError:
                    # 컨테이너가 끝난 뒤에야 상태(OOM·종료 코드)가 확정된다.
                    stdin.close()
                    await proc.wait()
        except TimeoutError:
            await _docker("kill", name)
            reply = failure("timeout", talk.index, f"{wall_seconds:g}초 안에 끝나지 않았습니다")
        except _OutputOverflowError as exc:
            reply = failure("output", talk.index, str(exc))
        if reply is None:
            reply = await _exit_reason(name, talk, proc.returncode)
    finally:
        await _docker("rm", "-f", name)
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        # 다 읽어야 파이프가 닫히고 wait가 끝난다(출력 폭주로 읽기가 멈춘 경우도).
        await _discard(stdout)
        await stderr_drain
        await proc.wait()
    # 표준 에러는 끝까지 센 뒤에 본다. 한도를 넘었으면 다른 결과보다 먼저다.
    if errors.flooded:
        return failure("output", talk.index, "표준 에러가 1MB를 넘었습니다")
    return reply


async def remove_leftovers() -> int:
    """지난 실행기가 남긴 전략 컨테이너(이름이 NAME_PREFIX로 시작)를 지운다. 지운 수를 돌려준다.

    실행기가 시작할 때 한 번 부른다. 실패해도 시작을 막지 않는다(로그만 남긴다).
    """
    listing = await _docker("ps", "-aq", "--filter", f"name=^{NAME_PREFIX}")
    if listing is None:
        _LOG.warning("남은 전략 컨테이너 목록을 읽지 못했습니다")
        return 0
    ids = listing.split()
    if not ids:
        return 0
    if await _docker("rm", "-f", *ids) is None:
        _LOG.warning("남은 전략 컨테이너 %d개를 지우지 못했습니다", len(ids))
        return 0
    _LOG.info("남은 전략 컨테이너 %d개를 지웠습니다", len(ids))
    return len(ids)
