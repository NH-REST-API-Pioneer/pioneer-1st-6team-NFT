"""전략 코드를 격리 컨테이너에서 돌리고 이벤트마다 출력(목표 포지션)을 받아 온다(안전 불변식 12).

코드는 문자열 그대로 하네스에 넘기기만 한다. 이 프로세스에서는 실행하지도 파싱하지도 않는다.
docker run 인자는 상수다. 바뀌는 값은 이미지 이름과 무작위 컨테이너 이름뿐이고, 셸을 거치지 않는다.
출력은 여기서 검사한다. 하네스는 전략 코드가 고칠 수 있는 프로세스라 그 검사를 믿지 않는다.

두 가지로 돈다.
- 백테스트(run_in_container): 실행마다 컨테이너 하나. 체결 계산(strategies/backtest.py)을 대화
  안에서 돌려 체결 이벤트를 만든다.
- 띄워 두는 컨테이너(ResidentSession, M4b 자동 실행): 전략마다 하나를 띄워 두고 이벤트를 하나씩
  넣는다. 체결 계산은 없고 체결 이벤트도 부르는 쪽이 넣는다. 누적 CPU 한도 대신 이벤트마다 2초
  마감이다.
"""

import asyncio
import base64
import contextlib
import json
import logging
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import cast

from nh_mcp.domain import (
    JsonValue,
    Output,
    SandboxFailure,
    SandboxJob,
    SandboxResult,
    SessionFailed,
    SessionJob,
    StrategyEvent,
    Streams,
)
from nh_mcp.sandbox.wire import (
    MAX_DETAIL,
    BadOutput,
    event_to_wire,
    output_from_wire,
    preview,
)
from nh_mcp.strategies.backtest import Simulator, decision_time

NAME_PREFIX = "nhmcp-sbx-"
# 띄워 두는 컨테이너의 이름 접두어. 실행기가 다시 시작하면 이 이름도 모두 지운다.
RESIDENT_PREFIX = "nhmcp-run-"
# 실행기 쪽 벽시계 마감. 컨테이너 안 CPU 한도(60초)보다 길다.
WALL_SECONDS = 70.0
# 하네스가 쓰는 프로토콜 줄 하나의 상한(64KiB). 표준 출력 스트림의 limit이기도 하다.
MAX_LINE = 65_536
# 표준 출력 합계 상한은 max(MAX_STDOUT, STDOUT_PER_EVENT * 넣은 이벤트 줄 수)다. 이벤트마다 목표를
# 내는 전략이 25,000 이벤트에서도 끝까지 돌게 넣은 만큼 늘린다. 설정·모델 줄은 늘리지 않는다.
MAX_STDOUT = 1_048_576
STDOUT_PER_EVENT = 256
# 표준 에러(전략 코드의 print도 이리로 온다)도 같은 규칙이다:
# max(MAX_STDERR, STDOUT_PER_EVENT * 넣은 이벤트 줄 수). 띄워 둔 컨테이너는 며칠 돌므로 고정 1MiB면
# 이벤트마다 print하는 전략이 죽는다(사람 승인 2026-11-26). 넘으면 출력 폭주로 끊는다.
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
# 띄워 둔 컨테이너가 이벤트 하나에 답하는 시간(사람 승인 2026-11-25, 불변식 12).
EVENT_SECONDS = 2.0
# 띄워 두는 컨테이너를 여는 데(시작, 모델 파일 전송, 코드 불러오기·init) 쓰는 벽시계 마감. 누적 CPU
# 한도가 없어서 init이 쓰는 CPU는 이것이 묶는다. 백테스트의 CPU 한도(60초, --cpus 1)와 같은 크기다.
OPEN_SECONDS = 60.0
STDERR_FLOOD = "표준 에러가 한도를 넘었습니다"

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
# 띄워 두는 컨테이너(M4b 자동 실행)의 고정 인자. 위 인자에서 누적 CPU 한도 한 쌍
# (--ulimit cpu=60:60)만 뺐다. 며칠씩 도는 컨테이너라 누적 한도가 있으면 언젠가 반드시 죽는다. 순간
# 사용량은 --cpus 1이, 답하는 시간은 이벤트마다 2초 마감(EVENT_SECONDS)이 묶는다(사람 승인
# 2026-11-25, 불변식 12).
_CPU_LIMIT_AT = FIXED_ARGS.index("cpu=60:60") - 1
RESIDENT_ARGS = FIXED_ARGS[:_CPU_LIMIT_AT] + FIXED_ARGS[_CPU_LIMIT_AT + 2 :]


