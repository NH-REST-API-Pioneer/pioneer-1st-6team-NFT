"""키 등록 화면(/keys)의 웹 로그인.

Google 계정으로 로그인하고, MCP 로그인과 같은 규칙(AccessPolicy, 계정 정지)으로 거른다.
Google 주소와 토큰 확인 방식(tokeninfo, aud 확인)은 FastMCP의 Google 로그인과 같다.
로그인 상태는 암호화한 쿠키에 둔다. 쿠키 열쇠는 KEY_ENCRYPTION_KEY에서 용도별로 갈라 쓴다.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx2
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from nh_mcp.auth.access import AccessPolicy, Identity
from nh_mcp.config import Settings
from nh_mcp.domain import Clock, JsonValue, UserId
from nh_mcp.nh.client import parse_body
from nh_mcp.store import users

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"

KEYS_PATH = "/keys"
LOGIN_PATH = "/keys/login"
CALLBACK_PATH = "/keys/callback"
LOGOUT_PATH = "/keys/logout"
APPROVE_PREFIX = "/limits/approve/"
TOKEN_CHARS = re.compile(r"[A-Za-z0-9_-]{1,128}")
SESSION_COOKIE = "nh_mcp_session"
# 세션은 /keys 밖의 한도 승인 화면에도 보내야 한다.
SESSION_PATH = "/"
LOGIN_COOKIE = "nh_mcp_login"
SESSION_SECONDS = 30 * 60
LOGIN_SECONDS = 10 * 60
NONCE_BYTES = 12
TIMEOUT_SECONDS = 10.0
HTTP_OK = 200

type JsonObject = dict[str, JsonValue]


def safe_return_to(value: str) -> str:
    """로그인 뒤 돌아갈 같은 서버 경로. 허용 목록(/keys, 한도 승인 링크) 밖은 모두 /keys."""
    if value == KEYS_PATH:
        return value
    if value.startswith(APPROVE_PREFIX) and TOKEN_CHARS.fullmatch(value[len(APPROVE_PREFIX) :]):
        return value
    return KEYS_PATH


class CookieSealer:
    """쿠키 값을 암호화하고 만료 시각을 넣는다. 용도마다 다른 열쇠를 쓴다."""

    def __init__(self, master_key: bytes, purpose: str) -> None:
        info = f"nh_mcp:{purpose}:v1".encode()
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(master_key)
        self._aead = AESGCM(key)

    def seal(self, data: JsonObject, expires_at: int) -> str:
        nonce = os.urandom(NONCE_BYTES)
        plain = json.dumps({**data, "exp": expires_at}).encode()
        return base64.urlsafe_b64encode(nonce + self._aead.encrypt(nonce, plain, None)).decode()

    def unseal(self, text: str, now: int) -> JsonObject | None:
        try:
            raw = base64.urlsafe_b64decode(text.encode())
            plain = self._aead.decrypt(raw[:NONCE_BYTES], raw[NONCE_BYTES:], None)
            data = parse_body(plain.decode())
        except (ValueError, InvalidTag):
            return None
        if not isinstance(data, dict):
            return None
        expires_at = data.get("exp")
        if not isinstance(expires_at, int) or expires_at <= now:
            return None
        return data


@dataclass(frozen=True)
class WebUser:
    user_id: UserId
    email: str
    csrf: str


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _message(status: int, text: str) -> HTMLResponse:
    body = f"<!doctype html><meta charset='utf-8'><title>NH MCP</title><p>{text}</p>"
    return HTMLResponse(body, status_code=status)


class GoogleWebLogin:
    def __init__(
        self,
        settings: Settings,
        conn: sqlite3.Connection,
        clock: Clock,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._policy = AccessPolicy(settings.open_signup, settings.admin_emails)
        self._conn = conn
        self._clock = clock
        self._transport = transport
        self._session = CookieSealer(settings.key_encryption_key, "web-session")
        self._login = CookieSealer(settings.key_encryption_key, "web-login")
        self._secure = settings.public_base_url.startswith("https://")

    @property
    def redirect_uri(self) -> str:
        return self._settings.public_base_url + CALLBACK_PATH

    def current_user(self, request: Request) -> WebUser | None:
        return self.user_from_cookie(request.cookies.get(SESSION_COOKIE, ""))

    def user_from_cookie(self, value: str) -> WebUser | None:
        data = self._session.unseal(value, self._now()) if value else None
        if data is None:
            return None
        user_id, email, csrf = data.get("uid"), data.get("email"), data.get("csrf")
        if not isinstance(user_id, int) or not isinstance(email, str) or not isinstance(csrf, str):
            return None
        # 로그인한 뒤에 정지된 계정도 바로 막는다.
        if users.is_suspended(self._conn, UserId(user_id)):
            return None
        return WebUser(UserId(user_id), email, csrf)

    async def login(self, request: Request) -> Response:
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
        query = urlencode(
            {
                "client_id": self._settings.google_client_id,
                "redirect_uri": self.redirect_uri,
                "response_type": "code",
                "scope": "openid email",
                "state": state,
                "code_challenge": _pkce_challenge(verifier),
                "code_challenge_method": "S256",
                "prompt": "select_account",
            }
        )
        response = RedirectResponse(f"{AUTHORIZE_URL}?{query}", status_code=302)
        return_to = safe_return_to(request.query_params.get("return_to", ""))
        cookie = self._login.seal(
            {"state": state, "verifier": verifier, "return_to": return_to},
            self._now() + LOGIN_SECONDS,
        )
        self._set_cookie(response, LOGIN_COOKIE, cookie, LOGIN_SECONDS)
        return response

    async def callback(self, request: Request) -> Response:
        pending = self._login.unseal(request.cookies.get(LOGIN_COOKIE, ""), self._now())
        state, code = request.query_params.get("state", ""), request.query_params.get("code", "")
        expected = pending.get("state") if pending else None
        verifier = pending.get("verifier") if pending else None
        if (
            not isinstance(expected, str)
            or not isinstance(verifier, str)
            or not code
            or not hmac.compare_digest(expected, state)
        ):
            return _message(400, "로그인 요청이 만료됐거나 맞지 않습니다. 다시 로그인해 주세요.")

        identity = await self._identity(code, verifier)
        if identity is None:
            return _message(502, "Google 로그인을 확인하지 못했습니다. 다시 시도해 주세요.")
        if not self._policy.allows(identity):
            return _message(403, "이 계정은 사용할 수 없습니다.")
        user_id = users.get_or_create(self._conn, identity.sub, identity.email, self._clock())
        if users.is_suspended(self._conn, user_id):
            return _message(403, "이 계정은 사용할 수 없습니다.")

        return_to = pending.get("return_to") if pending else None
        target = safe_return_to(return_to if isinstance(return_to, str) else "")
        response = RedirectResponse(target, status_code=303)
        session: JsonObject = {
            "uid": int(user_id),
            "email": identity.email,
            "csrf": secrets.token_urlsafe(32),
        }
        cookie = self._session.seal(session, self._now() + SESSION_SECONDS)
        self._set_cookie(response, SESSION_COOKIE, cookie, SESSION_SECONDS, SESSION_PATH)
        response.delete_cookie(LOGIN_COOKIE, path=KEYS_PATH)
        return response

    async def logout(self, request: Request) -> Response:
        user = self.current_user(request)
        form = await request.form()
        if user is None or not hmac.compare_digest(str(form.get("csrf", "")), user.csrf):
            return _message(400, "요청이 맞지 않습니다.")
        response = RedirectResponse(LOGIN_PATH, status_code=303)
        response.delete_cookie(SESSION_COOKIE, path=SESSION_PATH)
        return response

    async def _identity(self, code: str, verifier: str) -> Identity | None:
        form = {
            "client_id": self._settings.google_client_id,
            "client_secret": self._settings.google_client_secret,
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": self.redirect_uri,
        }
        try:
            async with httpx2.AsyncClient(
                transport=self._transport, timeout=TIMEOUT_SECONDS
            ) as http:
                exchanged = await http.post(TOKEN_URL, data=form)
                token = _field(exchanged, "access_token")
                if exchanged.status_code != HTTP_OK or not token:
                    return None
                info = await http.get(TOKENINFO_URL, headers={"Authorization": f"Bearer {token}"})
        except httpx2.HTTPError:
            return None
        sub, email = _field(info, "sub"), _field(info, "email")
        # 다른 앱이 받은 Google 토큰을 막는다.
        if info.status_code != HTTP_OK or _field(info, "aud") != self._settings.google_client_id:
            return None
        if not sub or not email:
            return None
        return Identity(sub, email, _field(info, "email_verified") == "true")

    def _now(self) -> int:
        return int(self._clock().timestamp())

    def _set_cookie(
        self, response: Response, name: str, value: str, max_age: int, path: str = KEYS_PATH
    ) -> None:
        response.set_cookie(
            name,
            value,
            max_age=max_age,
            path=path,
            httponly=True,
            secure=self._secure,
            samesite="lax",
        )


def _field(response: httpx2.Response, name: str) -> str:
    try:
        body = parse_body(response.text)
    except ValueError:
        return ""
    value = body.get(name) if isinstance(body, dict) else None
    if isinstance(value, bool):
        return "true" if value else "false"
    return value if isinstance(value, str) else ""


def add_web_login_routes(mcp: FastMCP, login: GoogleWebLogin) -> None:
    mcp.custom_route(LOGIN_PATH, methods=["GET"])(login.login)
    mcp.custom_route(CALLBACK_PATH, methods=["GET"])(login.callback)
    mcp.custom_route(LOGOUT_PATH, methods=["POST"])(login.logout)
