"""모델 파일 화면(/models): 로그인, CSRF, 올리기·지우기, 크기·개수 상한, 남의 파일, 보안 머리말.

실제 HTTP 서버에 브라우저처럼 multipart로 올린다. 모델 파일은 tmp 폴더에 쌓인다.
"""

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx2
from conftest import Serve
from fakes import ADMIN, World, browser_login

from nh_mcp.auth.web import LOGIN_PATH, SESSION_COOKIE
from nh_mcp.domain import MAX_MODEL_BYTES, ModelHash, UserId
from nh_mcp.models.page import DELETE_PATH, MODELS_PATH, UPLOAD_PATH
from nh_mcp.models.store import MAX_FILES
from nh_mcp.store import users

type Step = Callable[[httpx2.AsyncClient, str, str], Awaitable[None]]

CONTENT = b"\x80\x04\x95 fake sklearn pickle"


def sha(content: bytes) -> ModelHash:
    return ModelHash(hashlib.sha256(content).hexdigest())


class Desk:
    """tmp 폴더에 모델 파일을 두는 서버 하나와, 로그인한 브라우저로 그 화면을 쓰는 방법."""

    def __init__(self, tmp_path: Path) -> None:
        self.world = World(data_dir=tmp_path)
        self.root = tmp_path / "models"

    def me(self) -> UserId:
        return users.get_or_create(self.world.conn, "g-admin", ADMIN, self.world.clock())

    def browse(self, serve: Serve, step: Step, *, login: bool = True) -> None:
        async def scenario() -> None:
            async with serve(self.world.mcp) as url, httpx2.AsyncClient() as http:
                base = url.removesuffix("/mcp")
                csrf = ""
                if login:
                    await browser_login(http, base, "admin-code")
                    cookie = http.cookies.get(SESSION_COOKIE) or ""
                    user = self.world.login.user_from_cookie(cookie)
                    assert user is not None
                    csrf = user.csrf
                await step(http, base, csrf)

        asyncio.run(scenario())

    def on_disk(self) -> list[str]:
        """모델 폴더의 모든 파일(임시 파일 포함)."""
        if not self.root.exists():
            return []
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*") if p.is_file())

    def rows(self) -> list[tuple[int, str, str, int]]:
        found: list[tuple[int, str, str, int]] = self.world.conn.execute(
            "SELECT user_id, sha256, name, size FROM models ORDER BY rowid"
        ).fetchall()
        return found


async def upload(
    http: httpx2.AsyncClient,
    base: str,
    csrf: str | None,
    content: bytes = CONTENT,
    filename: str = "model.pkl",
) -> httpx2.Response:
    """화면의 폼처럼 csrf 칸 다음에 파일 칸을 보낸다. csrf가 None이면 칸을 빼고 보낸다."""
    data = {} if csrf is None else {"csrf": csrf}
    files = {"model": (filename, content, "application/octet-stream")}
    return await http.post(base + UPLOAD_PATH, data=data, files=files)


