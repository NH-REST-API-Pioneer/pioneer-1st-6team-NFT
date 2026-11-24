"""키 등록 화면(/keys). 로그인한 사용자만 쓰고, 모든 폼은 CSRF 토큰을 확인한다.

키는 끝 4자리만 보여 준다. 사용자가 넣은 값을 화면에 되돌려 보여 주지 않는다.
"""

from html import escape

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from nh_mcp.auth.pages import checked_form, csrf_input, form_field, html_page
from nh_mcp.auth.web import KEYS_PATH, LOGIN_PATH, LOGOUT_PATH, GoogleWebLogin, WebUser
from nh_mcp.domain import Market
from nh_mcp.keys.service import KeyNotAccepted, KeyStore, KeySummary
from nh_mcp.nh.client import Credentials

REGISTER_PATH = "/keys/register"
DELETE_PATH = "/keys/delete"
MARKET_NAMES: dict[Market, str] = {"domestic": "국내", "overseas": "해외"}
RESULTS = {
    "registered": "키를 등록했습니다.",
    "deleted": "등록한 키를 모두 지웠습니다.",
    "empty": "APP Key와 Secret을 모두 넣어 주세요.",
    "rejected": "모의 서버에서 이 키로 토큰을 받지 못했습니다. 모의 계좌의 키인지 확인해 주세요.",
    "unverified": "모의 서버에 확인하지 못했습니다. 잠시 뒤 다시 시도해 주세요.",
}


def _market(value: object) -> Market | None:
    if value == "domestic":
        return "domestic"
    if value == "overseas":
        return "overseas"
    return None


def _html(body: str, status: int = 200) -> HTMLResponse:
    return html_page("NH 모의 키 등록", body, status)


def render(user: WebUser, keys: list[KeySummary], result: str | None) -> HTMLResponse:
    rows = "".join(
        f"<tr><td>{MARKET_NAMES[k.market]}</td><td>····{escape(k.app_key_last4)}</td></tr>"
        for k in keys
    )
    table = (
        f"<table><tr><th>시장</th><th>APP Key</th></tr>{rows}</table>"
        if keys
        else "<p>등록된 키가 없습니다.</p>"
    )
    notice = f"<p class='result'>{escape(RESULTS[result])}</p>" if result in RESULTS else ""
    options = "".join(f"<option value='{m}'>{name}</option>" for m, name in MARKET_NAMES.items())
    body = f"""
<h1>NH선물 모의 키 등록</h1>
<p>{escape(user.email)} 로 로그인했습니다.</p>
{notice}
<p class='note'>모의 계좌의 APP Key만 받습니다. 등록할 때 모의 서버에서 토큰이 발급되는지 시험하고,
통과한 키만 암호화해 저장합니다. 화면에는 끝 4자리만 보여 줍니다.
키를 AI와의 대화창에 적지 마세요. 대화 기록에 남습니다.</p>
<h2>등록된 키</h2>
{table}
<h2>키 등록 또는 교체</h2>
<form method='post' action='{REGISTER_PATH}' autocomplete='off'>
{csrf_input(user)}
<p><label>시장 <select name='market'>{options}</select></label></p>
<p><label>APP Key <input type='password' name='app_key' required></label></p>
<p><label>APP Secret <input type='password' name='app_secret' required></label></p>
<p><button type='submit'>등록</button></p>
</form>
<h2>키 지우기</h2>
<form method='post' action='{DELETE_PATH}'>
{csrf_input(user)}
<button type='submit'>등록한 키를 모두 지우기</button>
</form>
<form method='post' action='{LOGOUT_PATH}'>
{csrf_input(user)}
<button type='submit'>로그아웃</button>
</form>
"""
    return _html(body)


class KeysPage:
    def __init__(self, login: GoogleWebLogin, store: KeyStore) -> None:
        self._login = login
        self._store = store

    async def show(self, request: Request) -> Response:
        user = self._login.current_user(request)
        if user is None:
            return RedirectResponse(LOGIN_PATH, status_code=303)
        result = request.query_params.get("result")
        return render(user, self._store.summaries(user.user_id), result)

    async def register(self, request: Request) -> Response:
        checked = await checked_form(self._login, request)
        if checked is None:
            return _html("<p>요청이 맞지 않습니다. 다시 로그인해 주세요.</p>", status=403)
        user, form = checked
        market = _market(form.get("market"))
        if market is None:
            return _html("<p>시장을 골라 주세요.</p>", status=400)
        credentials = Credentials(form_field(form, "app_key"), form_field(form, "app_secret"))
        try:
            await self._store.register(user.user_id, market, credentials)
        except KeyNotAccepted as exc:
            return RedirectResponse(f"{KEYS_PATH}?result={exc.reason}", status_code=303)
        return RedirectResponse(f"{KEYS_PATH}?result=registered", status_code=303)

    async def delete(self, request: Request) -> Response:
        checked = await checked_form(self._login, request)
        if checked is None:
            return _html("<p>요청이 맞지 않습니다. 다시 로그인해 주세요.</p>", status=403)
        self._store.delete_all(checked[0].user_id)
        return RedirectResponse(f"{KEYS_PATH}?result=deleted", status_code=303)


def add_keys_routes(mcp: FastMCP, page: KeysPage) -> None:
    mcp.custom_route(KEYS_PATH, methods=["GET"])(page.show)
    mcp.custom_route(REGISTER_PATH, methods=["POST"])(page.register)
    mcp.custom_route(DELETE_PATH, methods=["POST"])(page.delete)
