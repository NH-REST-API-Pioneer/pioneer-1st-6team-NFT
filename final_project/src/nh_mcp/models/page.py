"""모델 파일 화면(/models). 로그인한 사용자만 쓰고, 모든 폼은 CSRF 토큰을 확인한다.

올리기는 multipart 본문을 받는 대로 읽어 그 사용자 폴더의 임시 파일에 바로 쓴다. 본문 전체를
메모리나 /tmp에 모으지 않는다. 파일이 50MiB를 넘는 순간 쓰기를 멈추고 지운다. 본문은 그 크기에
여유를 더한 만큼만 읽는다. 결과는 정해 둔 문장으로만 알린다.
"""

import contextlib
import hmac
from collections.abc import AsyncIterator, Callable
from html import escape
from typing import TYPE_CHECKING, Literal

from fastmcp import FastMCP
from python_multipart.exceptions import FormParserError
from python_multipart.multipart import MultipartParser, parse_options_header
from starlette.requests import ClientDisconnect, Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from nh_mcp.auth.pages import checked_form, csrf_input, form_field, html_page
from nh_mcp.auth.web import KEYS_PATH, LOGIN_PATH, LOGOUT_PATH, MODELS_PATH, GoogleWebLogin, WebUser
from nh_mcp.domain import KST, MAX_MODEL_BYTES, model_hash
from nh_mcp.models.store import MAX_FILES, Draft, ModelStore, TooBig, display_name
from nh_mcp.store.models import ModelRow

if TYPE_CHECKING:
    # 타입 검사에만 있는 이름이다(python_multipart가 실행 때는 정의하지 않는다).
    from python_multipart.multipart import MultipartCallbacks

UPLOAD_PATH = "/models/upload"
DELETE_PATH = "/models/delete"
# 본문 상한: 파일 상한 + 경계·칸 머리말·csrf 칸이 들어갈 여유(64KiB).
MAX_UPLOAD_BODY = MAX_MODEL_BYTES + 65_536
# csrf 칸 값의 길이 상한(토큰은 43자다).
MAX_CSRF = 256
FILE_FIELD = b"model"
CSRF_FIELD = b"csrf"
MIB = 1024 * 1024
RESULTS = {
    "uploaded": "모델 파일을 올렸습니다.",
    "deleted": "모델 파일을 지웠습니다.",
    "too_big": f"파일이 {MAX_MODEL_BYTES // MIB}MB를 넘어 올리지 않았습니다.",
    "too_many": f"모델 파일은 {MAX_FILES}개까지 둘 수 있습니다. 하나를 지운 뒤 다시 올려 주세요.",
    "empty": "올릴 파일을 골라 주세요. 빈 파일은 받지 않습니다.",
    "missing": "그런 모델 파일이 없습니다.",
}


# 올리기를 받지 않은 이유. forbidden은 csrf가 없거나 틀림, malformed는 이 화면의 폼(csrf 다음
# 파일 하나)이 아님, too_big은 파일 또는 본문이 상한을 넘음, empty는 파일이 없거나 빔.
type Refusal = Literal["forbidden", "malformed", "too_big", "empty"]


class _Refused(Exception):
    def __init__(self, reason: Refusal) -> None:
        super().__init__(reason)
        self.reason: Refusal = reason


