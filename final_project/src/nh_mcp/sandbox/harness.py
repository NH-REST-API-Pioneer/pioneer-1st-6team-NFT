"""전략 컨테이너 안에서 AI가 짠 전략 코드를 돌리는 하네스.

이미지에 /harness/harness.py로 들어가 `python -s`로 돈다. 표준 라이브러리만 쓰고 nh_mcp는
import하지 않는다(이미지에 없다). 전략 코드를 exec하는 곳은 이 파일 하나뿐이다.

전략 코드는 on_event(event, ctx)(필수)와 init(ctx)(선택, 불러올 때 한 번)를 둔다.

프로토콜은 한 줄 JSON씩이다.
- 실행기 → {"code", "params", "streams", "model": 참거짓}
- model이 true면 실행기 → {"model_chunk": base64} 여러 줄과 {"model_end": true}. 하네스는 받는 대로
  /tmp/model에 쓴다(전략 코드를 불러오기 전이다). ctx.model_path가 그 경로가 된다.
- 하네스 → {"ready": true} 또는 {"error": 사유, "detail"}
- 실행기 → {"event"}. 하네스 → {"output": null 또는 {종목: 목표}} 또는 {"error": 사유, "detail"}
실행기는 출력을 받기 전에는 다음 이벤트를 보내지 않는다. 앞으로 올 이벤트는 이 프로세스 어디에도
없다. 출력은 JSON으로 쓸 수 있는지만 본다. 계약 검사는 실행기가 한다(이 프로세스는 전략 코드가
고칠 수 있다).
"""

import base64
import importlib
import importlib.abc
import importlib.machinery
import inspect
import json
import os
import random
import sys
from collections.abc import Callable, Sequence
from types import ModuleType
from typing import TextIO, cast

# 예외 메시지는 앞 200자만 돌려준다.
MESSAGE_HEAD = 200
# 실행기가 넘긴 모델 파일을 두는 곳. /tmp는 이 컨테이너만의 tmpfs다. 원본은 서버에 있다.
MODEL_PATH = "/tmp/model"

type Message = dict[str, object]
type OnEvent = Callable[..., object]


class InvalidCodeError(Exception):
    """코드는 돌았지만 on_event(event, ctx)가 없거나 init(ctx)의 모양이 틀렸다."""


def classify(exc: BaseException) -> str:
    """예외를 실패 사유로 바꾼다. 자원 한도(메모리·프로세스 수)에 닿은 것은 따로 가른다."""
    if isinstance(exc, MemoryError):
        return "memory"
    # 프로세스 수 한도에 닿으면 fork는 EAGAIN(BlockingIOError), 스레드는 RuntimeError가 난다.
    if isinstance(exc, BlockingIOError) or (
        isinstance(exc, RuntimeError) and "can't start new thread" in _text(exc)
    ):
        return "processes"
    return "exception"


def describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {_text(exc)[:MESSAGE_HEAD]}"


def _text(exc: BaseException) -> str:
    # 전략 코드가 만든 예외는 __str__에서 또 예외를 낼 수 있다.
    try:
        return str(exc)
    except Exception:
        return ""


def _takes(fn: Callable[..., object], count: int) -> bool:
    try:
        inspect.signature(fn).bind(*[None] * count)
    except (TypeError, ValueError):
        return False
    return True


class Context:
    """전략에 넘기는 ctx. 하네스가 채운다. 전략 코드가 고쳐도 서버에는 닿지 않는다.

    positions는 체결된 것만 센 종목별 포지션(부호 있는 정수), history는 그 종목·간격의 지난 봉
    (이번 이벤트 앞까지, 오래된 것부터)이다. 틱은 쌓지 않는다. 둘 다 부를 때마다 복사본을 준다.
    """

    def __init__(self, params: object, symbols: list[str]) -> None:
        self.params = params
        self.model_path: str | None = None
        self._positions = dict.fromkeys(symbols, 0)
        self._bars: dict[tuple[object, object], list[object]] = {}

    @property
    def positions(self) -> dict[str, int]:
        return dict(self._positions)

    def history(self, symbol: str, interval: str) -> list[object]:
        return list(self._bars.get((symbol, interval), ()))

    def filled(self, event: Message) -> None:
        # 실행기가 만든 체결이다(전략 코드가 쓴 것이 아니다).
        symbol, qty = str(event["symbol"]), cast(int, event["qty"])
        held = self._positions.get(symbol, 0)
        self._positions[symbol] = held + qty if event["side"] == "buy" else held - qty

    def shelf(self, bar: Message) -> list[object]:
        """이 봉이 쌓일 목록."""
        return self._bars.setdefault((bar["symbol"], bar["interval"]), [])


def load_strategy(code: str, ctx: Context) -> OnEvent:
    """코드를 새 이름공간에서 돌리고 on_event를 꺼낸다. init이 있으면 ctx로 한 번 부른다."""
    namespace: dict[str, object] = {"__name__": "strategy"}
    exec(compile(code, "<strategy>", "exec"), namespace)
    on_event = namespace.get("on_event")
    if not callable(on_event):
        raise InvalidCodeError("on_event 함수가 없습니다")
    if not _takes(on_event, 2):
        raise InvalidCodeError("on_event는 인자 둘(event, ctx)을 받아야 합니다")
    init = namespace.get("init")
    if init is not None:
        if not callable(init) or not _takes(init, 1):
            raise InvalidCodeError("init은 인자 하나(ctx)를 받는 함수여야 합니다")
        init(ctx)
    return on_event


def seed_numpy() -> None:
    # 개발 환경에는 numpy가 없어 정적 import를 쓰지 않는다. 이미지에는 있다.
    try:
        numpy = importlib.import_module("numpy")
    except ImportError:
        return
    numpy.random.seed(0)


