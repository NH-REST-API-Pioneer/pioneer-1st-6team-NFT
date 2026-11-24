"""FastMCP 서버: 도구를 등록하고 HTTP로 띄운다.

로그인 없이 서버를 만드는 길은 없다. build_server는 인증을 반드시 받고,
운영 서버를 만드는 create_server는 Google 로그인만 쓴다.
"""

import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TypedDict
from urllib.parse import urlsplit

import httpx2
from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider
from fastmcp.server.middleware import AuthMiddleware
from fastmcp.utilities.authorization import AuthCheck

from nh_mcp import envelope
from nh_mcp.auth.access import AccessPolicy, make_gate
from nh_mcp.auth.google import google_provider
from nh_mcp.auth.web import KEYS_PATH, GoogleWebLogin, add_web_login_routes
from nh_mcp.config import ENV, Settings, load_settings
from nh_mcp.domain import Clock
from nh_mcp.keys.page import KeysPage, add_keys_routes
from nh_mcp.keys.service import KeyStore, Verify, demo_server_verifier
from nh_mcp.keys.vault import Vault
from nh_mcp.markets.domestic import DomesticMarket
from nh_mcp.markets.domestic_master import DomesticMaster
from nh_mcp.markets.domestic_products import DomesticProducts
from nh_mcp.nh.clients import ClientPool
from nh_mcp.ops.rate_limit import LIMITS, RateLimiter
from nh_mcp.services.connection import ConnectionService
from nh_mcp.services.limits import APPROVE_PATH, LimitService
from nh_mcp.services.orders import OrderService
from nh_mcp.services.queries import QueryService
from nh_mcp.services.symbols import SymbolService
from nh_mcp.store import db
from nh_mcp.tools.account import register_account_tools
from nh_mcp.tools.connection import register_connection_tools
from nh_mcp.tools.context import ToolContext
from nh_mcp.tools.limits import register_limit_tools
from nh_mcp.tools.limits_page import LimitsPage, add_limits_routes
from nh_mcp.tools.market import register_market_tools
from nh_mcp.tools.order import register_order_tools

PORT = 8000


def utc_now() -> datetime:
    return datetime.now(UTC)


def build_server(auth: AuthProvider, gate: AuthCheck, clock: Clock = utc_now) -> FastMCP:
    mcp = FastMCP("nh-futures-mcp", auth=auth, middleware=[AuthMiddleware(auth=gate)])

    @mcp.tool(annotations={"readOnlyHint": True})
    def ping() -> envelope.Response:
        """서버가 살아 있는지 확인한다. NH API는 부르지 않는다.

        쓸 때: 서버 연결을 처음 확인할 때.
        쓰지 않을 때: 시세나 계좌를 볼 때는 해당 조회 도구를 쓴다.
        """
        return envelope.ok({"pong": True}, envelope.meta(ENV, clock()))

    return mcp


@dataclass(frozen=True)
class Wiring:
    """서버에 붙는 기능을 조립하는 재료. 테스트는 Google·NH 자리에 가짜 전송을 끼운다."""

    settings: Settings
    conn: sqlite3.Connection
    clock: Clock
    verify: Verify
    google: httpx2.AsyncBaseTransport | None = None
    nh: httpx2.AsyncBaseTransport | None = None
    master: httpx2.AsyncBaseTransport | None = None


@dataclass(frozen=True)
class Parts:
    login: GoogleWebLogin
    store: KeyStore
    pool: ClientPool


def attach(mcp: FastMCP, wiring: Wiring) -> Parts:
    """웹 로그인, 키 화면, 조회 도구, 한도 도구, (스위치가 켜졌으면) 주문 도구를 붙인다."""
    vault = Vault(wiring.settings.key_encryption_key)
    login = GoogleWebLogin(wiring.settings, wiring.conn, wiring.clock, transport=wiring.google)
    store = KeyStore(wiring.conn, vault, wiring.verify, wiring.clock)
    pool = ClientPool(wiring.conn, vault, wiring.clock, transport=wiring.nh)
    add_web_login_routes(mcp, login)
    add_keys_routes(mcp, KeysPage(login, store))
    keys_url = wiring.settings.public_base_url + KEYS_PATH
    ctx = ToolContext(wiring.conn, wiring.clock, keys_url)
    master = DomesticMaster(wiring.clock, transport=wiring.master)
    domestic = DomesticMarket(pool, master, wiring.clock)
    queries = QueryService(domestic)
    # 속도 제한은 프로세스 하나에 하나. 버킷은 사용자별로 나뉜다.
    limiter = RateLimiter(wiring.clock, LIMITS)
    register_connection_tools(mcp, ctx, ConnectionService(store, pool, keys_url))
    register_market_tools(mcp, ctx, SymbolService(master), queries)
    limit_service = LimitService(
        wiring.conn, wiring.clock, wiring.settings.public_base_url + APPROVE_PATH
    )
    register_limit_tools(mcp, ctx, limit_service, limiter)
    add_limits_routes(mcp, LimitsPage(login, limit_service))
    if wiring.settings.orders_enabled:
        products = DomesticProducts(pool, wiring.clock)
        orders = OrderService(wiring.conn, wiring.clock, domestic, products, orders_enabled=True)
        register_order_tools(mcp, ctx, orders, limiter)
        # get_orders가 응답을 못 받은 주문을 대조한다(UNKNOWN_STATE 안내가 get_orders를 가리킨다).
        register_account_tools(mcp, ctx, queries, orders.reconcile)
    else:
        register_account_tools(mcp, ctx, queries)
    return Parts(login, store, pool)


def create_server(settings: Settings, conn: sqlite3.Connection) -> FastMCP:
    policy = AccessPolicy(settings.open_signup, settings.admin_emails)
    mcp = build_server(google_provider(settings), make_gate(policy, conn, utc_now))
    attach(mcp, Wiring(settings, conn, utc_now, demo_server_verifier(utc_now)))
    return mcp


class UvicornOptions(TypedDict):
    access_log: bool


class RunOptions(TypedDict):
    host: str
    port: int
    host_origin_protection: bool
    allowed_hosts: list[str]
    allowed_origins: list[str]
    uvicorn_config: UvicornOptions


def run_options(settings: Settings) -> RunOptions:
    """HTTP로 띄울 때의 옵션. Host 머리말은 공개 주소만 받는다(DNS 리바인딩 막기).

    Tunnel이 HTTPS를 풀고 http로 넘기므로 서버는 자기 주소를 http로 안다. 그래서 브라우저가
    보내는 https Origin이 같은 사이트로 보이지 않는다. 공개 주소의 Origin을 따로 허용한다.

    uvicorn 접근 로그는 끈다. 요청 주소를 그대로 적어 한도 승인 링크(`/limits/approve/<토큰>`)가
    로그에 남기 때문이다(안전 불변식 7).
    """
    public = urlsplit(settings.public_base_url)
    return {
        "host": settings.bind_host,
        "port": PORT,
        "host_origin_protection": True,
        "allowed_hosts": [public.netloc],
        "allowed_origins": [f"{public.scheme}://{public.netloc}"],
        "uvicorn_config": {"access_log": False},
    }


def main() -> None:
    settings = load_settings(os.environ)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    server = create_server(settings, db.connect(settings.db_path))
    server.run(transport="http", **run_options(settings))


if __name__ == "__main__":
    main()
