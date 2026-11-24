"""MCP elicitation으로 사람에게 승인을 묻는다. 규격에 따라 두 가지 길이 있다.

판별은 협상한 규격(`ctx.session.protocol_version`)과 클라이언트가 선언한
`elicitation` 기능(`ctx.session.client_capabilities`)으로 한다.

- 2026-07-28 규격(`MODERN_PROTOCOL_VERSIONS`): 서버가 먼저 보내는 요청이 없다. 도구가
  `InputRequiredResult`를 돌려주면 클라이언트가 사용자 답을 담아 같은 도구를 다시 부르고,
  그때 `ctx.input_responses`와 `ctx.request_state`로 읽는다.
- 이전 규격: `ctx.elicit()`으로 바로 묻는다.
"""

import mcp_types
from fastmcp import Context
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

from nh_mcp.services.approval import Approval

_SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "boolean", "title": "승인"}},
    "required": ["value"],
}


async def ask(
    ctx: Context, key: str, message: str, state: str
) -> Approval | mcp_types.InputRequiredResult:
    """사람에게 승인을 묻는다.

    `state`는 부르는 쪽이 정한다. 사용자가 답한 뒤 다시 불렸을 때 `state`가 처음과 다르면
    (그 사이 내용이 바뀐 것) 승인으로 치지 않고 `declined`를 돌려준다.
    """
    capabilities = ctx.session.client_capabilities
    if capabilities is None or capabilities.elicitation is None:
        return "unsupported"
    if ctx.session.protocol_version in MODERN_PROTOCOL_VERSIONS:
        return _ask_in_two_rounds(ctx, key, message, state)
    return await _ask_now(ctx, message)


def _ask_in_two_rounds(
    ctx: Context, key: str, message: str, state: str
) -> Approval | mcp_types.InputRequiredResult:
    responses = ctx.input_responses
    if responses is None:
        request = mcp_types.ElicitRequest(
            params=mcp_types.ElicitRequestFormParams(message=message, requested_schema=_SCHEMA)
        )
        return mcp_types.InputRequiredResult(input_requests={key: request}, request_state=state)
    if ctx.request_state != state:
        return "declined"
    answer = responses.get(key)
    if not isinstance(answer, mcp_types.ElicitResult):
        return "declined"
    return _verdict(answer)


async def _ask_now(ctx: Context, message: str) -> Approval:
    # ctx.elicit(message, bool)은 pydantic 느슨 검사라 "true", "yes", 1도 True로 바꾼다.
    # 승인은 원본 응답에서 엄격하게 읽어야 해서 세션으로 직접 묻는다.
    try:
        answer = await ctx.session.elicit(
            message=message, requested_schema=_SCHEMA, related_request_id=ctx.request_id
        )
    except Exception:  # 창을 띄우지 못했다 = 승인을 받지 못했다
        return "declined"
    return _verdict(answer)


def _verdict(answer: mcp_types.ElicitResult) -> Approval:
    """두 규격이 함께 쓰는 판정. accept이고 value가 진짜 True일 때만 승인이다."""
    if answer.action != "accept" or answer.content is None:
        return "declined"
    return "approved" if answer.content.get("value") is True else "declined"