class SeedTorchOnImport(importlib.abc.MetaPathFinder):
    """torch가 처음 불러와지는 순간 torch.manual_seed(0)을 건다. 하네스는 torch를 import하지 않는다.

    torch의 기본 시드는 프로세스마다 다르다. 코드를 불러온 뒤나 init 뒤에 시드를 걸면 맨 위 코드나
    init에서 만든 모델(nn.Linear 등)은 그 전에 초기화돼 실행마다 달라진다. 그래서 import가 끝나는
    순간에 건다. 맨 위, init, on_event 어디서 불러와도(다른 라이브러리가 불러와도) 같다.
    시드가 예외를 내면 전략 코드의 그 import 문이 예외를 낸다. 다른 import 실패와 똑같이, 불러올
    때면 invalid_code, on_event 안이면 exception이 된다.
    """

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None,
        target: ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        if fullname != "torch":
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is not None and isinstance(spec.loader, importlib.abc.Loader):
            spec.loader = _SeedAfterLoad(spec.loader)
        return spec


class _SeedAfterLoad(importlib.abc.Loader):
    """torch 패키지를 원래 로더로 불러온 뒤 시드를 건다. 나머지는 원래 로더에 맡긴다."""

    def __init__(self, inner: importlib.abc.Loader) -> None:
        self.inner = inner

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> ModuleType | None:
        return self.inner.create_module(spec)

    def exec_module(self, module: ModuleType) -> None:
        self.inner.exec_module(module)
        module.manual_seed(0)

    def __getattr__(self, name: str) -> object:
        # get_source, get_resource_reader 등(traceback, importlib.resources가 쓴다).
        return getattr(self.inner, name)


class Harness:
    def __init__(self, pid: int, inbox: TextIO, outbox: TextIO) -> None:
        self.pid = pid
        self.inbox = inbox
        self.outbox = outbox

    def leave_if_child(self) -> None:
        """전략 코드가 fork한 자식은 아무것도 쓰지 않고 끝난다. 프로토콜은 처음 프로세스만 쓴다."""
        if os.getpid() != self.pid:
            os._exit(0)

    def send(self, message: Message) -> None:
        self.write(json.dumps(message))

    def write(self, line: str) -> None:
        self.leave_if_child()
        self.outbox.write(line + "\n")
        self.outbox.flush()

    def receive(self) -> Message | None:
        line = self.inbox.readline()
        if not line:
            return None
        # 실행기가 보낸 줄이다(전략 코드가 쓴 것이 아니다).
        message: Message = json.loads(line)
        return message

    def receive_model(self, path: str) -> bool:
        """모델 조각을 받는 대로 path에 쓴다. 끝 표시까지 받으면 True, 그 전에 입력이 끝나면 False.

        한 조각씩 풀어 쓰므로 파일 전체를 메모리에 두지 않는다. 읽기 전용(0400)으로 만든다.
        """
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        with os.fdopen(fd, "wb") as out:
            while (message := self.receive()) is not None:
                if message.get("model_end") is True:
                    return True
                out.write(base64.b64decode(cast(str, message["model_chunk"])))
        return False

    def run(self) -> None:
        setup = self.receive()
        if setup is None:
            return
        # 실행기가 보낸 줄이라 streams는 종목 → 흐름 목록이다. 포지션은 종목마다 0에서 시작한다.
        ctx = Context(setup.get("params"), [*cast(dict[str, object], setup.get("streams"))])
        if setup.get("model") is True:
            if not self.receive_model(MODEL_PATH):
                return
            ctx.model_path = MODEL_PATH
        try:
            on_event = load_strategy(str(setup.get("code")), ctx)
        except InvalidCodeError as exc:
            self.send({"error": "invalid_code", "detail": str(exc)})
            return
        except BaseException as exc:
            # 문법 오류, 맨 위 코드·init의 예외는 코드가 잘못된 것이다. 자원 한도는 그 사유로.
            reason = classify(exc)
            self.send(
                {
                    "error": "invalid_code" if reason == "exception" else reason,
                    "detail": describe(exc),
                }
            )
            return
        self.send({"ready": True})
        while (message := self.receive()) is not None:
            event = cast(Message, message.get("event"))
            kind = event["type"]
            if kind == "fill":
                ctx.filled(event)
            # 봉이 쌓일 자리는 코드를 부르기 전에 정한다. 코드가 이벤트를 고쳐도 흔들리지 않는다.
            shelf = ctx.shelf(event) if kind == "bar" else None
            try:
                output = on_event(event, ctx)
            except BaseException as exc:
                self.send({"error": classify(exc), "detail": describe(exc)})
                return
            try:
                line = json.dumps({"output": output})
            except BaseException as exc:
                # JSON으로 못 쓰는 값(객체, 순환 참조)이다. 자원 한도는 그 사유로 알린다.
                reason = classify(exc)
                invalid = reason == "exception"
                self.send(
                    {
                        "error": "invalid_output" if invalid else reason,
                        "detail": type(output).__name__ if invalid else describe(exc),
                    }
                )
                return
            self.write(line)
            if shelf is not None:
                shelf.append(event)


def main() -> None:
    pid = os.getpid()
    # 프로토콜 출구는 따로 쥐고, 표준 출력은 표준 에러로 돌린다. print가 프로토콜을 깨지 않는다.
    outbox = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    # 같은 입력이면 같은 출력이 나오게 난수를 고정한다(PYTHONHASHSEED=0은 실행 인자가 건다).
    random.seed(0)
    seed_numpy()
    sys.meta_path.insert(0, SeedTorchOnImport())
    Harness(pid, sys.stdin, outbox).run()


if __name__ == "__main__":
    main()
