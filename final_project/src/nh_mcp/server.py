"""FastMCP 서버: 도구를 등록하고 HTTP로 띄운다.

로그인 없이 서버를 만드는 길은 없다. build_server는 인증을 반드시 받고,
운영 서버를 만드는 create_server는 Google 로그인만 쓴다.
"""

import os
import sqlite3
from datetime import UTC, datetime

from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider
from fastmcp.server.middleware import AuthMiddleware
from fastmcp.utilities.authorization import AuthCheck

from nh_mcp import envelope
from nh_mcp.auth.access import AccessPolicy, make_gate
from nh_mcp.auth.google import google_provider
from nh_mcp.config import ENV, Settings, load_settings
from nh_mcp.domain import Clock
from nh_mcp.store import db

HOST = "127.0.0.1"
PORT = 8000


def utc_now() -> datetime:
    return datetime.now(UTC)


def build_server(auth: AuthProvider, gate: AuthCheck, clock: Clock = utc_now) -> FastMCP:
    mcp = FastMCP("nh-futures-mcp", auth=auth, middleware=[AuthMiddleware(auth=gate)])

    @mcp.tool(annotations={"readOnlyHint": True})
    def ping() -> envelope.Success:
        """서버가 살아 있는지 확인한다. NH API는 부르지 않는다.

        쓸 때: 서버 연결을 처음 확인할 때.
        쓰지 않을 때: 시세나 계좌를 볼 때는 해당 조회 도구를 쓴다.
        """
        return envelope.ok({"pong": True}, envelope.meta(ENV, clock()))

    return mcp


def create_server(settings: Settings, conn: sqlite3.Connection) -> FastMCP:
    policy = AccessPolicy(settings.open_signup, settings.admin_emails)
    return build_server(google_provider(settings), make_gate(policy, conn, utc_now))


def main() -> None:
    settings = load_settings(os.environ)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    create_server(settings, db.connect(settings.db_path)).run(
        transport="http", host=HOST, port=PORT
    )


if __name__ == "__main__":
    main()
