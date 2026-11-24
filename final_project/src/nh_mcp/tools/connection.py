"""연결 도구: get_connection_status, disconnect_account."""

from fastmcp import FastMCP

from nh_mcp import envelope
from nh_mcp.services.connection import ConnectionService
from nh_mcp.tools.context import ToolContext


def register_connection_tools(mcp: FastMCP, ctx: ToolContext, service: ConnectionService) -> None:
    @mcp.tool(annotations={"readOnlyHint": True})
    def get_connection_status() -> envelope.Response:
        """내 NH 모의 키가 시장(국내·해외)별로 등록돼 있는지와 등록 화면 주소를 알려 준다.

        쓸 때: 조회나 주문 전에 연결을 확인할 때, KEY_NOT_REGISTERED 오류를 받았을 때.
        쓰지 않을 때: 시세나 계좌를 볼 때는 해당 조회 도구를 바로 쓴다.
        키 값은 돌려주지 않는다. 키는 등록 화면에서만 받는다. 대화창에서 키를 받지 않는다.
        """
        return envelope.ok(service.status(ctx.user_id()), ctx.meta())

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def disconnect_account() -> envelope.Response:
        """내 NH 모의 키를 모두 지우고 연결을 끊는다. 확인 없이 바로 실행된다.

        쓸 때: 사용자가 연결을 끊거나 키를 지워 달라고 했을 때.
        쓰지 않을 때: 키를 바꾸려면 등록 화면에서 새 키로 교체한다.
        """
        await service.disconnect(ctx.user_id())
        return envelope.ok({"disconnected": True}, ctx.meta())
