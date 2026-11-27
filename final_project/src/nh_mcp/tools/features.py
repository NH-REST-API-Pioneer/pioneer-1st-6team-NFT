"""기능 묶음 숨기기(`docs/tools.md` "기능 묶음"): 켜지 않은 묶음의 도구를 도구 목록에서 뺀다.

숨기기만으로 끝내지 않는다. 꺼진 묶음의 일은 서비스가 다시 보고 거절한다(`auto_run_off`,
`options_off`). 묶음을 켜고 끄는 것은 등록 화면(`keys/page.py`)뿐이다.
"""

from collections.abc import Callable, Sequence

import mcp_types
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import Tool

from nh_mcp.auth.access import NotLoggedIn
from nh_mcp.domain import UserId
from nh_mcp.store.features import Feature
from nh_mcp.tools.context import ToolContext
from nh_mcp.tools.options import OPTIONS_TAG
from nh_mcp.tools.strategy import AUTO_RUN_TAG

# 도구 태그 → 그 태그의 도구를 보이게 하는 기능. 이 표에 없는 태그만 단 도구는 늘 보인다.
# "옵션 매도"는 도구가 없어 표에 없다.
FEATURE_TAGS: dict[str, Feature] = {AUTO_RUN_TAG: "auto_run", OPTIONS_TAG: "options"}

# 사용자가 그 기능을 켰는가(store.features.enabled). 서버 조립이 넣어 준다.
type Enabled = Callable[[UserId, Feature], bool]


class FeatureGate(Middleware):
    """켜지 않은 묶음의 도구를 그 사용자의 도구 목록에서 뺀다.

    설정은 요청마다 DB에서 읽는다(등록 화면에서 바꾸면 다음 목록부터 바뀐다). 로그인한 사용자를
    모르면 묶음의 도구를 모두 뺀다.
    """

    def __init__(self, ctx: ToolContext, enabled: Enabled) -> None:
        self._ctx = ctx
        self._enabled = enabled

    async def on_list_tools(
        self,
        context: MiddlewareContext[mcp_types.ListToolsRequest],
        call_next: CallNext[mcp_types.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        tools = await call_next(context)
        hidden = FEATURE_TAGS.keys() - self._shown()
        return [tool for tool in tools if not tool.tags & hidden]

    def _shown(self) -> set[str]:
        """켜진 묶음의 태그."""
        try:
            user_id = self._ctx.user_id()
        except NotLoggedIn:
            return set()
        return {tag for tag, feature in FEATURE_TAGS.items() if self._enabled(user_id, feature)}
