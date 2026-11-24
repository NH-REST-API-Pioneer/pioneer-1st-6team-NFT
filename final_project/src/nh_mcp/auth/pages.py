"""웹 화면(키 등록, 한도 승인)이 같이 쓰는 것: 보안 머리말, 쪽 틀, CSRF 확인."""

import hmac
from html import escape

from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.responses import HTMLResponse

from nh_mcp.auth.web import GoogleWebLogin, WebUser

SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"
    ),
    # no-referrer면 브라우저가 폼의 Origin을 "null"로 보내 서버의 Origin 검사에 막힌다.
    # same-origin은 같은 사이트에만 Origin을 알리고, 다른 사이트로는 주소를 넘기지 않는다.
    "Referrer-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff",
}
STYLE = (
    "body{font-family:sans-serif;max-width:40rem;margin:2rem auto;padding:0 1rem;line-height:1.5}"
    "table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:.3rem .6rem}"
    "form{margin:1rem 0}.note{color:#555;font-size:.9rem}.result{font-weight:bold}"
)


def html_page(title: str, body: str, status: int = 200) -> HTMLResponse:
    page = (
        "<!doctype html><html lang='ko'><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{escape(title)}</title><style>{STYLE}</style><body>{body}</body></html>"
    )
    return HTMLResponse(page, status_code=status, headers=SECURITY_HEADERS)


def csrf_input(user: WebUser) -> str:
    return f"<input type='hidden' name='csrf' value='{escape(user.csrf, quote=True)}'>"


def form_field(form: FormData, name: str) -> str:
    value = form.get(name)
    return value if isinstance(value, str) else ""


async def checked_form(login: GoogleWebLogin, request: Request) -> tuple[WebUser, FormData] | None:
    """로그인한 사용자와 폼. 로그인이 없거나 CSRF가 맞지 않으면 None."""
    user = login.current_user(request)
    if user is None:
        return None
    form = await request.form()
    if not hmac.compare_digest(form_field(form, "csrf"), user.csrf):
        return None
    return user, form