def _argv(args: tuple[str, ...], image: str, name: str) -> list[str]:
    return ["docker", "run", *args, "--name", name, image, "python", "-s", "/harness/harness.py"]


def docker_argv(image: str, name: str) -> list[str]:
    return _argv(FIXED_ARGS, image, name)


def resident_argv(image: str, name: str) -> list[str]:
    return _argv(RESIDENT_ARGS, image, name)


def failure(status: SandboxFailure, index: int | None, detail: str) -> SandboxResult:
    return SandboxResult(status, [], index, detail[:MAX_DETAIL])


def _failed(reason: SandboxFailure, detail: str) -> SessionFailed:
    return SessionFailed(reason, detail[:MAX_DETAIL])


class ContainerStartError(Exception):
    """컨테이너가 하네스를 돌리지 못했다(이미지 없음, 데몬 오류 등). 전략 코드의 실패가 아니다.

    실행기는 이 예외를 HTTP 500으로 답하고, MCP 서버는 실행 기록을 남기지 않는다.
    """


class _OutputOverflowError(Exception):
    """표준 출력이 한도를 넘었다. 메시지는 실패 사유의 detail이다."""


class _HarnessEndedError(Exception):
    """하네스가 답 없이 끝났다(표준 출력 EOF 또는 표준 입력 파이프 끊김)."""


class SessionClosedError(Exception):
    """닫은(또는 넣는 동안 다른 쪽이 닫은) 세션에 이벤트를 넣었다. 컨테이너는 이미 없다."""


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


def _streams(streams: Streams) -> dict[str, JsonValue]:
    return {symbol: [*sorted(names)] for symbol, names in sorted(streams.items())}


@dataclass(frozen=True)
class _Refusal:
    """출력(또는 ready) 대신 받은 실패. detail은 전략 코드가 낸 것이거나 실행기의 설명이다."""

    reason: SandboxFailure
    detail: str


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

    def refusal(self, message: dict[str, object] | None, loading: bool) -> _Refusal:
        """출력(또는 ready) 대신 온 줄을 실패 사유로 바꾼다.

        detail에는 전략 코드가 낸 것(하네스 줄의 detail)만 담는다. 프로토콜 출구는 전략 코드도
        쓸 수 있어서다. 설명 문장은 MCP 서버가 붙이고, 전략 코드가 낸 부분을 표시한다.
        """
        if message is not None:
            reason = _harness_reason(message.get("error"), loading)
            if reason is not None:
                return _Refusal(reason, str(message.get("detail", "")))
        # 읽지 못한 줄은 보여 주지 않는다.
        return _Refusal("invalid_output", "")

    async def send_model(self, model: bytes) -> None:
        """모델 파일을 base64 조각 줄로 보내고 끝을 알린다. 전략 코드는 아직 불러오기 전이다."""
        view = memoryview(model)
        for start in range(0, len(model), MODEL_CHUNK):
            piece = base64.b64encode(view[start : start + MODEL_CHUNK]).decode("ascii")
            await self.send({"model_chunk": piece})
        await self.send({"model_end": True})

    async def start(self, job: SandboxJob | SessionJob) -> _Refusal | None:
        """설정과 모델 파일을 보내고 ready를 기다린다. 하네스가 거절하면 그 사유."""
        setup = {"code": job.code, "params": job.params, "streams": _streams(job.streams)}
        await self.send({**setup, "model": job.model is not None})
        if job.model is not None:
            await self.send_model(job.model)
        ready = await self.receive()
        if ready is None or ready.get("ready") is not True:
            return self.refusal(ready, loading=True)
        self.ready = True
        return None

    async def answer(self, event: StrategyEvent, job: SandboxJob | SessionJob) -> Output | _Refusal:
        """이벤트 하나를 넣고 출력 하나를 받아 이 일의 계약대로 검사한다."""
        await self.feed(event_to_wire(event))
        answer = await self.receive()
        if answer is None or "output" not in answer:
            return self.refusal(answer, loading=False)
        try:
            return output_from_wire(answer["output"], job)
        except BadOutput:
            return _Refusal("invalid_output", preview(answer["output"]))

    async def run(self, job: SandboxJob) -> SandboxResult:
        refused = await self.start(job)
        if refused is not None:
            return failure(refused.reason, self.index, refused.detail)
        sim = Simulator(job.terms)
        outputs: list[Output] = []
        for index, event in enumerate(job.events):
            self.index = index
            # 이 이벤트에서 난 체결이 먼저, 이벤트 자신이 다음이다. 출력을 받기 전에는 다음
            # 이벤트를 보내지 않는다. 앞으로 올 이벤트는 컨테이너에 없다.
            for fed in [*sim.on_market(event), event]:
                output = await self.answer(fed, job)
                if isinstance(output, _Refusal):
                    return failure(output.reason, index, output.detail)
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

    def __init__(
        self,
        stream: asyncio.StreamReader,
        proc: asyncio.subprocess.Process,
        fed: Callable[[], int],
    ) -> None:
        self.stream = stream
        self.proc = proc
        # 지금까지 넣은 이벤트 줄 수. 한도가 이만큼 늘어난다.
        self.fed = fed
        self.total = 0

    @property
    def flooded(self) -> bool:
        return self.total > max(MAX_STDERR, STDOUT_PER_EVENT * self.fed())

    async def drain(self) -> None:
        cut = False
        while chunk := await self.stream.read(65536):
            self.total += len(chunk)
            if self.flooded and not cut:
                # docker run을 끊으면 표준 출력도 닫혀 대화가 바로 끝난다.
                cut = True
                with contextlib.suppress(ProcessLookupError):
                    self.proc.kill()


