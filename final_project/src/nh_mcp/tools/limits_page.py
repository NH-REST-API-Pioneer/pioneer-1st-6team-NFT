"""한도 승인 링크 화면(/limits/approve/{token}).

AI 클라이언트가 승인 창을 못 띄울 때 `set_risk_limits`가 이 링크를 준다. 사용자가 브라우저에서
Google로 로그인해 지금 값과 바뀔 값을 보고 직접 승인하거나 거절한다. AI는 이 화면에 닿지 못한다.
GET은 아무것도 바꾸지 않는다. 링크가 없는 것, 만료, 이미 쓴 것, 남의 것은 같은 문구로 답한다.
"""

from html import escape
from urllib.parse import urlencode

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from nh_mcp.auth.pages import checked_form, csrf_input, form_field, html_page
from nh_mcp.auth.web import APPROVE_PREFIX, LOGIN_PATH, TOKEN_CHARS, GoogleWebLogin, WebUser
from nh_mcp.domain import RiskLimits
from nh_mcp.services.limits import APPROVE_PATH, LimitService

TITLE = "NH 한도 승인"
ROUTE = APPROVE_PATH + "/{token}"
GONE = "만료되었거나 없는 링크입니다. 한도는 바뀌지 않았습니다. AI에게 다시 요청해 주세요."
CHANGED = "그 사이 한도가 바뀌어 적용하지 않았습니다. AI에게 다시 요청해 주세요."
APPLIED = "한도를 적용했습니다."
REJECTED = "거절했습니다. 한도는 바뀌지 않았습니다."


_LABELS = (
    "하루 손실 한도",
    "종목별 최대 계약",
    "1회 최대 계약",
    "옵션 하루 프리미엄",
    "옵션 전체 최대 계약",
    "옵션 매도 최대 계약",
)


def _value(limits: RiskLimits | None) -> tuple[str, ...]:
    """_LABELS 순서의 값. 옵션 한도가 없으면 옵션 칸은 "없음"이다."""
    if limits is None:
        return ("없음",) * len(_LABELS)
    futures = (
        f"{limits.daily_loss}원",
        f"{limits.max_position}계약",
        f"{limits.max_order_qty}계약",
    )
    options = limits.options
    if options is None:
        return (*futures, "없음", "없음", "없음")
    return (
        *futures,
        f"{options.premium_daily}원",
        f"{options.max_position}계약",
        f"{options.max_short}계약",
    )


def _message(text: str, status: int = 200) -> HTMLResponse:
    return html_page(TITLE, f"<h1>한도 승인</h1><p class='result'>{escape(text)}</p>", status)


def _review(user: WebUser, current: RiskLimits | None, requested: RiskLimits) -> str:
    rows = zip(
        _LABELS,
        _value(current),
        _value(requested),
        strict=True,
    )
    table = "".join(
        f"<tr><td>{label}</td><td>{escape(before)}</td><td>{escape(after)}</td></tr>"
        for label, before, after in rows
    )
    return f"""
<h1>한도 승인</h1>
<p>{escape(user.email)} 로 로그인했습니다. AI가 요청한 한도 변경을 확인해 주세요.</p>
<table><tr><th>항목</th><th>지금</th><th>바뀔 값</th></tr>{table}</table>
<p class='note'>직접 요청한 변경이 아니면 거절하세요. 링크는 곧 만료되고 한 번만 쓸 수 있습니다.</p>
<form method='post'>
{csrf_input(user)}
<button type='submit' name='decision' value='approve'>승인</button>
<button type='submit' name='decision' value='reject'>거절</button>
</form>
"""


class LimitsPage:
    def __init__(self, login: GoogleWebLogin, service: LimitService) -> None:
        self._login = login
        self._service = service

    async def show(self, request: Request) -> Response:
        token = str(request.path_params["token"])
        user = self._login.current_user(request)
        if user is None:
            back = urlencode({"return_to": APPROVE_PREFIX + token})
            return RedirectResponse(f"{LOGIN_PATH}?{back}", status_code=303)
        if not TOKEN_CHARS.fullmatch(token):
            return _message(GONE)
        pending = self._service.pending(user.user_id, token)
        if pending is None:
            return _message(GONE)
        return html_page(TITLE, _review(user, *pending))

    async def decide(self, request: Request) -> Response:
        checked = await checked_form(self._login, request)
        if checked is None:
            return _message("요청이 맞지 않습니다. 링크를 다시 열어 로그인해 주세요.", status=403)
        user, form = checked
        decision = form_field(form, "decision")
        if decision not in ("approve", "reject"):
            return _message("요청이 맞지 않습니다.", status=400)
        token = str(request.path_params["token"])
        if not TOKEN_CHARS.fullmatch(token):
            return _message(GONE)
        if decision == "reject":
            return _message(REJECTED if self._service.reject_link(user.user_id, token) else GONE)
        outcome = self._service.approve_link(user.user_id, token)
        if outcome == "applied":
            return _message(APPLIED)
        return _message(CHANGED if outcome == "changed" else GONE)


def add_limits_routes(mcp: FastMCP, page: LimitsPage) -> None:
    mcp.custom_route(ROUTE, methods=["GET"])(page.show)
    mcp.custom_route(ROUTE, methods=["POST"])(page.decide)