class _UploadForm:
    """multipart 본문을 받는 대로 읽는다. 칸은 csrf, 그다음 파일 하나(model)뿐이다.

    csrf가 파일보다 먼저 와서 맞아야 파일을 쓰기 시작한다(화면의 폼은 csrf 칸이 먼저다).
    파일 내용은 초안에 바로 쓰고, 상한을 넘으면 초안이 TooBig을 낸다. 받지 않으면 _Refused.
    """

    def __init__(self, csrf: str, begin: Callable[[], Draft]) -> None:
        self._csrf = csrf.encode()
        self._begin = begin
        self._header = bytearray()
        self._value = bytearray()
        self._disposition = b""
        # 지금 읽는 칸: csrf 값을 모으는 중이면 bytearray, 파일이면 None.
        self._csrf_value: bytearray | None = None
        self._in_file = False
        self.csrf_ok = False
        self.draft: Draft | None = None
        self.filename = ""
        # 마지막 경계까지 받았는가. 중간에 끊긴 본문의 파일은 보관하지 않는다.
        self.complete = False

    def callbacks(self) -> "MultipartCallbacks":
        return {
            "on_part_begin": self._part_begin,
            "on_header_field": self._header_field,
            "on_header_value": self._header_value,
            "on_header_end": self._header_end,
            "on_headers_finished": self._headers_finished,
            "on_part_data": self._part_data,
            "on_part_end": self._part_end,
            "on_end": self._end,
        }

    async def read(self, request: Request) -> Draft:
        """본문을 끝까지 읽고 다 받은 초안을 돌려준다. 받지 않으면 _Refused."""
        boundary = _boundary(request.headers.get("content-type", ""))
        if boundary is None:
            raise _Refused("malformed")
        if _declared_too_big(request.headers.get("content-length")):
            raise _Refused("too_big")
        parser = MultipartParser(boundary, self.callbacks())
        stream = request.stream()
        received = 0
        try:
            async for chunk in stream:
                received += len(chunk)
                if received > MAX_UPLOAD_BODY:
                    raise TooBig
                parser.write(chunk)
            parser.finalize()
        except TooBig:
            await _drain(stream, received)
            raise _Refused("too_big") from None
        except (FormParserError, ClientDisconnect):
            raise _Refused("malformed") from None
        if not self.csrf_ok:
            raise _Refused("forbidden")
        if not self.complete:
            raise _Refused("malformed")
        if self.draft is None:
            raise _Refused("empty")
        return self.draft

    def discard(self) -> None:
        if self.draft is not None:
            self.draft.discard()

    def _part_begin(self) -> None:
        self._disposition = b""

    def _header_field(self, data: bytes, start: int, end: int) -> None:
        self._header += data[start:end]

    def _header_value(self, data: bytes, start: int, end: int) -> None:
        self._value += data[start:end]

    def _header_end(self) -> None:
        if bytes(self._header).lower() == b"content-disposition":
            self._disposition = bytes(self._value)
        self._header.clear()
        self._value.clear()

    def _headers_finished(self) -> None:
        _, options = parse_options_header(self._disposition)
        name, filename = options.get(b"name"), options.get(b"filename")
        if name == CSRF_FIELD and filename is None and not self.csrf_ok:
            self._csrf_value = bytearray()
        elif name == FILE_FIELD and filename is not None and self.draft is None:
            if not self.csrf_ok:
                raise _Refused("forbidden")
            self.filename = filename.decode("utf-8", errors="replace")
            self.draft = self._begin()
            self._in_file = True
        else:
            raise _Refused("malformed")

    def _part_data(self, data: bytes, start: int, end: int) -> None:
        if self._in_file and self.draft is not None:
            self.draft.write(data[start:end])
        elif self._csrf_value is not None:
            self._csrf_value += data[start:end]
            if len(self._csrf_value) > MAX_CSRF:
                raise _Refused("forbidden")

    def _part_end(self) -> None:
        if self._csrf_value is not None:
            if not hmac.compare_digest(bytes(self._csrf_value), self._csrf):
                raise _Refused("forbidden")
            self.csrf_ok = True
            self._csrf_value = None
        self._in_file = False

    def _end(self) -> None:
        self.complete = True


def _boundary(content_type: str) -> bytes | None:
    kind, options = parse_options_header(content_type)
    boundary = options.get(b"boundary")
    return boundary if kind == b"multipart/form-data" and boundary else None


def _declared_too_big(declared: str | None) -> bool:
    return declared is not None and (not declared.isdigit() or int(declared) > MAX_UPLOAD_BODY)


async def _drain(stream: AsyncIterator[bytes], received: int) -> None:
    """남은 본문을 버리며 읽는다(본문 상한까지만). 다 읽어야 브라우저가 답을 받는다."""
    if received > MAX_UPLOAD_BODY:
        return
    with contextlib.suppress(ClientDisconnect):
        async for chunk in stream:
            received += len(chunk)
            if received > MAX_UPLOAD_BODY:
                return


def _html(body: str, status: int = 200) -> HTMLResponse:
    return html_page("모델 파일", body, status)


def _result(name: str) -> RedirectResponse:
    return RedirectResponse(f"{MODELS_PATH}?result={name}", status_code=303)


def _forbidden() -> HTMLResponse:
    return _html("<p>요청이 맞지 않습니다. 다시 로그인해 주세요.</p>", status=403)


