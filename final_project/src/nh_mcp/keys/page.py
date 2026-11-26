"""키 등록 화면(/keys). 로그인한 사용자만 쓰고, 모든 폼은 CSRF 토큰을 확인한다.

키는 끝 4자리만 보여 준다. 사용자가 넣은 값을 화면에 되돌려 보여 주지 않는다.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from html import escape

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from nh_mcp.auth.pages import checked_form, csrf_input, form_field, html_page
from nh_mcp.auth.web import KEYS_PATH, LOGIN_PATH, LOGOUT_PATH, GoogleWebLogin, WebUser
from nh_mcp.domain import Market, UserId
from nh_mcp.keys.service import KeyNotAccepted, KeyStore, KeySummary
from nh_mcp.nh.client import Credentials

REGISTER_PATH = "/keys/register"
# 로그인 쿠키가 /keys 아래에만 가므로 스위치도 그 아래에 둔다.
AUTO_RUN_PATH = "/keys/auto-run"
# 모델 파일 화면(nh_mcp.models.page.MODELS_PATH). keys는 models를 import하지 않는다.
MODELS_PATH = "/models"
DELETE_PATH = "/keys/delete"
MARKET_NAMES: dict[Market, str] = {"domestic": "국내", "overseas": "해외"}
RESULTS = {
    "registered": "키를 등록했습니다.",
    "deleted": "등록한 키를 모두 지웠습니다.",
    "empty": "APP Key와 Secret을 모두 넣어 주세요.",
    "rejected": "모의 서버에서 이 키로 토큰을 받지 못했습니다. 모의 계좌의 키인지 확인해 주세요.",
    "unverified": "모의 서버에 확인하지 못했습니다. 잠시 뒤 다시 시도해 주세요.",
    "auto_run_on": "자동 실행을 켰습니다. AI 앱에서 이 서버 연결을 새로 고치면 도구가 보입니다.",
    "auto_run_off": "자동 실행을 껐습니다.",
    "auto_run_stopped": (
        "자동 실행을 끄고 돌던 전략을 모두 멈췄습니다. 걸린 주문은 취소했고 보유 포지션은 그대로 "
        "있습니다. AI에게 계좌(get_account)를 확인해 달라고 하세요."
    ),
}


def _market(value: object) -> Market | None:
    if value == "domestic":
        return "domestic"
    if value == "overseas":
        return "overseas"
    return None


def _html(body: str, status: int = 200) -> HTMLResponse:
    return html_page("NH 모의 키 등록", body, status)


def _auto_run_section(user: WebUser, on: bool) -> str:
    state, button, value = (
        ("켜져 있습니다", "끄기", "off") if on else ("꺼져 있습니다", "켜기", "on")
    )
    return f"""
<h2>자동 실행</h2>
<p>지금 <strong>{state}</strong>.</p>
<p class='note'>켜면 AI가 백테스트한 전략을 승인 범위와 함께 미리보기(preview_strategy)하고,
승인 창에서 승인받아 서버에서 계속 돌릴 수 있습니다(start_strategy). 대화를 닫아도 돌고, 자동
모드는 주문마다 묻지 않습니다. 끄면 이 계정에서 돌던 전략을 모두 멈춥니다. 걸린 주문은 취소하고
보유 포지션은 그대로 둡니다. 켜고 끄는 것은 이 화면에서만 할 수 있고 AI는 바꿀 수 없습니다.</p>
<form method='post' action='{AUTO_RUN_PATH}'>
{csrf_input(user)}
<input type='hidden' name='enabled' value='{value}'>
<button type='submit'>자동 실행 {button}</button>
</form>
"""


def render(
    user: WebUser, keys: list[KeySummary], result: str | None, auto_run: bool | None = None
) -> HTMLResponse:
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
    switch = "" if auto_run is None else _auto_run_section(user, auto_run)
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
{switch}
<h2>모델 파일</h2>
<p>전략에 쓸 학습 모델은 <a href='{MODELS_PATH}'>모델 파일 화면</a>에서 올립니다.</p>
<form method='post' action='{LOGOUT_PATH}'>
{csrf_input(user)}
<button type='submit'>로그아웃</button>
</form>
"""
    return _html(body)


# 키를 모두 지우고 그 사용자의 연결(NH 클라이언트, 틱 저장)을 끊는 일. 서버 조립이 넣어 준다
# (services.connection.ConnectionService.disconnect, keys는 services를 import하지 않는다).
type Disconnect = Callable[[UserId], Awaitable[None]]


@dataclass(frozen=True)
class AutoRunSwitch:
    """ "자동 실행" 묶음 스위치(services.strategy.StrategyService의 auto_run·set_auto_run).

    turn은 켜거나 끄고, 끄면서 멈춘 전략 수를 돌려준다. 주문 스위치가 꺼진 서버에는 없다.
    """

    is_on: Callable[[UserId], bool]
    turn: Callable[[UserId, bool], Awaitable[int]]


class KeysPage:
    def __init__(
        self,
        login: GoogleWebLogin,
        store: KeyStore,
        disconnect: Disconnect,
        auto_run: AutoRunSwitch | None = None,
    ) -> None:
        self._login = login
        self._store = store
        self._disconnect = disconnect
        self._auto_run = auto_run

    async def show(self, request: Request) -> Response:
        user = self._login.current_user(request)
        if user is None:
            return RedirectResponse(LOGIN_PATH, status_code=303)
        result = request.query_params.get("result")
        on = None if self._auto_run is None else self._auto_run.is_on(user.user_id)
        return render(user, self._store.summaries(user.user_id), result, on)

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
        await self._disconnect(checked[0].user_id)
        return RedirectResponse(f"{KEYS_PATH}?result=deleted", status_code=303)

    async def switch_auto_run(self, request: Request) -> Response:
        checked = await checked_form(self._login, request)
        if checked is None or self._auto_run is None:
            return _html("<p>요청이 맞지 않습니다. 다시 로그인해 주세요.</p>", status=403)
        user, form = checked
        wanted = {"on": True, "off": False}.get(form_field(form, "enabled"))
        if wanted is None:
            return _html("<p>켜기 또는 끄기를 골라 주세요.</p>", status=400)
        stopped = await self._auto_run.turn(user.user_id, wanted)
        result = "auto_run_on" if wanted else "auto_run_stopped" if stopped else "auto_run_off"
        return RedirectResponse(f"{KEYS_PATH}?result={result}", status_code=303)


def add_keys_routes(mcp: FastMCP, page: KeysPage) -> None:
    mcp.custom_route(KEYS_PATH, methods=["GET"])(page.show)
    mcp.custom_route(REGISTER_PATH, methods=["POST"])(page.register)
    mcp.custom_route(DELETE_PATH, methods=["POST"])(page.delete)
    mcp.custom_route(AUTO_RUN_PATH, methods=["POST"])(page.switch_auto_run)
