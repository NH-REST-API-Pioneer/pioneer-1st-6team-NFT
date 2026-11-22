"""FastMCP 서버: 도구를 등록하고 HTTP로 띄운다."""

import os
from collections.abc import Callable
from datetime import UTC, datetime

from fastmcp import FastMCP

from nh_mcp import envelope
from nh_mcp.config import Settings, load_settings

type Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def build_server(settings: Settings, clock: Clock = _utc_now) -> FastMCP:
    mcp = FastMCP("nh-futures-mcp")

    @mcp.tool(annotations={"readOnlyHint": True})
    def ping() -> envelope.Success:
        """서버가 살아 있는지 확인한다. NH API는 부르지 않는다.

        쓸 때: 서버 연결을 처음 확인할 때.
        쓰지 않을 때: 시세나 계좌를 볼 때는 해당 조회 도구를 쓴다.
        """
        return envelope.ok({"pong": True}, envelope.meta(settings.env, clock()))

    return mcp


def main() -> None:
    build_server(load_settings(os.environ)).run(transport="http")


if __name__ == "__main__":
    main()
