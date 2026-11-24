"""전략 컨테이너 안에서 AI가 짠 전략 코드를 돌리는 하네스.

이미지에 /harness/harness.py로 들어가 `python -s`로 돈다. 표준 라이브러리만 쓰고 nh_mcp는
import하지 않는다(이미지에 없다). 전략 코드를 exec하는 곳은 이 파일 하나뿐이다.

프로토콜은 한 줄 JSON씩이다.
- 실행기 → {"code", "params"}. 하네스 → {"ready": true} 또는 {"error": "invalid_code", "detail"}
- 실행기 → {"bar"}. 하네스 → {"signal"} 또는 {"error": 사유, "detail"}
실행기는 신호를 받기 전에는 다음 봉을 보내지 않는다. 앞으로 올 봉은 이 프로세스 어디에도 없다.
"""

import importlib
import inspect
import json
import os
import random
import sys
from collections.abc import Callable
from typing import TextIO

# 예외 메시지는 앞 200자만 돌려준다.
MESSAGE_HEAD = 200

type Message = dict[str, object]
type OnBar = Callable[..., object]


class InvalidCodeError(Exception):
    """코드는 돌았지만 on_bar(bar, history, params)가 없다."""


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


def load_strategy(code: str) -> OnBar:
    """코드를 새 이름공간에서 돌리고 on_bar를 꺼낸다. 위치 인자 셋을 받아야 한다."""
    namespace: dict[str, object] = {"__name__": "strategy"}
    exec(compile(code, "<strategy>", "exec"), namespace)
    on_bar = namespace.get("on_bar")
    if not callable(on_bar):
        raise InvalidCodeError("on_bar 함수가 없습니다")
    try:
        inspect.signature(on_bar).bind(None, None, None)
    except (TypeError, ValueError):
        raise InvalidCodeError("on_bar는 인자 셋(bar, history, params)을 받아야 합니다") from None
    return on_bar


def seed_numpy() -> None:
    # 개발 환경에는 numpy가 없어 정적 import를 쓰지 않는다. 이미지에는 있다.
    try:
        numpy = importlib.import_module("numpy")
    except ImportError:
        return
    numpy.random.seed(0)


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
        self.leave_if_child()
        self.outbox.write(json.dumps(message) + "\n")
        self.outbox.flush()

    def receive(self) -> Message | None:
        line = self.inbox.readline()
        if not line:
            return None
        # 실행기가 보낸 줄이다(전략 코드가 쓴 것이 아니다).
        message: Message = json.loads(line)
        return message

    def run(self) -> None:
        setup = self.receive()
        if setup is None:
            return
        params = setup.get("params")
        try:
            on_bar = load_strategy(str(setup.get("code")))
        except InvalidCodeError as exc:
            self.send({"error": "invalid_code", "detail": str(exc)})
            return
        except BaseException as exc:
            # 문법 오류, 맨 위 코드의 예외는 코드가 잘못된 것이다. 자원 한도는 그 사유로 알린다.
            reason = classify(exc)
            self.send(
                {
                    "error": "invalid_code" if reason == "exception" else reason,
                    "detail": describe(exc),
                }
            )
            return
        self.send({"ready": True})
        history: list[object] = []
        while (message := self.receive()) is not None:
            bar = message.get("bar")
            try:
                # history는 복사본을 준다. 전략 코드가 고쳐도 다음 봉의 history는 그대로다.
                signal = on_bar(bar, list(history), params)
            except BaseException as exc:
                self.send({"error": classify(exc), "detail": describe(exc)})
                return
            if not isinstance(signal, str):
                self.send({"error": "invalid_signal", "detail": type(signal).__name__})
                return
            # 네 값 중 하나인지는 실행기가 본다(이 프로세스는 전략 코드가 고칠 수 있다).
            self.send({"signal": signal})
            history.append(bar)


def main() -> None:
    pid = os.getpid()
    # 프로토콜 출구는 따로 쥐고, 표준 출력은 표준 에러로 돌린다. print가 프로토콜을 깨지 않는다.
    outbox = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    # 같은 입력이면 같은 신호가 나오게 난수를 고정한다(PYTHONHASHSEED=0은 실행 인자가 건다).
    random.seed(0)
    seed_numpy()
    Harness(pid, sys.stdin, outbox).run()


if __name__ == "__main__":
    main()