def test_page_needs_login(serve: Serve, tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    answers: list[httpx2.Response] = []

    async def step(http: httpx2.AsyncClient, base: str, _: str) -> None:
        answers.append(await http.get(base + MODELS_PATH))

    desk.browse(serve, step, login=False)

    assert answers[0].status_code == 303
    # 로그인하고 나면 모델 화면으로 돌아온다.
    assert answers[0].headers["location"] == LOGIN_PATH + "?return_to=" + MODELS_PATH


def test_posts_without_login_change_nothing(serve: Serve, tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    draft = desk.world.models.draft(desk.me())
    draft.write(CONTENT)
    asyncio.run(desk.world.models.commit(desk.me(), draft, "kept.pkl"))
    answers: list[httpx2.Response] = []

    async def step(http: httpx2.AsyncClient, base: str, _: str) -> None:
        answers.append(await upload(http, base, "any", b"new model"))
        answers.append(await http.post(base + DELETE_PATH, data={"sha256": sha(CONTENT)}))

    desk.browse(serve, step, login=False)

    assert [a.status_code for a in answers] == [403, 403]
    assert desk.on_disk() == [f"{desk.me()}/{sha(CONTENT)}"]
    assert [r[2] for r in desk.rows()] == ["kept.pkl"]


def test_upload_shows_name_hash_size_with_security_headers(serve: Serve, tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    answers: list[httpx2.Response] = []

    async def step(http: httpx2.AsyncClient, base: str, csrf: str) -> None:
        answers.append(await upload(http, base, csrf, filename="<b>내 모델<b>.pkl"))
        answers.append(await http.get(base + answers[0].headers["location"]))

    desk.browse(serve, step)

    uploaded, page = answers
    assert uploaded.status_code == 303
    assert uploaded.headers["location"] == "/models?result=uploaded"
    assert page.status_code == 200
    assert "모델 파일을 올렸습니다." in page.text
    assert sha(CONTENT) in page.text
    assert f"{len(CONTENT):,}바이트" in page.text
    # 이름은 글자 그대로 보여 준다(태그로 해석되지 않는다).
    assert "&lt;b&gt;내 모델&lt;b&gt;.pkl" in page.text and "<b>내 모델" not in page.text
    assert page.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.headers["referrer-policy"] == "same-origin"
    stored = desk.root / str(desk.me()) / sha(CONTENT)
    assert stored.read_bytes() == CONTENT
    assert desk.on_disk() == [f"{desk.me()}/{sha(CONTENT)}"]


def test_missing_forged_or_late_csrf_stores_nothing(serve: Serve, tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    answers: list[httpx2.Response] = []

    async def step(http: httpx2.AsyncClient, base: str, csrf: str) -> None:
        answers.append(await upload(http, base, None))
        answers.append(await upload(http, base, "forged"))
        answers.append(await upload(http, base, csrf + "x"))
        # 파일 칸이 csrf보다 먼저 오면 파일을 쓰기 전에 막는다.
        late = httpx2.Request(
            "POST",
            base + UPLOAD_PATH,
            files=[("model", ("m.pkl", CONTENT)), ("csrf", (None, csrf.encode()))],
        )
        answers.append(await http.send(late))

    desk.browse(serve, step)

    assert [a.status_code for a in answers] == [403, 403, 403, 403]
    assert desk.on_disk() == []
    assert desk.rows() == []


def test_delete_needs_csrf_and_a_post(serve: Serve, tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    answers: list[httpx2.Response] = []
    target = sha(CONTENT)

    async def step(http: httpx2.AsyncClient, base: str, csrf: str) -> None:
        answers.append(await upload(http, base, csrf))
        answers.append(await http.post(base + DELETE_PATH, data={"sha256": target}))
        answers.append(
            await http.post(base + DELETE_PATH, data={"csrf": "forged", "sha256": target})
        )
        answers.append(await http.get(base + DELETE_PATH, params={"sha256": target}))
        answers.append(await http.get(base + MODELS_PATH, params={"sha256": target}))

    desk.browse(serve, step)

    assert [a.status_code for a in answers] == [303, 403, 403, 405, 200]
    assert desk.on_disk() == [f"{desk.me()}/{target}"]
    assert len(desk.rows()) == 1


def test_delete_removes_the_file_and_the_entry(serve: Serve, tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    answers: list[httpx2.Response] = []

    async def step(http: httpx2.AsyncClient, base: str, csrf: str) -> None:
        await upload(http, base, csrf)
        form = {"csrf": csrf, "sha256": sha(CONTENT)}
        answers.append(await http.post(base + DELETE_PATH, data=form))
        answers.append(await http.get(base + answers[0].headers["location"]))
        answers.append(await http.post(base + DELETE_PATH, data=form))

    desk.browse(serve, step)

    deleted, page, again = answers
    assert deleted.headers["location"] == "/models?result=deleted"
    assert "모델 파일을 지웠습니다." in page.text and "올린 모델 파일이 없습니다." in page.text
    assert again.headers["location"] == "/models?result=missing"
    assert desk.on_disk() == []
    assert desk.rows() == []


def test_another_accounts_file_is_invisible_and_cannot_be_deleted(
    serve: Serve, tmp_path: Path
) -> None:
    desk = Desk(tmp_path)
    other = users.get_or_create(desk.world.conn, "g-other", "other@example.com", desk.world.clock())
    theirs = b"someone else's model"
    draft = desk.world.models.draft(other)
    draft.write(theirs)
    asyncio.run(desk.world.models.commit(other, draft, "theirs.pkl"))
    answers: list[httpx2.Response] = []

    async def step(http: httpx2.AsyncClient, base: str, csrf: str) -> None:
        answers.append(await http.get(base + MODELS_PATH))
        form = {"csrf": csrf, "sha256": sha(theirs)}
        answers.append(await http.post(base + DELETE_PATH, data=form))

    desk.browse(serve, step)

    page, delete = answers
    assert sha(theirs) not in page.text and "theirs.pkl" not in page.text
    assert "올린 모델 파일이 없습니다." in page.text
    # 남의 해시는 없는 해시와 같은 답이다.
    assert delete.headers["location"] == "/models?result=missing"
    assert desk.on_disk() == [f"{other}/{sha(theirs)}"]


def test_one_byte_over_50mb_is_refused_and_not_stored(serve: Serve, tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    answers: list[httpx2.Response] = []

    async def step(http: httpx2.AsyncClient, base: str, csrf: str) -> None:
        answers.append(await upload(http, base, csrf, bytes(MAX_MODEL_BYTES + 1)))
        answers.append(await http.get(base + answers[0].headers["location"]))

    desk.browse(serve, step)

    refused, page = answers
    assert refused.status_code == 303
    assert refused.headers["location"] == "/models?result=too_big"
    assert "파일이 50MB를 넘어 올리지 않았습니다." in page.text
    assert desk.on_disk() == []
    assert desk.rows() == []


def test_a_file_of_exactly_50mb_is_stored(serve: Serve, tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    content = bytes(range(256)) * (MAX_MODEL_BYTES // 256)
    answers: list[httpx2.Response] = []

    async def step(http: httpx2.AsyncClient, base: str, csrf: str) -> None:
        answers.append(await upload(http, base, csrf, content))

    desk.browse(serve, step)

    assert answers[0].headers["location"] == "/models?result=uploaded"
    assert desk.rows() == [(desk.me(), sha(content), "model.pkl", MAX_MODEL_BYTES)]
    assert (desk.root / str(desk.me()) / sha(content)).read_bytes() == content


def test_sixth_file_is_refused(serve: Serve, tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    answers: list[httpx2.Response] = []

    async def step(http: httpx2.AsyncClient, base: str, csrf: str) -> None:
        for n in range(MAX_FILES + 1):
            answers.append(await upload(http, base, csrf, f"model {n}".encode()))
        answers.append(await http.get(base + answers[-1].headers["location"]))

    desk.browse(serve, step)

    *uploads, page = answers
    assert [a.headers["location"] for a in uploads] == ["/models?result=uploaded"] * MAX_FILES + [
        "/models?result=too_many"
    ]
    assert "모델 파일은 5개까지 둘 수 있습니다." in page.text
    assert len(desk.rows()) == MAX_FILES
    assert len(desk.on_disk()) == MAX_FILES
    assert sha(b"model 5") not in page.text


def test_same_file_twice_is_one_entry(serve: Serve, tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    answers: list[httpx2.Response] = []

    async def step(http: httpx2.AsyncClient, base: str, csrf: str) -> None:
        answers.append(await upload(http, base, csrf, filename="first.pkl"))
        answers.append(await upload(http, base, csrf, filename="second.pkl"))

    desk.browse(serve, step)

    assert [a.headers["location"] for a in answers] == ["/models?result=uploaded"] * 2
    assert desk.rows() == [(desk.me(), sha(CONTENT), "first.pkl", len(CONTENT))]
    assert desk.on_disk() == [f"{desk.me()}/{sha(CONTENT)}"]


def test_names_with_path_characters_are_only_labels(serve: Serve, tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    names = ["../../../escape.pkl", "..\\..\\win.pkl", "/etc/passwd", "a/../../b.pkl"]
    contents = [f"model {n}".encode() for n in range(len(names))]

    async def step(http: httpx2.AsyncClient, base: str, csrf: str) -> None:
        for name, content in zip(names, contents, strict=True):
            await upload(http, base, csrf, content, filename=name)

    desk.browse(serve, step)

    assert [r[2] for r in desk.rows()] == ["escape.pkl", "win.pkl", "passwd", "b.pkl"]
    # 파일은 모두 내 폴더 안의 해시 이름이다. 모델 폴더 밖에는 아무것도 생기지 않는다.
    assert desk.on_disk() == sorted(f"{desk.me()}/{sha(c)}" for c in contents)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["models"]


def test_empty_or_missing_file_and_other_fields_are_refused(serve: Serve, tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    answers: list[httpx2.Response] = []

    async def step(http: httpx2.AsyncClient, base: str, csrf: str) -> None:
        answers.append(await upload(http, base, csrf, b""))
        answers.append(await http.post(base + UPLOAD_PATH, files={"csrf": (None, csrf.encode())}))
        answers.append(await http.post(base + UPLOAD_PATH, data={"csrf": csrf}))
        extra = {"csrf": csrf, "path": "/tmp/x"}
        answers.append(await http.post(base + UPLOAD_PATH, data=extra, files={"model": CONTENT}))
        two = [("model", ("a.pkl", b"a")), ("model", ("b.pkl", b"b"))]
        answers.append(await http.post(base + UPLOAD_PATH, data={"csrf": csrf}, files=two))

    desk.browse(serve, step)

    assert answers[0].headers["location"] == "/models?result=empty"
    assert answers[1].headers["location"] == "/models?result=empty"
    # 폼 인코딩, 모르는 칸, 파일 둘은 이 화면의 폼이 아니다.
    assert [a.status_code for a in answers[2:]] == [400, 400, 400]
    assert desk.on_disk() == []
    assert desk.rows() == []


def test_keys_page_links_to_the_models_page(serve: Serve, tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    answers: list[httpx2.Response] = []

    async def step(http: httpx2.AsyncClient, base: str, _: str) -> None:
        answers.append(await http.get(base + "/keys"))

    desk.browse(serve, step)

    assert f"href='{MODELS_PATH}'" in answers[0].text
