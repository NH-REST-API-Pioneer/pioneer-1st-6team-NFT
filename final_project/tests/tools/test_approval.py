"""승인 창. 실제 FastMCP 서버 + 클라이언트로 2단계 경로를, 가짜 ctx로 이전 규격 경로를 본다."""

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Literal, cast

import pytest
from conftest import Serve
from fastmcp import Client, Context, FastMCP
from fastmcp.client.elicitation import ElicitResult
from mcp_types import InputRequiredResult

from nh_mcp.services.approval import Approval
from nh_mcp.tools.approval import ask

type Content = dict[str, str | int | float | bool | list[str] | None]

MESSAGE = "A016C000 매수 1계약을 승인할까요?"
YES: Content = {"value": True}
# 진짜 True가 아닌 값. 느슨한 검사는 이것들을 True로 바꿔 버린다.
NOT_TRUE: list[Content | None] = [
    None,
    {},
    {"value": False},
    {"value": "true"},
    {"value": "yes"},
    {"value": "on"},
    {"value": 1},
    {"x": True},
]


def gate_server() -> FastMCP:
    """승인만 묻는 시험 서버. 두 번째 라운드에 다른 state나 key를 쓰게 할 수 있다."""
    mcp = FastMCP("approval-test")

    @mcp.tool
    async def gate(
        ctx: Context, change_state: bool = False, change_key: bool = False
    ) -> str | InputRequiredResult:
        second_round = ctx.input_responses is not None
        state = "changed" if change_state and second_round else "first"
        key = "other" if change_key and second_round else "approve"
        return await ask(ctx, key, MESSAGE, state)

    return mcp


@dataclass
class Answer:
    """창에서 사용자가 한 일을 흉내 내고, 창이 어떤 문구로 떴는지 기록한다."""

    action: Literal["accept", "decline", "cancel"]
    content: Content | None = None
    messages: list[str] | None = None

    async def __call__(self, message: str, *_: object) -> ElicitResult:
        if self.messages is not None:
            self.messages.append(message)
        return ElicitResult(action=self.action, content=self.content)


async def run_gate(serve: Serve, answer: Answer | None, **args: bool) -> str:
    async with (
        serve(gate_server()) as url,
        Client(url, elicitation_handler=answer) as client,
    ):
        result = await client.call_tool("gate", args)
    assert len(result.content) == 1
    block = result.content[0]
    assert block.type == "text"
    return block.text


def test_accept_true_is_approved_and_window_shows_the_message(serve: Serve) -> None:
    messages: list[str] = []

    assert asyncio.run(run_gate(serve, Answer("accept", YES, messages))) == "approved"
    assert messages == [MESSAGE]


def test_decline_is_declined(serve: Serve) -> None:
    assert asyncio.run(run_gate(serve, Answer("decline"))) == "declined"


def test_cancel_is_declined(serve: Serve) -> None:
    assert asyncio.run(run_gate(serve, Answer("cancel"))) == "declined"


# 실제 클라이언트는 스키마(value: boolean, 필수)에 안 맞는 답을 서버에 보내기 전에 막는다.
# 그래서 서버까지 닿는 거절은 이 둘이다. 나머지는 아래 가짜 ctx 시험이 본다.
@pytest.mark.parametrize("content", [None, {"value": False}])
def test_accept_without_true_is_declined(serve: Serve, content: Content | None) -> None:
    assert asyncio.run(run_gate(serve, Answer("accept", content))) == "declined"


def test_client_without_elicitation_is_unsupported(serve: Serve) -> None:
    assert asyncio.run(run_gate(serve, None)) == "unsupported"


def test_changed_state_between_rounds_is_declined_even_if_the_user_said_yes(
    serve: Serve,
) -> None:
    assert asyncio.run(run_gate(serve, Answer("accept", YES), change_state=True)) == "declined"


def test_missing_key_in_the_second_round_is_declined(serve: Serve) -> None:
    assert asyncio.run(run_gate(serve, Answer("accept", YES), change_key=True)) == "declined"


def test_same_state_and_key_between_rounds_is_approved(serve: Serve) -> None:
    assert asyncio.run(run_gate(serve, Answer("accept", YES))) == "approved"


