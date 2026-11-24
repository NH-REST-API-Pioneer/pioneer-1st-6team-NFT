"""전략 코드를 격리 컨테이너에서 돌리고 봉마다 신호를 받아 온다(안전 불변식 12).

코드는 문자열 그대로 하네스에 넘기기만 한다. 이 프로세스에서는 실행하지도 파싱하지도 않는다.
docker run 인자는 상수다. 바뀌는 값은 이미지 이름과 무작위 컨테이너 이름뿐이고, 셸을 거치지 않는다.
"""

import asyncio
import contextlib
import json
import logging
import secrets
from typing import cast

from nh_mcp.domain import JsonValue, SandboxFailure, SignalName
from nh_mcp.sandbox.wire import MAX_DETAIL, Reply, WireBar

NAME_PREFIX = "nhmcp-sbx-"
# 실행기 쪽 벽시계 마감. 컨테이너 안 CPU 한도(60초)보다 길다.
WALL_SECONDS = 70.0
MAX_STDOUT = 1_048_576
# 표준 에러(전략 코드의 print도 이리로 온다)도 이만큼만 받는다. 넘으면 출력 폭주로 끊는다.
MAX_STDERR = 1_048_576
# docker kill · rm · inspect 한 번을 기다리는 시간.
DOCKER_CLI_SECONDS = 30.0
# CPU 한도(--ulimit cpu=60:60)는 soft와 hard가 같아 커널이 SIGKILL(128+9)을 먼저 보낸다.
# SIGXCPU(128+24)는 soft < hard일 때만 오지만 같이 받아 둔다. OOM도 137이라 OOM 표시를 먼저 본다.
CPU_EXIT_CODES = frozenset({"137", "152"})
# docker run이 컨테이너를 띄우지 못했을 때 내는 종료 코드(125 docker 오류, 126·127 실행 파일 문제).
DOCKER_START_CODES = frozenset({125, 126, 127})

_LOG = logging.getLogger(__name__)