async def _exit_reason(name: str, ready: bool, cli_code: int | None, cpu_limit: bool) -> _Refusal:
    """하네스가 답 없이 끝났을 때 컨테이너 상태로 사유를 가른다.

    컨테이너가 하네스를 돌리지 못했으면(상태를 못 읽음, 데몬이 남긴 오류, ready 전 docker 오류
    종료 코드) 전략 사유로 답하지 않고 ContainerStartError를 낸다. cpu_limit은 누적 CPU 한도가 걸린
    컨테이너(백테스트)인지다. 띄워 둔 컨테이너에는 한도가 없어 137을 CPU로 읽지 않는다.
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
    if not ready and cli_code in DOCKER_START_CODES and code != str(cli_code):
        raise ContainerStartError(f"docker run이 종료 코드 {cli_code}로 끝났습니다")
    if oom == "true":
        return _Refusal("memory", "메모리 한도(512MB)를 넘었습니다")
    if cpu_limit and code in CPU_EXIT_CODES:
        return _Refusal("cpu", "CPU 시간 한도(60초)를 넘었습니다")
    detail = (
        f"전략 프로세스가 종료 코드 {code}로 끝났습니다" if code else "종료 코드를 알 수 없습니다"
    )
    return _Refusal("exception", detail)


class _Box:
    """docker run 프로세스 하나와 그 파이프. 표준 에러는 처음부터 끝까지 읽어 버리며 센다."""

    def __init__(self, name: str, proc: asyncio.subprocess.Process) -> None:
        stdin, stdout, stderr = proc.stdin, proc.stdout, proc.stderr
        if stdin is None or stdout is None or stderr is None:
            raise RuntimeError("docker run 파이프를 열지 못했습니다")
        self.name = name
        self.proc = proc
        self.talk = _Conversation(stdin, stdout)
        self.errors = _ErrorStream(stderr, proc, lambda: self.talk.fed)
        self.draining = asyncio.create_task(self.errors.drain())

    async def kill(self) -> None:
        """컨테이너를 지우고 docker run을 끊는다. 이것이 끝나면 컨테이너는 남아 있지 않다."""
        await _docker("rm", "-f", self.name)
        if self.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.proc.kill()

    async def reap(self) -> None:
        """kill 뒤에 부른다. 표준 출력을 읽으므로 대화(feed)와 겹치게 부르면 안 된다.

        다 읽어야 파이프가 닫히고 wait가 끝난다(출력 폭주로 읽기가 멈춘 경우도).
        """
        await _discard(self.talk.stdout)
        await self.draining
        await self.proc.wait()


async def _launch(argv: list[str], name: str) -> _Box:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=MAX_LINE,
    )
    try:
        return _Box(name, proc)
    except RuntimeError:
        await _docker("rm", "-f", name)
        raise


async def run_in_container(
    job: SandboxJob, image: str, wall_seconds: float = WALL_SECONDS
) -> SandboxResult:
    """전략 코드를 새 컨테이너 하나에서 돌린다. 끝나면 어떤 경우에도 컨테이너를 지운다."""
    name = NAME_PREFIX + secrets.token_hex(8)
    box = await _launch(docker_argv(image, name), name)
    talk, proc = box.talk, box.proc
    reply: SandboxResult | None = None
    try:
        try:
            # 마감은 하나다. 모든 쓰기·읽기가 남은 시간 안에서 돈다.
            async with asyncio.timeout(wall_seconds):
                try:
                    reply = await talk.run(job)
                except _HarnessEndedError:
                    # 컨테이너가 끝난 뒤에야 상태(OOM·종료 코드)가 확정된다.
                    talk.stdin.close()
                    await proc.wait()
        except TimeoutError:
            await _docker("kill", name)
            reply = failure("timeout", talk.index, f"{wall_seconds:g}초 안에 끝나지 않았습니다")
        except _OutputOverflowError as exc:
            reply = failure("output", talk.index, str(exc))
        if reply is None:
            ended = await _exit_reason(name, talk.ready, proc.returncode, cpu_limit=True)
            reply = failure(ended.reason, talk.index, ended.detail)
    finally:
        await box.kill()
        await box.reap()
    # 표준 에러는 끝까지 센 뒤에 본다. 한도를 넘었으면 다른 결과보다 먼저다.
    if box.errors.flooded:
        return failure("output", talk.index, STDERR_FLOOD)
    return reply


class ResidentSession:
    """띄워 둔 전략 컨테이너 하나(M4b 자동 실행). 이벤트를 하나 넣고 출력을 하나 받는다.

    체결 계산은 없다. 체결 이벤트는 부르는 쪽(실시간이면 NH 체결)이 넣는다. 이벤트 하나에 2초 안에
    답하지 않거나, 출력이 계약 밖이거나, 하네스가 오류를 알리거나, 컨테이너가 죽거나 한도(메모리·
    프로세스·출력)를 넘으면 컨테이너를 지우고 사유(SessionFailed)를 돌려준다. 그 뒤로는 쓸 수 없다.
    close는 몇 번 불러도 되고, 넣는 중인 이벤트가 있어도 컨테이너를 바로 지운다. 취소를 포함해 어느
    길로 끝나도 컨테이너를 남기지 않는다. 한 번에 이벤트 하나만 넣는다(겹치는 것은 실행기가 막는다).
    """

    def __init__(self, box: _Box, contract: SessionJob) -> None:
        self._box = box
        # 출력 검사용 계약. 모델 파일(수십 MB)은 보낸 뒤 들고 있지 않는다.
        self._contract = contract
        # 대화(표준 출력 읽기)가 없을 때 선다. 닫기는 이것을 기다린 뒤에 파이프를 비운다.
        self._idle = asyncio.Event()
        self._idle.set()
        self._closing: asyncio.Task[None] | None = None

    @property
    def name(self) -> str:
        return self._box.name

    async def feed(self, event: StrategyEvent) -> Output | SessionFailed:
        """이벤트 하나를 넣고 출력 하나를 받는다. 닫은 세션이면 SessionClosedError."""
        return await self._converse(lambda: self._box.talk.answer(event, self._contract))

    async def close(self) -> None:
        if self._closing is None:
            self._closing = asyncio.create_task(self._shutdown())
        # 부른 쪽이 취소돼도 닫기는 끝까지 간다.
        await asyncio.shield(self._closing)

    async def _shutdown(self) -> None:
        await self._box.kill()
        # 넣는 중인 이벤트가 있으면 그 읽기가 끝나기를 기다린다. 컨테이너가 없어서 곧 끝난다.
        await self._idle.wait()
        await self._box.reap()

    @classmethod
    async def open(
        cls, job: SessionJob, image: str, open_seconds: float = OPEN_SECONDS
    ) -> "ResidentSession | SessionFailed":
        """컨테이너를 하나 띄우고 코드를 불러 ready까지 받는다. 못 열면 컨테이너를 지우고 사유.

        컨테이너가 하네스를 돌리지 못하면(이미지 없음 등) ContainerStartError를 낸다.
        """
        name = RESIDENT_PREFIX + secrets.token_hex(8)
        session = cls(await _launch(resident_argv(image, name), name), replace(job, model=None))

        async def ready() -> _Refusal | None:
            return await session._box.talk.start(job)

        refused = await session._converse(ready, open_seconds)
        return session if refused is None else refused

    async def _converse[T](
        self, step: Callable[[], Awaitable[T | _Refusal]], seconds: float = EVENT_SECONDS
    ) -> T | SessionFailed:
        """대화 한 단계를 마감 안에서 돌린다. 실패하면 컨테이너를 지우고 사유를 돌려준다."""
        if self._closing is not None:
            raise SessionClosedError
        self._idle.clear()
        try:
            outcome = await self._guarded(step, seconds)
        except BaseException:
            # 취소 등. 대화가 어디서 멈췄는지 모르니 세션을 끝낸다.
            self._idle.set()
            await self.close()
            raise
        self._idle.set()
        if self._closing is not None:
            raise SessionClosedError
        if isinstance(outcome, SessionFailed):
            await self.close()
        return outcome

    async def _guarded[T](
        self, step: Callable[[], Awaitable[T | _Refusal]], seconds: float
    ) -> T | SessionFailed:
        box = self._box
        try:
            async with asyncio.timeout(seconds):
                outcome = await step()
        except TimeoutError:
            await _docker("kill", box.name)
            return _failed("timeout", f"{seconds:g}초 안에 답하지 않았습니다")
        except _OutputOverflowError as exc:
            return _failed("output", str(exc))
        except _HarnessEndedError:
            return await self._ended()
        # 표준 에러는 따로 읽으므로 넘친 것을 늦게 알 수 있다. 알게 된 첫 단계에서 끝낸다.
        if box.errors.flooded:
            return _failed("output", STDERR_FLOOD)
        if isinstance(outcome, _Refusal):
            return _failed(outcome.reason, outcome.detail)
        return outcome

    async def _ended(self) -> SessionFailed:
        """하네스가 답 없이 끝났다. 컨테이너가 끝난 뒤에야 상태(OOM·종료 코드)가 확정된다."""
        box = self._box
        box.talk.stdin.close()
        try:
            async with asyncio.timeout(DOCKER_CLI_SECONDS):
                await box.proc.wait()
        except TimeoutError:
            return _failed("timeout", "전략 컨테이너가 끝나지 않았습니다")
        if self._closing is not None:
            # 넣는 동안 다른 쪽이 닫았다(그래서 끝났다). 지운 컨테이너의 상태는 읽을 수 없다.
            raise SessionClosedError
        if box.errors.flooded:
            return _failed("output", STDERR_FLOOD)
        ended = await _exit_reason(box.name, box.talk.ready, box.proc.returncode, cpu_limit=False)
        return _failed(ended.reason, ended.detail)


async def remove_leftovers() -> int:
    """지난 실행기가 남긴 전략 컨테이너(백테스트·띄워 둔 것 모두)를 지운다. 지운 수를 돌려준다.

    실행기가 시작할 때 한 번 부른다. 실패해도 시작을 막지 않는다(로그만 남긴다). 실행기가 다시
    시작하면 세션은 하나도 살아남지 않는다. MCP 서버는 세션이 없다는 답(404)을 받고 그 전략을
    멈춘다.
    """
    listing = await _docker(
        "ps", "-aq", "--filter", f"name=^{NAME_PREFIX}", "--filter", f"name=^{RESIDENT_PREFIX}"
    )
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