# --- 2026-07-28 규격, 가짜 ctx: 클라이언트가 막아 주지 않는 답을 서버가 스스로 거르는지 ---


class ModernContext:
    def __init__(self, responses: dict[str, ElicitResult] | None, state: str | None) -> None:
        self.session = SimpleNamespace(
            protocol_version="2026-07-28",
            client_capabilities=SimpleNamespace(elicitation=SimpleNamespace()),
        )
        self.input_responses = responses
        self.request_state = state


def modern_ask(ctx: ModernContext) -> Approval | InputRequiredResult:
    return asyncio.run(ask(cast(Context, ctx), "approve", MESSAGE, "s"))


def test_modern_first_round_asks_with_the_state() -> None:
    result = modern_ask(ModernContext(None, None))

    assert isinstance(result, InputRequiredResult)
    assert result.request_state == "s"
    assert list(result.input_requests or {}) == ["approve"]


def test_modern_real_true_with_matching_state_is_approved() -> None:
    answer = ElicitResult(action="accept", content=YES)

    assert modern_ask(ModernContext({"approve": answer}, "s")) == "approved"


@pytest.mark.parametrize("content", NOT_TRUE)
def test_modern_accept_without_a_real_true_is_declined(content: Content | None) -> None:
    answer = ElicitResult(action="accept", content=content)

    assert modern_ask(ModernContext({"approve": answer}, "s")) == "declined"


def test_modern_matching_state_but_missing_key_is_declined() -> None:
    answer = ElicitResult(action="accept", content=YES)

    assert modern_ask(ModernContext({"other": answer}, "s")) == "declined"


# --- 이전 규격(핸드셰이크 시대): 세션으로 직접 묻고 같은 엄격한 판정을 쓴다 -----------


class FakeSession:
    def __init__(self, answer: object, declares_elicitation: bool) -> None:
        self.protocol_version = "2025-06-18"
        self.client_capabilities = SimpleNamespace(
            elicitation=SimpleNamespace() if declares_elicitation else None
        )
        self._answer = answer
        self.asked: list[tuple[str, dict[str, object]]] = []

    async def elicit(
        self, message: str, requested_schema: dict[str, object], related_request_id: str
    ) -> object:
        assert related_request_id == "req-1"
        self.asked.append((message, requested_schema))
        if isinstance(self._answer, Exception):
            raise self._answer
        return self._answer


class FakeContext:
    """`ask`가 읽는 것만 가진 ctx. 이전 규격 연결이라고 친다."""

    def __init__(self, answer: object, declares_elicitation: bool = True) -> None:
        self.session = FakeSession(answer, declares_elicitation)
        self.request_id = "req-1"
        self.input_responses = None
        self.request_state = None


def legacy_ask(ctx: FakeContext) -> Approval | InputRequiredResult:
    return asyncio.run(ask(cast(Context, ctx), "approve", MESSAGE, "s"))


def test_legacy_true_is_approved_and_asks_the_form_with_the_message() -> None:
    ctx = FakeContext(ElicitResult(action="accept", content=YES))

    assert legacy_ask(ctx) == "approved"
    assert len(ctx.session.asked) == 1
    message, schema = ctx.session.asked[0]
    assert message == MESSAGE
    assert schema["required"] == ["value"]


@pytest.mark.parametrize("content", NOT_TRUE)
def test_legacy_accept_without_a_real_true_is_declined(content: Content | None) -> None:
    assert legacy_ask(FakeContext(ElicitResult(action="accept", content=content))) == "declined"


@pytest.mark.parametrize("action", ["decline", "cancel"])
def test_legacy_decline_and_cancel_are_declined(action: Literal["decline", "cancel"]) -> None:
    assert legacy_ask(FakeContext(ElicitResult(action=action))) == "declined"


def test_legacy_exception_is_declined() -> None:
    assert legacy_ask(FakeContext(RuntimeError("연결 끊김"))) == "declined"


def test_legacy_without_elicitation_is_unsupported_and_asks_nothing() -> None:
    ctx = FakeContext(ElicitResult(action="accept", content=YES), declares_elicitation=False)

    assert legacy_ask(ctx) == "unsupported"
    assert ctx.session.asked == []