# 고정 인자. 요청 값은 어느 것도 여기에 들어가지 않는다.
FIXED_ARGS = (
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


def failure(status: SandboxFailure, bar_index: int | None, detail: str) -> Reply:
    return Reply(status, [], bar_index, detail[:MAX_DETAIL])


class ContainerStartError(Exception):
    """컨테이너가 하네스를 돌리지 못했다(이미지 없음, 데몬 오류 등). 전략 코드의 실패가 아니다.

    실행기는 이 예외를 HTTP 500으로 답하고, MCP 서버는 실행 기록을 남기지 않는다.
    """


class _OutputOverflowError(Exception):
    """표준 출력이 한도를 넘었다."""


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


def _signal(message: dict[str, object] | None) -> SignalName | None:
    value = None if message is None else message.get("signal")
    if not isinstance(value, str):
        return None
    match value:
        case "buy" | "sell" | "exit" | "none" as signal:
            return signal
        case _:
            return None


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
        case "invalid_signal" | "exception" as reason if not loading:
            return reason
        case _:
            return None


class _Conversation:
    """하네스와 한 줄씩 주고받는다. 읽은 양을 세고, 지금 어느 봉을 처리 중인지 적어 둔다."""

    def __init__(self, stdin: asyncio.StreamWriter, stdout: asyncio.StreamReader) -> None:
        self.stdin = stdin
        self.stdout = stdout
        self.bar_index: int | None = None
        self.read_total = 0
        # 하네스가 ready를 보냈는지. 그 전에 끝났다면 컨테이너가 아예 못 떴을 수도 있다.
        self.ready = False

    async def send(self, message: dict[str, object]) -> None:
        self.stdin.write(json.dumps(message).encode() + b"\n")
        try:
            await self.stdin.drain()
        except ConnectionError as exc:
            raise _HarnessEndedError from exc

    async def receive(self) -> dict[str, object] | None:
        try:
            line = await self.stdout.readuntil(b"\n")
        except asyncio.LimitOverrunError as exc:
            raise _OutputOverflowError from exc
        except asyncio.IncompleteReadError as exc:
            self.read_total += len(exc.partial)
            if self.read_total > MAX_STDOUT:
                raise _OutputOverflowError from exc
            raise _HarnessEndedError from exc
        self.read_total += len(line)
        if self.read_total > MAX_STDOUT:
            raise _OutputOverflowError
        return _decode(line)

    def refusal(self, message: dict[str, object] | None, loading: bool) -> Reply:
        """신호(또는 ready) 대신 온 줄을 실패 사유로 바꾼다.

        detail에는 전략 코드가 낸 것(받은 값, 하네스 줄의 detail)만 담는다. 프로토콜 출구는 전략
        코드도 쓸 수 있어서다. 설명 문장은 MCP 서버가 붙이고, 전략 코드가 낸 부분을 표시한다.
        """
        if message is not None:
            reason = _harness_reason(message.get("error"), loading)
            if reason is not None:
                return failure(reason, self.bar_index, str(message.get("detail", "")))
            if "signal" in message:
                return failure("invalid_signal", self.bar_index, repr(str(message["signal"])[:100]))
        # 읽지 못한 줄은 보여 주지 않는다.
        return failure("invalid_signal", self.bar_index, "")

    async def run(self, code: str, params: dict[str, JsonValue], bars: list[WireBar]) -> Reply:
        await self.send({"code": code, "params": params})
        ready = await self.receive()
        if ready is None or ready.get("ready") is not True:
            return self.refusal(ready, loading=True)
        self.ready = True
        signals: list[SignalName] = []
        for index, bar in enumerate(bars):
            # 신호를 받기 전에는 다음 봉을 보내지 않는다. 앞으로 올 봉은 컨테이너에 없다.
            self.bar_index = index
            await self.send({"bar": bar})
            answer = await self.receive()
            signal = _signal(answer)
            if signal is None:
                return self.refusal(answer, loading=False)
            signals.append(signal)
        self.stdin.close()
        return Reply("ok", signals, None, "")


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


async def _exit_reason(name: str, talk: _Conversation, cli_code: int | None) -> Reply:
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
    bar_index = talk.bar_index
    if oom == "true":
        return failure("memory", bar_index, "메모리 한도(512MB)를 넘었습니다")
    if code in CPU_EXIT_CODES:
        return failure("cpu", bar_index, "CPU 시간 한도(60초)를 넘었습니다")
    detail = (
        f"전략 프로세스가 종료 코드 {code}로 끝났습니다" if code else "종료 코드를 알 수 없습니다"
    )
    return failure("exception", bar_index, detail)


async def run_in_container(
    code: str,
    params: dict[str, JsonValue],
    bars: list[WireBar],
    image: str,
    wall_seconds: float = WALL_SECONDS,
) -> Reply:
    """전략 코드를 새 컨테이너 하나에서 돌린다. 끝나면 어떤 경우에도 컨테이너를 지운다."""
    name = NAME_PREFIX + secrets.token_hex(8)
    proc = await asyncio.create_subprocess_exec(
        *docker_argv(image, name),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=MAX_STDOUT,
    )
    stdin, stdout, stderr = proc.stdin, proc.stdout, proc.stderr
    if stdin is None or stdout is None or stderr is None:
        await _docker("rm", "-f", name)
        raise RuntimeError("docker run 파이프를 열지 못했습니다")
    errors = _ErrorStream(stderr, proc)
    stderr_drain = asyncio.create_task(errors.drain())
    talk = _Conversation(stdin, stdout)
    reply: Reply | None = None
    try:
        try:
            # 마감은 하나다. 모든 쓰기·읽기가 남은 시간 안에서 돈다.
            async with asyncio.timeout(wall_seconds):
                try:
                    reply = await talk.run(code, params, bars)
                except _HarnessEndedError:
                    # 컨테이너가 끝난 뒤에야 상태(OOM·종료 코드)가 확정된다.
                    stdin.close()
                    await proc.wait()
        except TimeoutError:
            await _docker("kill", name)
            reply = failure("timeout", talk.bar_index, f"{wall_seconds:g}초 안에 끝나지 않았습니다")
        except _OutputOverflowError:
            reply = failure("output", talk.bar_index, "출력이 1MB를 넘었습니다")
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
        return failure("output", talk.bar_index, "출력이 1MB를 넘었습니다")
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
