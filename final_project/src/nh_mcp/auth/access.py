"""누가 들어올 수 있는지 정한다. 도구 목록과 도구 호출이 모두 이 검사를 지난다."""

import sqlite3
from dataclasses import dataclass

from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.dependencies import get_access_token
from fastmcp.utilities.authorization import AuthCheck, AuthContext

from nh_mcp.domain import Clock, UserId
from nh_mcp.store import users


@dataclass(frozen=True)
class Identity:
    sub: str
    email: str
    email_verified: bool


@dataclass(frozen=True)
class AccessPolicy:
    open_signup: bool
    admin_emails: frozenset[str]

    def allows(self, identity: Identity) -> bool:
        # 검증되지 않은 이메일은 막는다. 모르면 막는다.
        if not identity.email_verified:
            return False
        return self.open_signup or identity.email.lower() in self.admin_emails


def identity_of(token: AccessToken | None) -> Identity | None:
    if token is None:
        return None
    sub, email = token.claims.get("sub"), token.claims.get("email")
    if not isinstance(sub, str) or not sub or not isinstance(email, str) or not email:
        return None
    verified = token.claims.get("email_verified")
    return Identity(sub, email, verified is True or verified == "true")


def make_gate(policy: AccessPolicy, conn: sqlite3.Connection, clock: Clock) -> AuthCheck:
    async def gate(ctx: AuthContext) -> bool:
        identity = identity_of(ctx.token)
        if identity is None or not policy.allows(identity):
            return False
        # 허용된 사용자만 기록한다.
        user_id = users.get_or_create(conn, identity.sub, identity.email, clock())
        return not users.is_suspended(conn, user_id)

    return gate


class NotLoggedIn(Exception):
    """로그인한 사용자를 알 수 없다. 게이트를 지났다면 일어나지 않는다."""


def current_user_id(conn: sqlite3.Connection) -> UserId:
    """지금 MCP 요청을 보낸 사용자. 게이트가 허용한 사용자만 여기 온다."""
    identity = identity_of(get_access_token())
    user_id = users.find_by_google_sub(conn, identity.sub) if identity else None
    if user_id is None:
        raise NotLoggedIn("로그인한 사용자를 확인하지 못했습니다.")
    return user_id