def _malformed() -> HTMLResponse:
    return _html("<p>요청이 맞지 않습니다. 이 화면의 올리기 폼으로 다시 올려 주세요.</p>", 400)


def _refusal(reason: Refusal) -> Response:
    match reason:
        case "forbidden":
            return _forbidden()
        case "malformed":
            return _malformed()
        case "too_big" | "empty":
            return _result(reason)


def _row(user: WebUser, f: ModelRow) -> str:
    uploaded = f.uploaded_at.astimezone(KST).strftime("%Y-%m-%d %H:%M")
    return (
        f"<tr><td>{escape(f.name)}</td><td><code>{f.sha256}</code></td>"
        f"<td>{f.size:,}바이트</td><td>{uploaded}</td><td>"
        f"<form method='post' action='{DELETE_PATH}'>{csrf_input(user)}"
        f"<input type='hidden' name='sha256' value='{f.sha256}'>"
        "<button type='submit'>지우기</button></form></td></tr>"
    )


def render(user: WebUser, files: list[ModelRow], result: str | None) -> HTMLResponse:
    rows = "".join(_row(user, f) for f in files)
    table = (
        "<table><tr><th>이름</th><th>SHA-256</th><th>크기</th><th>올린 시각</th><th></th></tr>"
        f"{rows}</table>"
        if files
        else "<p>올린 모델 파일이 없습니다.</p>"
    )
    notice = f"<p class='result'>{escape(RESULTS[result])}</p>" if result in RESULTS else ""
    body = f"""
<h1>모델 파일</h1>
<p>{escape(user.email)} 로 로그인했습니다.</p>
{notice}
<p class='note'>다른 곳에서 학습한 모델 파일을 올려 두면 전략이 백테스트에서 쓸 수 있습니다.
AI에게 아래 SHA-256을 알려 주세요. AI가 backtest_strategy의 strategy.model에 넣으면, 이 파일이
격리 컨테이너 안의 /tmp/model로 복사되고 전략 코드는 init(ctx)에서 ctx.model_path로 읽습니다.
서버는 파일을 열어 보지 않습니다. 파일당 {MAX_MODEL_BYTES // MIB}MB, {MAX_FILES}개까지이고,
같은 파일을 다시 올리면 하나로 남습니다.</p>
<h2>올린 파일</h2>
{table}
<h2>올리기</h2>
<form method='post' action='{UPLOAD_PATH}' enctype='multipart/form-data'>
{csrf_input(user)}
<p><input type='file' name='model' required></p>
<p><button type='submit'>올리기</button></p>
</form>
<p><a href='{KEYS_PATH}'>키 등록 화면</a></p>
<form method='post' action='{LOGOUT_PATH}'>
{csrf_input(user)}
<button type='submit'>로그아웃</button>
</form>
"""
    return _html(body)


class ModelsPage:
    def __init__(self, login: GoogleWebLogin, store: ModelStore) -> None:
        self._login = login
        self._store = store

    async def show(self, request: Request) -> Response:
        user = self._login.current_user(request)
        if user is None:
            return RedirectResponse(f"{LOGIN_PATH}?return_to={MODELS_PATH}", status_code=303)
        return render(user, self._store.files(user.user_id), request.query_params.get("result"))

    async def upload(self, request: Request) -> Response:
        user = self._login.current_user(request)
        if user is None:
            return _forbidden()
        form = _UploadForm(user.csrf, lambda: self._store.draft(user.user_id))
        try:
            draft = await form.read(request)
        except _Refused as exc:
            form.discard()
            return _refusal(exc.reason)
        name = display_name(form.filename)
        return _result(await self._store.commit(user.user_id, draft, name))

    async def delete(self, request: Request) -> Response:
        checked = await checked_form(self._login, request)
        if checked is None:
            return _forbidden()
        user, form = checked
        sha256 = model_hash(form_field(form, "sha256"))
        if sha256 is None or not self._store.delete(user.user_id, sha256):
            return _result("missing")
        return _result("deleted")


def add_models_routes(mcp: FastMCP, page: ModelsPage) -> None:
    mcp.custom_route(MODELS_PATH, methods=["GET"])(page.show)
    mcp.custom_route(UPLOAD_PATH, methods=["POST"])(page.upload)
    mcp.custom_route(DELETE_PATH, methods=["POST"])(page.delete)
